"""Reddit access layer with three backends.

Routing order (first match wins):

  1. Apify  (when APIFY_TOKEN is set)        -- works from anywhere, paid
  2. PRAW   (when REDDIT_CLIENT_ID/SECRET    -- official Reddit API, free
            are set)
  3. Anonymous reddit.com JSON               -- works from residential
                                              IPs only; cloud IPs get
                                              403s.

All three backends expose the same surface so callers don't need to
know which one is active:

  - search_subreddits(query, limit)       -> [info dict]
  - get_subreddit_info(name)              -> info dict | None
  - bulk_recent_threads(subs, per_sub)    -> [post dict]
  - bulk_post_comments(posts, max_per)    -> {post_id: [comment str]}
  - get_top_comments(post_id, limit)      -> [str]
  - list_recent_threads(sub, limit)       -> [post dict]   (legacy)
  - search_threads(sub, q, limit, ...)    -> [post dict]   (legacy)
"""
from __future__ import annotations

import os
import time
from datetime import datetime
from typing import Any

import httpx

try:
    import praw  # type: ignore
except Exception:  # pragma: no cover
    praw = None  # type: ignore


_USER_AGENT = os.getenv(
    "REDDIT_USER_AGENT", "reddit-sales-poc/0.1 (anonymous)"
)


# ---------------------------------------------------------------------------
# Backend selection
# ---------------------------------------------------------------------------


def _backend() -> str:
    if os.getenv("APIFY_TOKEN"):
        return "apify"
    if praw and os.getenv("REDDIT_CLIENT_ID") and os.getenv("REDDIT_CLIENT_SECRET"):
        return "praw"
    return "anon"


def current_backend() -> str:
    return _backend()


def anon_reachable() -> bool:
    """Cheap probe: does anonymous reddit.com respond from this host?"""
    try:
        _anon_get("https://www.reddit.com/r/python/about.json")
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


def search_subreddits(query: str, limit: int = 10) -> list[dict]:
    b = _backend()
    if b == "praw":
        return _praw_search_subreddits(query, limit)
    if b == "anon":
        return _anon_search_subreddits(query, limit)
    # Apify: this actor doesn't expose a clean subreddit-search API.
    # We return [] and let the LLM proposals drive the candidate list.
    return []


def get_subreddit_info(name: str) -> dict | None:
    name = _clean_sub(name)
    b = _backend()
    if b == "praw":
        return _praw_get_sub(name)
    if b == "anon":
        return _anon_get_sub(name)
    # Apify mode: skip verification to save credits.
    return {
        "name": name,
        "title": "",
        "subscribers": 0,
        "description": "",
        "over_18": False,
        "url": f"https://www.reddit.com/r/{name}/",
        "_unverified": True,
    }


def bulk_recent_threads(
    subreddits: list[str],
    per_sub: int = 12,
    *,
    include_comments: bool = False,
    comments_per_post: int = 6,
    on_status=None,
    cancel_event=None,
    max_wait: float = 120.0,
) -> tuple[list[dict], dict[str, list[str]]]:
    """Return (posts, comments_by_post_id).

    `on_status({"status": str, "run_id": str})` is called with the
    actor's real status as it changes. `cancel_event` is a
    threading.Event; if set, we'll abort the Apify run and return
    early. `max_wait` is how long we'll wait for the actor before
    giving up and aborting it.
    """
    subs = [_clean_sub(s) for s in subreddits if s]
    if not subs:
        return [], {}
    b = _backend()
    if b == "apify":
        return _apify_recent(
            subs,
            per_sub,
            include_comments=include_comments,
            comments_per_post=comments_per_post,
            on_status=on_status,
            cancel_event=cancel_event,
            max_wait=max_wait,
        )
    posts: list[dict] = []
    for s in subs:
        posts.extend(list_recent_threads(s, limit=per_sub))
    return posts, {}


def bulk_post_comments(
    posts: list[dict], *, max_per_post: int = 8
) -> dict[str, list[str]]:
    """Returns {post_id: [comment_body, ...]} for each given post."""
    if not posts:
        return {}
    b = _backend()
    if b == "apify":
        # The Apify actor doesn't reliably scrape direct post URLs; for
        # the agent flow we always have comments piggy-backed on the
        # subreddit scrape (see bulk_recent_threads), so callers should
        # not need this path in Apify mode. Return empty as a no-op.
        return {}
    out: dict[str, list[str]] = {}
    for p in posts:
        pid = p.get("id") or ""
        if pid:
            out[pid] = get_top_comments(pid, limit=max_per_post)
    return out


def get_top_comments(post_id: str, *, limit: int = 10) -> list[str]:
    b = _backend()
    if b == "praw":
        return _praw_comments(post_id, limit)
    if b == "anon":
        return _anon_comments(post_id, limit)
    # Apify: callers should use bulk_post_comments for efficiency, but
    # support the single-post path too.
    return _apify_comments([{"id": post_id, "url": f"https://www.reddit.com/comments/{post_id}/"}], limit).get(
        post_id, []
    )


def list_recent_threads(subreddit: str, *, limit: int = 15) -> list[dict]:
    b = _backend()
    sub = _clean_sub(subreddit)
    if b == "apify":
        return _apify_recent([sub], limit)
    if b == "praw":
        return _praw_recent(sub, limit)
    return _anon_recent(sub, limit)


def search_threads(
    subreddit: str,
    query: str,
    *,
    limit: int = 8,
    time_filter: str = "month",
    sort: str = "new",
) -> list[dict]:
    b = _backend()
    sub = _clean_sub(subreddit)
    if b == "praw":
        return _praw_search(sub, query, limit, sort, time_filter)
    if b == "anon":
        return _anon_search(sub, query, limit, sort, time_filter)
    # Apify: this actor's per-subreddit search is awkward; for the agent
    # flow we rely on bulk_recent_threads + LLM relevance scoring, so
    # return [] here.
    return []


# ===========================================================================
# Apify backend
# ===========================================================================


_APIFY_BASE = "https://api.apify.com/v2"


def _apify_actor() -> str:
    return os.getenv("APIFY_ACTOR_ID", "trudax~reddit-scraper-lite")


def _apify_token() -> str:
    return os.environ["APIFY_TOKEN"]


def _apify_run(
    payload: dict,
    *,
    max_wait: float = 120.0,
    poll_interval: float = 3.0,
    on_status: "callable | None" = None,
    cancel_event: "threading.Event | None" = None,
) -> list[dict]:
    """Kick off an actor run, poll until it finishes, return dataset items.

    We deliberately avoid `run-sync-get-dataset-items` because Apify
    hard-caps that endpoint at 300 s. The async pattern (POST /runs ->
    poll /actor-runs/ID -> GET /datasets/ID/items) has no such limit.

    Important: we *abort* the Apify run if we hit max_wait or if the
    caller signals via `cancel_event`, so the user isn't charged for a
    run we no longer care about.

    `on_status(state)` is called whenever we learn something new about
    the run (READY -> RUNNING -> ...), useful for surfacing real
    progress in a UI.
    """
    import threading as _threading

    token = _apify_token()
    actor = _apify_actor()
    headers = {"Content-Type": "application/json"}

    def _cancelled() -> bool:
        return bool(cancel_event and cancel_event.is_set())

    with httpx.Client(timeout=30.0) as c:
        # 1. Start the run.
        start = c.post(
            f"{_APIFY_BASE}/acts/{actor}/runs?token={token}",
            json=payload,
            headers=headers,
        )
        start.raise_for_status()
        run = start.json().get("data", {})
        run_id = run.get("id")
        dataset_id = run.get("defaultDatasetId")
        if not run_id or not dataset_id:
            return []
        if on_status:
            try:
                on_status({"status": run.get("status", "READY"), "run_id": run_id})
            except Exception:
                pass

        # 2. Poll until terminal status, our timeout, or cancellation.
        # We also peek at the dataset every few polls so the caller
        # can surface 'X items scraped so far' to the UI.
        deadline = time.time() + max_wait
        status = run.get("status", "READY")
        terminal = {"SUCCEEDED", "FAILED", "TIMED-OUT", "ABORTED"}
        timed_out = False
        last_item_count = 0
        peek_counter = 0
        while status not in terminal:
            if _cancelled():
                break
            if time.time() > deadline:
                timed_out = True
                break
            time.sleep(poll_interval)
            try:
                s = c.get(f"{_APIFY_BASE}/actor-runs/{run_id}?token={token}")
                s.raise_for_status()
                new_status = (s.json().get("data") or {}).get("status", status)
            except Exception:
                continue
            peek_counter += 1
            new_count = last_item_count
            # Peek dataset every ~9 s (every 3 polls at 3 s) to limit
            # extra API hits.
            if peek_counter % 3 == 0:
                try:
                    head = c.get(
                        f"{_APIFY_BASE}/datasets/{dataset_id}/items"
                        f"?token={token}&format=json&limit=0&clean=true"
                    )
                    # The X-Total-Count response header is the cheapest
                    # source of dataset size without fetching items.
                    new_count = int(
                        head.headers.get("X-Total-Count") or last_item_count
                    )
                except Exception:
                    pass
            if (new_status != status or new_count != last_item_count) and on_status:
                try:
                    on_status(
                        {
                            "status": new_status,
                            "run_id": run_id,
                            "items_so_far": new_count,
                        }
                    )
                except Exception:
                    pass
            status = new_status
            last_item_count = new_count

        items_url = (
            f"{_APIFY_BASE}/datasets/{dataset_id}/items"
            f"?token={token}&format=json"
        )

        def _fetch_dataset() -> list:
            try:
                resp = c.get(items_url)
                resp.raise_for_status()
                parsed = resp.json()
                return parsed if isinstance(parsed, list) else []
            except Exception:
                return []

        # 3. If we gave up before the actor finished, FIRST grab any
        # items the actor already wrote to the dataset (Apify streams
        # items into the dataset as it scrapes them, so a partial run
        # is still useful), THEN abort the run so the user isn't
        # billed for the rest.
        if timed_out or (_cancelled() and status not in terminal):
            partial = _fetch_dataset()
            try:
                c.post(
                    f"{_APIFY_BASE}/actor-runs/{run_id}/abort?token={token}"
                )
            except Exception:
                pass
            if on_status:
                try:
                    on_status(
                        {
                            "status": "ABORTED_BY_AGENT",
                            "run_id": run_id,
                            "partial_items": len(partial),
                        }
                    )
                except Exception:
                    pass
            return partial

        # 4. Run reached a terminal state. The dataset can lag the run
        # status by a few seconds, so we poll briefly for the flush.
        data: list = []
        flush_deadline = time.time() + 20.0
        while time.time() < flush_deadline and not _cancelled():
            data = _fetch_dataset()
            if data or status != "SUCCEEDED":
                break
            time.sleep(1.5)
        return data


# Back-compat alias for any older callers; internally always async.
_apify_post = _apify_run


def _apify_run_with_retry(
    payload: dict,
    *,
    retries: int = 1,
    max_wait: float = 120.0,
    on_status=None,
    cancel_event=None,
) -> list[dict]:
    """Run the actor; retry once if the dataset comes back empty
    despite a clean run. The actor is occasionally flaky and returns 0
    items for the same input on one attempt and the expected items on
    the next."""
    for _ in range(retries + 1):
        if cancel_event and cancel_event.is_set():
            return []
        items = _apify_run(
            payload,
            max_wait=max_wait,
            on_status=on_status,
            cancel_event=cancel_event,
        )
        if items:
            return items
    return []


def _apify_recent(
    subs: list[str],
    per_sub: int,
    *,
    include_comments: bool = False,
    comments_per_post: int = 6,
    on_status=None,
    cancel_event=None,
    max_wait: float = 120.0,
) -> tuple[list[dict], dict[str, list[str]]]:
    """Scrape recent posts (and optionally their top comments) across a
    set of subreddits in a single Apify run. Returns (posts, comments).

    When include_comments is True we ask the actor to return comments
    inline, which avoids a second paid call. The maxItems budget is
    sized to accommodate posts + (per_sub * comments_per_post) per
    subreddit. The actor decides on its own how many comments it
    returns per post within that budget.
    """
    start_urls = [
        {"url": f"https://www.reddit.com/r/{s}/new/"} for s in subs
    ]
    if include_comments:
        max_items = max(
            len(subs) * (per_sub + per_sub * comments_per_post), 10
        )
    else:
        max_items = max(per_sub * len(subs), 5)
    payload = {
        "startUrls": start_urls,
        "maxItems": max_items,
        "skipComments": not include_comments,
        "skipUserPosts": True,
        "skipCommunity": True,
    }
    # No retry from this path: the caller's max_wait is a *total*
    # budget, so retrying would double it. If the actor occasionally
    # returns 0 items, the user can just click "find threads" again.
    items = _apify_run(
        payload,
        max_wait=max_wait,
        on_status=on_status,
        cancel_event=cancel_event,
    )
    posts: list[dict] = []
    comments_by_post: dict[str, list[str]] = {}
    for it in items:
        dtype = it.get("dataType")
        if dtype == "post":
            posts.append(_apify_to_post(it))
        elif dtype == "comment":
            pid = (it.get("postId") or "").replace("t3_", "")
            body = (it.get("body") or "").strip()
            if pid and body:
                comments_by_post.setdefault(pid, []).append(body)
    return posts, comments_by_post


def _apify_to_post(it: dict) -> dict:
    pid = (it.get("id") or "").replace("t3_", "") or it.get("parsedId", "")
    created_iso = it.get("createdAt") or ""
    try:
        created_ts = datetime.fromisoformat(
            created_iso.replace("Z", "+00:00")
        ).timestamp() if created_iso else 0.0
    except Exception:
        created_ts = 0.0
    return {
        "id": pid,
        "title": it.get("title") or "",
        "selftext": (it.get("body") or "")[:2000],
        "subreddit": it.get("parsedCommunityName")
        or (it.get("communityName") or "").lstrip("r/"),
        "author": it.get("username") or "",
        "score": int(it.get("upVotes") or 0),
        "num_comments": int(it.get("numberOfComments") or 0),
        "created_utc": created_ts,
        "url": it.get("url") or it.get("link") or "",
        "is_self": True,
        "over_18": bool(it.get("over18", False)),
        "link_flair_text": it.get("flair") or "",
    }


# ===========================================================================
# PRAW backend
# ===========================================================================


_praw_client = None


def _get_praw():
    global _praw_client
    if _praw_client is not None:
        return _praw_client
    _praw_client = praw.Reddit(
        client_id=os.environ["REDDIT_CLIENT_ID"],
        client_secret=os.environ["REDDIT_CLIENT_SECRET"],
        user_agent=_USER_AGENT,
        check_for_async=False,
    )
    _praw_client.read_only = True
    return _praw_client


def _praw_search_subreddits(query: str, limit: int) -> list[dict]:
    r = _get_praw()
    out: list[dict] = []
    try:
        for sub in r.subreddits.search(query, limit=limit):
            out.append(_praw_sub_to_dict(sub))
    except Exception:
        pass
    return out


def _praw_get_sub(name: str) -> dict | None:
    try:
        return _praw_sub_to_dict(_get_praw().subreddit(name))
    except Exception:
        return None


def _praw_recent(sub: str, limit: int) -> list[dict]:
    posts: list[dict] = []
    try:
        for p in _get_praw().subreddit(sub).new(limit=limit):
            posts.append(_praw_post_to_dict(p))
    except Exception:
        pass
    return posts


def _praw_search(sub: str, q: str, limit: int, sort: str, t: str) -> list[dict]:
    posts: list[dict] = []
    try:
        for p in _get_praw().subreddit(sub).search(
            q, sort=sort, time_filter=t, limit=limit
        ):
            posts.append(_praw_post_to_dict(p))
    except Exception:
        pass
    return posts


def _praw_comments(post_id: str, limit: int) -> list[str]:
    try:
        s = _get_praw().submission(id=post_id)
        s.comment_sort = "top"
        s.comments.replace_more(limit=0)
        return [
            (c.body or "").strip()
            for c in s.comments[:limit]
            if getattr(c, "body", "")
        ]
    except Exception:
        return []


def _praw_sub_to_dict(sub) -> dict:
    return {
        "name": sub.display_name,
        "title": getattr(sub, "title", "") or "",
        "subscribers": getattr(sub, "subscribers", 0) or 0,
        "description": (getattr(sub, "public_description", "") or "")[:400],
        "over_18": bool(getattr(sub, "over18", False)),
        "url": f"https://www.reddit.com/r/{sub.display_name}/",
    }


def _praw_post_to_dict(p) -> dict:
    return {
        "id": p.id,
        "title": p.title or "",
        "selftext": (getattr(p, "selftext", "") or "")[:2000],
        "subreddit": str(p.subreddit),
        "author": str(getattr(p, "author", "") or ""),
        "score": int(getattr(p, "score", 0) or 0),
        "num_comments": int(getattr(p, "num_comments", 0) or 0),
        "created_utc": float(getattr(p, "created_utc", 0) or 0),
        "url": f"https://www.reddit.com{p.permalink}",
        "is_self": bool(getattr(p, "is_self", True)),
        "over_18": bool(getattr(p, "over_18", False)),
        "link_flair_text": getattr(p, "link_flair_text", "") or "",
    }


# ===========================================================================
# Anonymous reddit.com backend
# ===========================================================================


def _anon_search_subreddits(query: str, limit: int) -> list[dict]:
    try:
        data = _anon_get(
            "https://www.reddit.com/subreddits/search.json",
            params={"q": query, "limit": limit, "include_over_18": "off"},
        )
    except Exception:
        return []
    out = []
    for child in data.get("data", {}).get("children", []):
        d = child.get("data", {})
        out.append(
            {
                "name": d.get("display_name", ""),
                "title": d.get("title", "") or "",
                "subscribers": d.get("subscribers", 0) or 0,
                "description": (d.get("public_description", "") or "")[:400],
                "over_18": bool(d.get("over18", False)),
                "url": f"https://www.reddit.com{d.get('url', '')}",
            }
        )
    return out


def _anon_get_sub(name: str) -> dict | None:
    try:
        data = _anon_get(f"https://www.reddit.com/r/{name}/about.json")
    except Exception:
        return None
    d = data.get("data") or {}
    if not d:
        return None
    return {
        "name": d.get("display_name", name),
        "title": d.get("title", "") or "",
        "subscribers": d.get("subscribers", 0) or 0,
        "description": (d.get("public_description", "") or "")[:400],
        "over_18": bool(d.get("over18", False)),
        "url": f"https://www.reddit.com/r/{d.get('display_name', name)}/",
    }


def _anon_recent(sub: str, limit: int) -> list[dict]:
    posts: list[dict] = []
    try:
        data = _anon_get(
            f"https://www.reddit.com/r/{sub}/new.json", params={"limit": limit}
        )
    except Exception:
        return posts
    for child in data.get("data", {}).get("children", []):
        posts.append(_anon_post_to_dict(child.get("data", {})))
    return posts


def _anon_search(sub: str, q: str, limit: int, sort: str, t: str) -> list[dict]:
    posts: list[dict] = []
    try:
        data = _anon_get(
            f"https://www.reddit.com/r/{sub}/search.json",
            params={
                "q": q, "restrict_sr": "1", "sort": sort, "t": t, "limit": limit,
            },
        )
    except Exception:
        return posts
    for child in data.get("data", {}).get("children", []):
        posts.append(_anon_post_to_dict(child.get("data", {})))
    return posts


def _anon_comments(post_id: str, limit: int) -> list[str]:
    try:
        data = _anon_get(
            f"https://www.reddit.com/comments/{post_id}.json",
            params={"limit": limit, "sort": "top"},
        )
    except Exception:
        return []
    if not isinstance(data, list) or len(data) < 2:
        return []
    out: list[str] = []
    for child in data[1].get("data", {}).get("children", []):
        body = (child.get("data", {}).get("body") or "").strip()
        if body:
            out.append(body)
        if len(out) >= limit:
            break
    return out


def _anon_post_to_dict(d: dict) -> dict:
    return {
        "id": d.get("id", ""),
        "title": d.get("title", "") or "",
        "selftext": (d.get("selftext", "") or "")[:2000],
        "subreddit": d.get("subreddit", ""),
        "author": d.get("author", "") or "",
        "score": int(d.get("score", 0) or 0),
        "num_comments": int(d.get("num_comments", 0) or 0),
        "created_utc": float(d.get("created_utc", 0) or 0),
        "url": f"https://www.reddit.com{d.get('permalink', '')}",
        "is_self": bool(d.get("is_self", True)),
        "over_18": bool(d.get("over_18", False)),
        "link_flair_text": d.get("link_flair_text", "") or "",
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _clean_sub(name: str) -> str:
    return (name or "").strip().lstrip("/").removeprefix("r/")


_LAST_ANON_CALL = 0.0


def _anon_get(url: str, params: dict | None = None) -> Any:
    global _LAST_ANON_CALL
    delta = time.time() - _LAST_ANON_CALL
    if delta < 1.1:
        time.sleep(1.1 - delta)
    headers = {"User-Agent": _USER_AGENT}
    with httpx.Client(timeout=20.0, headers=headers, follow_redirects=True) as c:
        resp = c.get(url, params=params)
        _LAST_ANON_CALL = time.time()
        resp.raise_for_status()
        return resp.json()
