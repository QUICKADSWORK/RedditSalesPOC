"""Find recent threads worth commenting on, and draft helpful replies.

The flow is tuned for efficiency: in Apify mode we make exactly TWO
paid API calls per "find threads" run -- one to pull recent posts from
every selected subreddit, and one to pull comments for the top-scored
threads after the LLM relevance pass.
"""
from __future__ import annotations

import time

from . import llm, reddit_client


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------


_RELEVANCE_BATCH_SYSTEM = """You score Reddit threads for fit with a
business that wants to leave *helpful, non-spammy* comments.

For each post, decide how good a fit it is for someone working in this
space to add value. Score generously when the OP is asking a question
we can clearly help with; score low when the thread is off-topic, a
meme, an announcement, or a place where comments would feel
promotional.

Reply with JSON of this exact shape:

{
  "scores": [
    {
      "id": "<post id from input>",
      "relevance": <int 0-100>,
      "intent": "asking_for_help" | "sharing_problem" |
                "comparing_options" | "discussion" | "showcase" |
                "off_topic",
      "angle": "<one sentence: what genuine value we could add>"
    }
  ]
}

Score every post in the input. Only output JSON.
"""


_REPLIES_SYSTEM = """You write Reddit comments that DO NOT look like
ads. You're commenting as a real human who happens to work in this
space. Rules:

1. Lead with empathy or a concrete observation about OP's situation.
2. Give a specific, useful idea or framework even if they don't use our
   product. No fluff, no buzzwords.
3. At MOST one soft mention of our product, only if it is genuinely the
   best answer. Phrase it like "we built X to solve Y, happy to share
   what we learned" -- never a hard sell, never a link unless it would
   come across as helpful.
4. Match the subreddit tone (casual, lowercase ok, short paragraphs).
5. Each reply should be DIFFERENT in angle (e.g. one tactical tip, one
   contrarian take, one personal anecdote, one question that helps OP
   think).
6. 40-140 words each. No emoji unless the thread uses them.
7. Do NOT repeat what other commenters already said.

Reply with JSON:

{
  "replies": [
    { "angle": "<short label>", "text": "<the comment>",
      "mentions_product": <bool> }
  ]
}

Generate exactly the requested number of replies. Only output JSON.
"""


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def find_threads(
    profile: dict,
    subreddits: list[str],
    *,
    per_sub: int = 10,
    total_limit: int = 8,
    replies_per_thread: int = 3,
    min_relevance: int = 55,
    max_age_days: int = 45,
) -> list[dict]:
    import sys

    def log(msg: str) -> None:
        print(f"[threads] {msg}", file=sys.stderr, flush=True)

    cutoff = time.time() - max_age_days * 86400

    t0 = time.time()
    # We deliberately do NOT include comments inline -- it doubles
    # actor runtime and the relevance/reply prompts work well from the
    # post text alone.
    candidates, inline_comments = reddit_client.bulk_recent_threads(
        subreddits, per_sub=per_sub, include_comments=False
    )
    log(
        f"bulk_recent_threads: {len(candidates)} posts from "
        f"{len(subreddits)} subs in {time.time() - t0:.1f}s"
    )
    candidates = [
        p for p in candidates
        if not p.get("over_18") and (p.get("created_utc") or 0) >= cutoff
    ]
    if not candidates:
        return []

    seen: set[str] = set()
    uniq: list[dict] = []
    for p in candidates:
        pid = p.get("id") or ""
        if not pid or pid in seen:
            continue
        seen.add(pid)
        uniq.append(p)
    uniq.sort(key=lambda p: -(p.get("created_utc") or 0))
    uniq = uniq[:80]

    t1 = time.time()
    scored = _score_batch(profile, uniq)
    log(
        f"scored {len(scored)} posts in {time.time() - t1:.1f}s; "
        f"top relevances="
        f"{sorted([s['relevance'] for s in scored], reverse=True)[:8]}"
    )
    scored = [
        s for s in scored
        if s["relevance"] >= min_relevance and s["intent"] != "off_topic"
    ]
    scored.sort(key=lambda p: (-p["relevance"], -(p.get("created_utc") or 0)))
    scored = scored[:total_limit]
    log(
        f"kept {len(scored)} posts after relevance filter "
        f"(min={min_relevance})"
    )
    if not scored:
        return []

    if inline_comments:
        comments_map = inline_comments
    else:
        comments_map = reddit_client.bulk_post_comments(scored, max_per_post=6)

    results: list[dict] = []
    for p in scored:
        pid = p.get("id") or ""
        comments = comments_map.get(pid, [])
        replies = _draft_replies(profile, p, comments, replies_per_thread)
        results.append(
            {
                "id": pid,
                "subreddit": p["subreddit"],
                "title": p["title"],
                "selftext_preview": (p.get("selftext", "") or "")[:600],
                "url": p["url"],
                "score": p["score"],
                "num_comments": p["num_comments"],
                "created_utc": p["created_utc"],
                "relevance": p["relevance"],
                "intent": p["intent"],
                "angle": p["angle"],
                "top_comments_sampled": comments[:5],
                "replies": replies,
            }
        )
    return results


# ---------------------------------------------------------------------------
# LLM helpers
# ---------------------------------------------------------------------------


def _score_batch(profile: dict, posts: list[dict]) -> list[dict]:
    """One LLM call scores up to ~30 posts at a time. Falls back to
    per-post scoring if the batch call fails to score everything."""
    out: list[dict] = []
    batch_size = 25
    for i in range(0, len(posts), batch_size):
        batch = posts[i : i + batch_size]
        listing = "\n\n".join(
            f"[{p['id']}] r/{p['subreddit']}\n"
            f"  title: {p['title']}\n"
            f"  body: {(p.get('selftext') or '')[:600]}"
            for p in batch
        )
        user = (
            f"Business: {profile.get('one_liner')}\n"
            f"Audience: {profile.get('target_audience')}\n"
            f"Pains we solve: {profile.get('pain_points')}\n\n"
            f"Posts to score:\n{listing}"
        )
        try:
            data = llm.chat_json(_RELEVANCE_BATCH_SYSTEM, user, temperature=0.2)
            scores = {s["id"]: s for s in data.get("scores", []) or []}
        except Exception:
            scores = {}
        for p in batch:
            s = scores.get(p["id"], {})
            out.append(
                {
                    **p,
                    "relevance": int(s.get("relevance", 0) or 0),
                    "intent": s.get("intent", "off_topic"),
                    "angle": s.get("angle", ""),
                }
            )
    return out


def _draft_replies(
    profile: dict, post: dict, comments: list[str], n: int
) -> list[dict]:
    existing = "\n".join(f"- {c[:300]}" for c in comments[:8]) or "(none)"
    user = (
        f"Business: {profile.get('name')} -- {profile.get('one_liner')}\n"
        f"Value props: {profile.get('value_props')}\n"
        f"Pains we solve: {profile.get('pain_points')}\n\n"
        f"Subreddit: r/{post['subreddit']}\n"
        f"Post title: {post['title']}\n"
        f"Post body:\n{(post.get('selftext') or '')[:1800]}\n\n"
        f"Existing top comments (avoid repeating these):\n{existing}\n\n"
        f"Write exactly {n} different replies."
    )
    try:
        data = llm.chat_json(_REPLIES_SYSTEM, user, temperature=0.8)
        replies = data.get("replies") or []
    except Exception:
        replies = []
    cleaned: list[dict] = []
    for r in replies[:n]:
        text = (r.get("text") or "").strip()
        if not text:
            continue
        cleaned.append(
            {
                "angle": (r.get("angle") or "").strip(),
                "text": text,
                "mentions_product": bool(r.get("mentions_product", False)),
            }
        )
    return cleaned
