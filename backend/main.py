"""FastAPI server for the Reddit Sales POC agent."""
from __future__ import annotations

import os
from pathlib import Path

import logging
import traceback

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
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


_logger = logging.getLogger("reddit-sales-poc")


@app.exception_handler(Exception)
async def _last_resort_handler(request: Request, exc: Exception) -> JSONResponse:
    """Catch any exception that slipped past per-route try/except so a
    single failing request can never kill the process. The full
    traceback is still printed to the server terminal."""
    _logger.error("unhandled exception on %s %s", request.method, request.url.path)
    traceback.print_exc()
    return JSONResponse(
        status_code=500,
        content={
            "detail": f"{type(exc).__name__}: {exc}",
            "where": f"{request.method} {request.url.path}",
        },
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
    max_threads: int = 25
    min_relevance: int = 10
    max_wait_seconds: int = Field(240, ge=30, le=600)


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
    # Never raise on fetch -- big sites (Udemy, anything behind
    # Cloudflare) often block plain HTTP clients. The LLM can still
    # profile well-known domains from its own knowledge.
    site = website_mod.fetch_site_text_or_stub(body.website_url)
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

    Hardened against every failure mode I could think of:
      - Catches BaseException (not just Exception) so async-cancel
        errors can't escape and silently kill the stream.
      - Always yields a final `done` event before returning, even on
        error or early exit, so the client never has to guess.
      - Comment-line keep-alives (`:keepalive\n\n`) every event so
        intermediate proxies don't classify the stream as idle.
    """
    if not body.subreddits:
        raise HTTPException(400, "subreddits is empty")

    import json as _json

    def _send(ev: dict) -> str:
        # The leading ':keepalive\n' is a comment line per the SSE
        # spec; clients ignore it but proxies see traffic.
        try:
            return ":keepalive\n" + f"data: {_json.dumps(ev)}\n\n"
        except (TypeError, ValueError):
            safe = {"type": ev.get("type", "unknown"),
                    "error": "non-serializable event"}
            return f"data: {_json.dumps(safe)}\n\n"

    def event_stream():
        sent_done = False
        try:
            for ev in threads_mod.find_threads_stream(
                body.business,
                body.subreddits,
                replies_per_thread=body.replies_per_thread,
                total_limit=body.max_threads,
                min_relevance=body.min_relevance,
                max_wait_seconds=body.max_wait_seconds,
            ):
                if ev.get("type") == "done":
                    sent_done = True
                yield _send(ev)
        except GeneratorExit:
            # Client disconnected. Don't try to yield anything else.
            raise
        except BaseException as exc:  # noqa: BLE001
            _logger.error(
                "threads_stream blew up: %s: %s",
                type(exc).__name__, exc,
            )
            traceback.print_exc()
            err = {
                "type": "done",
                "threads": [],
                "error": f"{type(exc).__name__}: {exc}",
            }
            try:
                yield _send(err)
            except Exception:
                pass
            return
        if not sent_done:
            try:
                yield _send({
                    "type": "done",
                    "threads": [],
                    "error": "stream ended without a done event",
                })
            except Exception:
                pass

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
