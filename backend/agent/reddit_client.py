"""Reddit access layer.

Uses PRAW when REDDIT_CLIENT_ID/SECRET are configured, otherwise falls back
to anonymous reddit.com JSON endpoints. The fallback is fine for low-volume
POC use; for production you should always provide credentials.
"""
from __future__ import annotations

import os
import time
from typing import Any

import httpx

try:  # PRAW is optional at runtime
    import praw  # type: ignore
except Exception:  # pragma: no cover
    praw = None  # type: ignore


_USER_AGENT = os.getenv(
    "REDDIT_USER_AGENT", "reddit-sales-poc/0.1 (anonymous)"
)


_reddit = None


def _get_praw():
    global _reddit
    if _reddit is not None:
        return _reddit
    cid = os.getenv("REDDIT_CLIENT_ID")
    csec = os.getenv("REDDIT_CLIENT_SECRET")
    if not (praw and cid and csec):
        return None
    _reddit = praw.Reddit(
        client_id=cid,
        client_secret=csec,
        user_agent=_USER_AGENT,
        check_for_async=False,
    )
    _reddit.read_only = True
    return _reddit


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def search_subreddits(query: str, limit: int = 10) -> list[dict]:
    """Search for subreddits whose name/description matches the query."""
    r = _get_praw()
    if r is not None:
        out: list[dict] = []
        try:
            for sub in r.subreddits.search(query, limit=limit):
                out.append(
                    {
                        "name": sub.display_name,
                        "title": getattr(sub, "title", "") or "",
                        "subscribers": getattr(sub, "subscribers", 0) or 0,
                        "description": (getattr(sub, "public_description", "") or "")[:400],
                        "over_18": bool(getattr(sub, "over18", False)),
                        "url": f"https://www.reddit.com/r/{sub.display_name}/",
                    }
                )
        except Exception:
            pass
        return out
    url = "https://www.reddit.com/subreddits/search.json"
    params = {"q": query, "limit": limit, "include_over_18": "off"}
    try:
        data = _anon_get(url, params=params)
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


def get_subreddit_info(name: str) -> dict | None:
    name = name.lstrip("/").removeprefix("r/")
    r = _get_praw()
    if r is not None:
        try:
            sub = r.subreddit(name)
            return {
                "name": sub.display_name,
                "title": getattr(sub, "title", "") or "",
                "subscribers": getattr(sub, "subscribers", 0) or 0,
                "description": (getattr(sub, "public_description", "") or "")[:400],
                "over_18": bool(getattr(sub, "over18", False)),
                "url": f"https://www.reddit.com/r/{sub.display_name}/",
            }
        except Exception:
            return None
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


def search_threads(
    subreddit: str,
    query: str,
    *,
    limit: int = 8,
    time_filter: str = "month",
    sort: str = "new",
) -> list[dict]:
    """Search for posts inside a subreddit. Returns recent threads first."""
    subreddit = subreddit.lstrip("/").removeprefix("r/")
    r = _get_praw()
    posts: list[dict] = []
    if r is not None:
        try:
            results = r.subreddit(subreddit).search(
                query, sort=sort, time_filter=time_filter, limit=limit
            )
            for p in results:
                posts.append(_praw_post_to_dict(p))
        except Exception:
            pass
        return posts
    # Anonymous
    url = f"https://www.reddit.com/r/{subreddit}/search.json"
    params = {
        "q": query,
        "restrict_sr": "1",
        "sort": sort,
        "t": time_filter,
        "limit": limit,
    }
    try:
        data = _anon_get(url, params=params)
    except Exception:
        return posts
    for child in data.get("data", {}).get("children", []):
        d = child.get("data", {})
        posts.append(_anon_post_to_dict(d))
    return posts


def list_recent_threads(subreddit: str, *, limit: int = 15) -> list[dict]:
    subreddit = subreddit.lstrip("/").removeprefix("r/")
    r = _get_praw()
    posts: list[dict] = []
    if r is not None:
        try:
            for p in r.subreddit(subreddit).new(limit=limit):
                posts.append(_praw_post_to_dict(p))
        except Exception:
            pass
        return posts
    url = f"https://www.reddit.com/r/{subreddit}/new.json"
    try:
        data = _anon_get(url, params={"limit": limit})
    except Exception:
        return posts
    for child in data.get("data", {}).get("children", []):
        d = child.get("data", {})
        posts.append(_anon_post_to_dict(d))
    return posts


def get_top_comments(post_id: str, *, limit: int = 10) -> list[str]:
    r = _get_praw()
    if r is not None:
        try:
            sub = r.submission(id=post_id)
            sub.comment_sort = "top"
            sub.comments.replace_more(limit=0)
            out: list[str] = []
            for c in sub.comments[:limit]:
                body = getattr(c, "body", "") or ""
                if body:
                    out.append(body.strip())
            return out
        except Exception:
            return []
    url = f"https://www.reddit.com/comments/{post_id}.json"
    try:
        data = _anon_get(url, params={"limit": limit, "sort": "top"})
    except Exception:
        return []
    if not isinstance(data, list) or len(data) < 2:
        return []
    out: list[str] = []
    for child in data[1].get("data", {}).get("children", []):
        d = child.get("data", {})
        body = (d.get("body") or "").strip()
        if body:
            out.append(body)
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_LAST_ANON_CALL = 0.0


def anon_reachable() -> bool:
    """Cheap probe: does anonymous reddit.com respond from this host?"""
    try:
        _anon_get("https://www.reddit.com/r/python/about.json")
        return True
    except Exception:
        return False


def _anon_get(url: str, params: dict | None = None) -> Any:
    """Polite anonymous GET against reddit.com with light rate limiting."""
    global _LAST_ANON_CALL
    delta = time.time() - _LAST_ANON_CALL
    if delta < 1.1:  # ~1 req/sec
        time.sleep(1.1 - delta)
    headers = {"User-Agent": _USER_AGENT}
    with httpx.Client(timeout=20.0, headers=headers, follow_redirects=True) as c:
        resp = c.get(url, params=params)
        _LAST_ANON_CALL = time.time()
        resp.raise_for_status()
        return resp.json()


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
