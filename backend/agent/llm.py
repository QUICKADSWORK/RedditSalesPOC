"""Thin wrapper around the OpenAI chat-completions API.

All LLM calls in the agent flow through `chat_json` (for structured output)
or `chat_text` (for free-form text). This makes it easy to swap providers
later without touching the agent logic.
"""
from __future__ import annotations

import json
import os
from typing import Any

from openai import OpenAI


_client: OpenAI | None = None


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError(
                "OPENAI_API_KEY is not set. Copy backend/.env.example to "
                "backend/.env and fill it in."
            )
        base_url = os.getenv("OPENAI_BASE_URL") or None
        _client = OpenAI(api_key=api_key, base_url=base_url)
    return _client


def _model() -> str:
    return os.getenv("OPENAI_MODEL", "gpt-4o-mini")


def chat_text(system: str, user: str, *, temperature: float = 0.7) -> str:
    client = _get_client()
    resp = client.chat.completions.create(
        model=_model(),
        temperature=temperature,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )
    return (resp.choices[0].message.content or "").strip()


def chat_json(system: str, user: str, *, temperature: float = 0.4) -> Any:
    """Ask the model for JSON and parse it. The system prompt should already
    instruct the model to reply with valid JSON only."""
    client = _get_client()
    resp = client.chat.completions.create(
        model=_model(),
        temperature=temperature,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )
    raw = resp.choices[0].message.content or "{}"
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # Last-resort recovery: strip code fences and try again.
        cleaned = raw.strip().strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:].strip()
        return json.loads(cleaned)
