"""FastAPI server for the Reddit Sales POC agent."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

# Load .env before importing modules that read env vars at import time.
load_dotenv(Path(__file__).resolve().parent / ".env")

from agent import llm as llm_mod  # noqa: E402
from agent import posts as posts_mod  # noqa: E402
from agent import reddit_client  # noqa: E402
from agent import subreddits as subs_mod  # noqa: E402
from agent import threads as threads_mod  # noqa: E402
from agent import website as website_mod  # noqa: E402


app = FastAPI(title="Reddit Sales POC", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class AnalyzeBody(BaseModel):
    website_url: str = Field(..., description="Public URL of the business")
    max_subreddits: int = 12


class ThreadsBody(BaseModel):
    business: dict
    subreddits: list[str]
    replies_per_thread: int = Field(3, ge=2, le=4)
    max_threads: int = 6
    min_relevance: int = 30
    max_wait_seconds: int = Field(120, ge=30, le=300)


class PostsBody(BaseModel):
    business: dict
    subreddits: list[str]
    count: int = Field(4, ge=1, le=8)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def _real_key(name: str) -> bool:
    val = (os.getenv(name) or "").strip()
    if not val:
        return False
    if val.startswith("sk-...") or val in {"sk-...", "your-key-here", "changeme"}:
        return False
    return True


@app.get("/api/health")
def health() -> dict:
    backend = reddit_client.current_backend()
    provider = llm_mod.current_provider()
    return {
        "ok": True,
        "llm": {
            "provider": provider["name"],
            "model": provider["model"],
            "anthropic_configured": _real_key("ANTHROPIC_API_KEY"),
            "openai_configured": _real_key("OPENAI_API_KEY"),
        },
        "reddit": {
            "backend": backend,
            "apify_configured": _real_key("APIFY_TOKEN"),
            "praw_configured": (
                _real_key("REDDIT_CLIENT_ID") and _real_key("REDDIT_CLIENT_SECRET")
            ),
            "anon_reachable": (
                True if backend != "anon" else reddit_client.anon_reachable()
            ),
        },
    }


@app.post("/api/analyze")
def analyze(body: AnalyzeBody) -> dict:
    try:
        site = website_mod.fetch_site_text(body.website_url)
    except Exception as exc:
        raise HTTPException(400, f"Could not fetch website: {exc}") from exc
    try:
        profile = website_mod.build_business_profile(site)
    except Exception as exc:
        raise HTTPException(500, f"Profile generation failed: {exc}") from exc
    try:
        recs = subs_mod.recommend_subreddits(profile, max_results=body.max_subreddits)
    except Exception as exc:
        raise HTTPException(500, f"Subreddit recommendation failed: {exc}") from exc
    return {"business": profile, "subreddits": recs}


@app.post("/api/threads")
def threads(body: ThreadsBody) -> dict:
    if not body.subreddits:
        raise HTTPException(400, "subreddits is empty")
    try:
        results = threads_mod.find_threads(
            body.business,
            body.subreddits,
            replies_per_thread=body.replies_per_thread,
            total_limit=body.max_threads,
            min_relevance=body.min_relevance,
            max_wait_seconds=body.max_wait_seconds,
        )
    except Exception as exc:
        raise HTTPException(500, f"Thread search failed: {exc}") from exc
    return {"threads": results}


@app.post("/api/threads/stream")
def threads_stream(body: ThreadsBody):
    """Server-Sent Events version of /api/threads.

    The client receives progress events (`step`, `heartbeat`,
    `fetched`, `thread`) as the search runs, and a final `done` event
    with the complete result list. This keeps the connection warm
    during the long Apify scrape so it doesn't get killed by an idle
    timeout somewhere between the browser and the server.
    """
    if not body.subreddits:
        raise HTTPException(400, "subreddits is empty")

    import json as _json

    def event_stream():
        try:
            for ev in threads_mod.find_threads_stream(
                body.business,
                body.subreddits,
                replies_per_thread=body.replies_per_thread,
                total_limit=body.max_threads,
                min_relevance=body.min_relevance,
                max_wait_seconds=body.max_wait_seconds,
            ):
                yield f"data: {_json.dumps(ev)}\n\n"
        except Exception as exc:  # noqa: BLE001
            err = {"type": "done", "threads": [], "error": str(exc)}
            yield f"data: {_json.dumps(err)}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",  # disable nginx-style buffering
            "Connection": "keep-alive",
        },
    )


@app.post("/api/posts")
def posts(body: PostsBody) -> dict:
    if not body.subreddits:
        raise HTTPException(400, "subreddits is empty")
    try:
        results = posts_mod.generate_posts(
            body.business, body.subreddits, count=body.count
        )
    except Exception as exc:
        raise HTTPException(500, f"Post generation failed: {exc}") from exc
    return {"posts": results}


# ---------------------------------------------------------------------------
# Static frontend
# ---------------------------------------------------------------------------


_FRONTEND = Path(__file__).resolve().parent.parent / "frontend"
if _FRONTEND.is_dir():
    app.mount(
        "/static",
        StaticFiles(directory=str(_FRONTEND)),
        name="static",
    )

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(str(_FRONTEND / "index.html"))


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8000")),
        reload=bool(os.getenv("RELOAD")),
    )
