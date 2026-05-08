"""FastAPI server for the Reddit Sales POC agent."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

# Load .env before importing modules that read env vars at import time.
load_dotenv(Path(__file__).resolve().parent / ".env")

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
    max_threads: int = 8
    min_relevance: int = 55


class PostsBody(BaseModel):
    business: dict
    subreddits: list[str]
    count: int = Field(4, ge=1, le=8)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/api/health")
def health() -> dict:
    reddit_configured = bool(
        os.getenv("REDDIT_CLIENT_ID") and os.getenv("REDDIT_CLIENT_SECRET")
    )
    return {
        "ok": True,
        "openai_configured": bool(os.getenv("OPENAI_API_KEY")),
        "reddit_configured": reddit_configured,
        "reddit_anon_reachable": (
            True if reddit_configured else reddit_client.anon_reachable()
        ),
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
        )
    except Exception as exc:
        raise HTTPException(500, f"Thread search failed: {exc}") from exc
    return {"threads": results}


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
