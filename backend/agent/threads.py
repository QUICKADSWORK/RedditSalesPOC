"""Find recent threads worth commenting on, and draft helpful replies.

The flow is tuned for efficiency: in Apify mode we make exactly ONE
paid API call per "find threads" run -- a single batched scrape of
recent posts across every selected subreddit. The relevance pass is
also batched into one LLM call.

The public entry point comes in two flavours:

  - find_threads(...)                     -> returns the full result list
  - find_threads_stream(...)              -> yields progress events as
                                             dicts, terminating with a
                                             {"type": "done", ...}

The streaming variant lets the API endpoint emit Server-Sent Events so
the HTTP connection stays warm during the long Apify scrape and the
client can show real progress.
"""
from __future__ import annotations

import threading
import time
from typing import Iterator

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
    per_sub: int = 8,
    total_limit: int = 6,
    replies_per_thread: int = 3,
    min_relevance: int = 35,
    max_age_days: int = 45,
) -> list[dict]:
    """Blocking version. Use find_threads_stream for SSE."""
    final: dict = {"threads": []}
    for ev in find_threads_stream(
        profile,
        subreddits,
        per_sub=per_sub,
        total_limit=total_limit,
        replies_per_thread=replies_per_thread,
        min_relevance=min_relevance,
        max_age_days=max_age_days,
    ):
        if ev.get("type") == "done":
            final = ev
    return final.get("threads", [])


def find_threads_stream(
    profile: dict,
    subreddits: list[str],
    *,
    per_sub: int = 8,
    total_limit: int = 6,
    replies_per_thread: int = 3,
    min_relevance: int = 35,
    max_age_days: int = 45,
) -> Iterator[dict]:
    """Yield progress events as the search runs. Each event is a
    JSON-ready dict with a "type" field. The final event is always
    {"type": "done", "threads": [...]}.
    """
    import sys

    def log(msg: str) -> None:
        print(f"[threads] {msg}", file=sys.stderr, flush=True)

    cutoff = time.time() - max_age_days * 86400
    t_start = time.time()

    # ----- step 1: pull recent posts from each subreddit -------------
    yield {
        "type": "step",
        "step": "fetch",
        "message": (
            f"scraping recent posts from {len(subreddits)} subreddit"
            f"{'s' if len(subreddits) != 1 else ''}…"
        ),
    }

    result: dict = {}

    def _scrape() -> None:
        try:
            posts, _ = reddit_client.bulk_recent_threads(
                subreddits, per_sub=per_sub, include_comments=False
            )
            result["posts"] = posts
        except Exception as e:  # noqa: BLE001
            result["error"] = f"scrape failed: {e}"

    th = threading.Thread(target=_scrape, daemon=True)
    th.start()
    # Heartbeat every 3 s so any proxy / browser keeps the connection.
    elapsed = 0
    while th.is_alive():
        th.join(timeout=3.0)
        if th.is_alive():
            elapsed += 3
            yield {
                "type": "heartbeat",
                "step": "fetch",
                "elapsed": elapsed,
                "message": f"still scraping… ({elapsed}s)",
            }
    th.join()
    if "error" in result:
        yield {"type": "done", "threads": [], "error": result["error"]}
        return
    candidates = result.get("posts", [])
    log(
        f"bulk_recent_threads: {len(candidates)} posts from "
        f"{len(subreddits)} subs in {time.time() - t_start:.1f}s"
    )
    yield {
        "type": "fetched",
        "count": len(candidates),
        "message": f"got {len(candidates)} recent posts",
    }
    candidates = [
        p for p in candidates
        if not p.get("over_18") and (p.get("created_utc") or 0) >= cutoff
    ]
    if not candidates:
        yield {
            "type": "done",
            "threads": [],
            "message": (
                "No recent posts came back from Reddit for these "
                "subreddits. Try different subreddits, lower the "
                "min_relevance, or check that APIFY_TOKEN is valid."
            ),
        }
        return

    seen: set[str] = set()
    uniq: list[dict] = []
    for p in candidates:
        pid = p.get("id") or ""
        if not pid or pid in seen:
            continue
        seen.add(pid)
        uniq.append(p)
    uniq.sort(key=lambda p: -(p.get("created_utc") or 0))
    uniq = uniq[:60]

    # ----- step 2: score relevance with the LLM ----------------------
    yield {
        "type": "step",
        "step": "score",
        "message": f"scoring {len(uniq)} posts with the LLM…",
    }
    t1 = time.time()
    try:
        scored = _score_batch(profile, uniq)
    except Exception as e:  # noqa: BLE001
        yield {"type": "done", "threads": [], "error": f"scoring failed: {e}"}
        return
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
        yield {
            "type": "done",
            "threads": [],
            "message": (
                f"Found {len(uniq)} recent posts but none scored above "
                f"{min_relevance}/100. Try lowering min_relevance or "
                f"picking subreddits closer to your audience."
            ),
        }
        return

    # ----- step 3: draft replies for the top threads -----------------
    yield {
        "type": "step",
        "step": "draft",
        "message": (
            f"drafting {replies_per_thread} replies for each of "
            f"{len(scored)} threads…"
        ),
    }

    results: list[dict] = []
    for idx, p in enumerate(scored, 1):
        pid = p.get("id") or ""
        try:
            replies = _draft_replies(profile, p, [], replies_per_thread)
        except Exception as e:  # noqa: BLE001
            log(f"reply draft failed for {pid}: {e}")
            replies = []
        thread = {
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
            "top_comments_sampled": [],
            "replies": replies,
        }
        results.append(thread)
        yield {
            "type": "thread",
            "index": idx,
            "total": len(scored),
            "thread": thread,
        }

    yield {
        "type": "done",
        "threads": results,
        "elapsed_seconds": round(time.time() - t_start, 1),
    }


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
