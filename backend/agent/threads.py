"""Find recent threads worth commenting on, and draft helpful replies."""
from __future__ import annotations

import time

from . import llm, reddit_client


_QUERIES_SYSTEM = """You generate Reddit search queries that will surface
*recent posts where a member of the audience is asking for help, sharing
a problem, or comparing options* relevant to the given business.

Goal: find threads where a thoughtful, non-spammy comment from someone
who works in this space would actually be helpful.

Reply with JSON: { "queries": [string] }  -- 4-8 short search queries.
"""


def _build_queries(profile: dict) -> list[str]:
    user = (
        f"Business: {profile.get('one_liner')}\n"
        f"Pain points: {profile.get('pain_points')}\n"
        f"Keywords: {profile.get('keywords')}\n"
    )
    try:
        data = llm.chat_json(_QUERIES_SYSTEM, user, temperature=0.4)
        qs = [q for q in data.get("queries", []) if isinstance(q, str) and q.strip()]
    except Exception:
        qs = []
    if not qs:
        qs = list((profile.get("keywords") or [])[:6])
    return qs[:8]


_RELEVANCE_SYSTEM = """You decide whether a Reddit thread is a good fit
for a non-spammy, helpful comment from someone working at the given
business. Score generously when the OP is asking a question we can
clearly help with; score low when the thread is off-topic, a meme, an
announcement, or somewhere comments would feel promotional.

Reply with JSON: {
  "relevance": int (0-100),
  "intent": "asking_for_help" | "sharing_problem" | "comparing_options"
            | "discussion" | "showcase" | "off_topic",
  "angle": string  // 1 sentence: what genuine value we could add
}
Only output JSON.
"""


_REPLIES_SYSTEM = """You write Reddit comments that *do not look like
ads*. You're commenting as a real human who happens to work in this
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
    { "angle": string, "text": string, "mentions_product": bool }
  ]
}
Generate exactly the requested number of replies.
"""


def _score_relevance(profile: dict, post: dict) -> dict:
    user = (
        f"Business: {profile.get('one_liner')}\n"
        f"Audience: {profile.get('target_audience')}\n"
        f"Pains we solve: {profile.get('pain_points')}\n\n"
        f"Subreddit: r/{post['subreddit']}\n"
        f"Title: {post['title']}\n"
        f"Body: {post.get('selftext', '')[:1500]}\n"
    )
    try:
        return llm.chat_json(_RELEVANCE_SYSTEM, user, temperature=0.2)
    except Exception:
        return {"relevance": 0, "intent": "off_topic", "angle": ""}


def _draft_replies(
    profile: dict, post: dict, comments: list[str], n: int
) -> list[dict]:
    existing = "\n".join(f"- {c[:300]}" for c in comments[:8])
    user = (
        f"Business: {profile.get('name')} -- {profile.get('one_liner')}\n"
        f"Value props: {profile.get('value_props')}\n"
        f"Pains we solve: {profile.get('pain_points')}\n\n"
        f"Subreddit: r/{post['subreddit']}\n"
        f"Post title: {post['title']}\n"
        f"Post body:\n{post.get('selftext', '')[:1800]}\n\n"
        f"Existing top comments (avoid repeating these):\n{existing or '(none)'}\n\n"
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


def find_threads(
    profile: dict,
    subreddits: list[str],
    *,
    per_sub: int = 5,
    total_limit: int = 10,
    replies_per_thread: int = 3,
    min_relevance: int = 55,
    max_age_days: int = 45,
) -> list[dict]:
    queries = _build_queries(profile)
    cutoff = time.time() - max_age_days * 86400
    seen: set[str] = set()
    candidates: list[dict] = []

    for sub in subreddits:
        sub = sub.lstrip("/").removeprefix("r/")
        for q in queries[:4]:
            try:
                posts = reddit_client.search_threads(
                    sub, q, limit=per_sub, time_filter="month", sort="new"
                )
            except Exception:
                posts = []
            for p in posts:
                if p["id"] in seen:
                    continue
                if p.get("over_18"):
                    continue
                if (p.get("created_utc") or 0) < cutoff:
                    continue
                seen.add(p["id"])
                candidates.append(p)
        try:
            for p in reddit_client.list_recent_threads(sub, limit=per_sub):
                if p["id"] in seen or p.get("over_18"):
                    continue
                if (p.get("created_utc") or 0) < cutoff:
                    continue
                seen.add(p["id"])
                candidates.append(p)
        except Exception:
            continue

    candidates.sort(key=lambda p: -(p.get("created_utc") or 0))
    candidates = candidates[: max(total_limit * 4, 20)]

    scored: list[dict] = []
    for p in candidates:
        meta = _score_relevance(profile, p)
        rel = int(meta.get("relevance", 0) or 0)
        if rel < min_relevance:
            continue
        if meta.get("intent") == "off_topic":
            continue
        scored.append(
            {
                **p,
                "relevance": rel,
                "intent": meta.get("intent", ""),
                "angle": meta.get("angle", ""),
            }
        )
        if len(scored) >= total_limit * 2:
            break

    scored.sort(key=lambda p: (-p["relevance"], -(p.get("created_utc") or 0)))
    scored = scored[:total_limit]

    results: list[dict] = []
    for p in scored:
        comments = reddit_client.get_top_comments(p["id"], limit=8)
        replies = _draft_replies(profile, p, comments, replies_per_thread)
        results.append(
            {
                "id": p["id"],
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
