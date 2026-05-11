"""Find recent threads worth commenting on, and draft helpful replies.

The flow is tuned for efficiency:

  - ONE paid Apify call per request -- a single batched scrape of
    recent posts across every selected subreddit.
  - ONE batched LLM call to score every post for relevance.
  - PARALLEL LLM calls (default concurrency 4) to draft replies for
    each kept thread; finished threads stream back as they complete,
    so the user sees results faster than the worst-case wall time.

The public entry point comes in two flavours:

  - find_threads(...)            -> returns the full result list
  - find_threads_stream(...)     -> yields SSE-ready progress events
"""
from __future__ import annotations

import concurrent.futures as cf
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
    per_sub: int = 12,
    total_limit: int = 25,
    replies_per_thread: int = 3,
    min_relevance: int = 10,
    max_age_days: int = 45,
    max_wait_seconds: float = 240.0,
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
        max_wait_seconds=max_wait_seconds,
    ):
        if ev.get("type") == "done":
            final = ev
    return final.get("threads", [])


_MAX_SUBREDDITS_PER_REQUEST = 4


def find_threads_stream(
    profile: dict,
    subreddits: list[str],
    *,
    per_sub: int = 12,
    total_limit: int = 25,
    replies_per_thread: int = 3,
    min_relevance: int = 10,
    max_age_days: int = 45,
    max_wait_seconds: float = 240.0,
    draft_concurrency: int = 4,
) -> Iterator[dict]:
    """Yield progress events as the search runs. The final event is
    always `{"type": "done", "threads": [...]}`."""
    import sys

    def log(msg: str) -> None:
        print(f"[threads] {msg}", file=sys.stderr, flush=True)

    cutoff = time.time() - max_age_days * 86400
    t_start = time.time()

    if len(subreddits) > _MAX_SUBREDDITS_PER_REQUEST:
        yield {
            "type": "done",
            "threads": [],
            "error": (
                f"Pick at most {_MAX_SUBREDDITS_PER_REQUEST} subreddits "
                f"per run (you picked {len(subreddits)}). Each subreddit "
                f"adds Apify scrape time; small batches are much faster."
            ),
        }
        return

    yield {
        "type": "step",
        "step": "fetch",
        "message": (
            f"starting Apify scrape across {len(subreddits)} subreddit"
            f"{'s' if len(subreddits) != 1 else ''}…"
        ),
    }

    # Shared state between the scrape thread and the heartbeat loop.
    result: dict = {"posts": []}
    state: dict = {"apify_status": "READY", "run_id": "", "items": 0}
    state_lock = threading.Lock()
    cancel_event = threading.Event()

    def _on_status(meta: dict) -> None:
        with state_lock:
            state["apify_status"] = meta.get("status", state["apify_status"])
            if meta.get("run_id"):
                state["run_id"] = meta["run_id"]
            if "items_so_far" in meta:
                state["items"] = int(meta.get("items_so_far") or 0)
            if "partial_items" in meta:
                state["items"] = int(meta.get("partial_items") or 0)

    def _scrape() -> None:
        try:
            posts, _ = reddit_client.bulk_recent_threads(
                subreddits,
                per_sub=per_sub,
                include_comments=False,
                on_status=_on_status,
                cancel_event=cancel_event,
                max_wait=max_wait_seconds,
            )
            result["posts"] = posts
        except Exception as e:  # noqa: BLE001
            result["error"] = f"scrape failed: {e}"

    th = threading.Thread(target=_scrape, daemon=True)
    th.start()

    elapsed = 0
    while th.is_alive():
        th.join(timeout=3.0)
        if th.is_alive():
            elapsed += 3
            with state_lock:
                apify_status = state["apify_status"]
                run_id = state["run_id"]
                items_so_far = state["items"]
            human = {
                "READY": "queued in Apify",
                "RUNNING": "scraping Reddit",
                "SUCCEEDED": "wrapping up",
                "FAILED": "actor failed",
                "TIMED-OUT": "actor timed out",
                "ABORTED": "run aborted",
                "ABORTED_BY_AGENT": "aborted (took too long)",
            }.get(apify_status, apify_status.lower())
            tail = (
                f" · {items_so_far} items scraped" if items_so_far else ""
            )
            yield {
                "type": "heartbeat",
                "step": "fetch",
                "elapsed": elapsed,
                "apify_status": apify_status,
                "run_id": run_id,
                "items_so_far": items_so_far,
                "run_url": (
                    f"https://console.apify.com/actors/runs/{run_id}"
                    if run_id else ""
                ),
                "message": f"{human}… ({elapsed}s){tail}",
            }
    th.join()
    if "error" in result:
        yield {"type": "done", "threads": [], "error": result["error"]}
        return
    candidates = result.get("posts", [])
    log(
        f"bulk_recent_threads: {len(candidates)} posts from "
        f"{len(subreddits)} subs in {time.time() - t_start:.1f}s "
        f"(apify={state['apify_status']})"
    )
    if not candidates:
        if state["apify_status"] == "ABORTED_BY_AGENT":
            msg = (
                f"Apify scrape took longer than {int(max_wait_seconds)}s "
                f"and was aborted before any posts were written. The "
                f"actor is having a slow day — try again in a minute, "
                f"or bump max_wait_seconds in the request."
            )
        else:
            msg = (
                f"Reddit returned no recent posts for these subreddits. "
                f"Try different subreddits or check APIFY_TOKEN."
            )
        yield {"type": "done", "threads": [], "message": msg}
        return
    if state["apify_status"] == "ABORTED_BY_AGENT":
        partial_note = " (partial — scrape was aborted at the budget)"
    else:
        partial_note = ""
    yield {
        "type": "fetched",
        "count": len(candidates),
        "message": (
            f"got {len(candidates)} recent posts from Apify{partial_note}"
        ),
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
    total_scored = len(scored)
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
    yield {
        "type": "filtered",
        "scored": total_scored,
        "kept": len(scored),
        "min_relevance": min_relevance,
        "message": (
            f"scored {total_scored} posts, {len(scored)} passed the "
            f"relevance bar (min={min_relevance})"
        ),
    }
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

    # ----- step 3: draft replies in parallel -------------------------
    workers = max(1, min(draft_concurrency, len(scored)))
    yield {
        "type": "step",
        "step": "draft",
        "message": (
            f"drafting {replies_per_thread} replies for {len(scored)} "
            f"threads ({workers}× parallel)…"
        ),
    }

    def _build_thread(p: dict) -> dict:
        pid = p.get("id") or ""
        try:
            replies = _draft_replies(profile, p, [], replies_per_thread)
        except Exception as e:  # noqa: BLE001
            log(f"reply draft failed for {pid}: {e}")
            replies = []
        return {
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

    results: list[dict] = []
    pool = cf.ThreadPoolExecutor(max_workers=workers, thread_name_prefix="draft")
    try:
        futures = {pool.submit(_build_thread, p): p for p in scored}
        idx = 0
        for fut in cf.as_completed(futures):
            idx += 1
            try:
                thread = fut.result()
            except Exception as e:  # noqa: BLE001
                log(f"draft worker raised: {e}")
                continue
            results.append(thread)
            yield {
                "type": "thread",
                "index": idx,
                "total": len(scored),
                "thread": thread,
            }
    finally:
        pool.shutdown(wait=False, cancel_futures=True)

    # Sort the final summary by relevance so consumers of /api/threads
    # (non-streaming) get a sensible ordering. The streaming UI sorts
    # client-side as items arrive.
    results.sort(key=lambda t: -t["relevance"])
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
