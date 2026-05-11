"""Thin LLM wrapper with multi-provider support.

The agent uses Anthropic Claude when `ANTHROPIC_API_KEY` is set,
otherwise it falls back to OpenAI. Both providers are exposed through
the same two functions:

  - chat_text(system, user)  -> str
  - chat_json(system, user)  -> parsed JSON

Add new providers by extending `_provider()` and adding the two private
implementations.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any


# ---------------------------------------------------------------------------
# Provider selection
# ---------------------------------------------------------------------------


def _provider() -> str:
    if os.getenv("ANTHROPIC_API_KEY"):
        return "anthropic"
    if os.getenv("OPENAI_API_KEY"):
        return "openai"
    return "none"


def current_provider() -> dict:
    p = _provider()
    if p == "anthropic":
        return {
            "name": "anthropic",
            "model": os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6"),
        }
    if p == "openai":
        return {
            "name": "openai",
            "model": os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        }
    return {"name": "none", "model": ""}


def _no_provider() -> RuntimeError:
    return RuntimeError(
        "No LLM provider configured. Set ANTHROPIC_API_KEY (preferred) "
        "or OPENAI_API_KEY in backend/.env."
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def chat_text(system: str, user: str, *, temperature: float = 0.7) -> str:
    p = _provider()
    if p == "anthropic":
        return _anthropic_text(system, user, temperature)
    if p == "openai":
        return _openai_text(system, user, temperature)
    raise _no_provider()


def chat_json(system: str, user: str, *, temperature: float = 0.4) -> Any:
    """Return parsed JSON. The system prompt should request JSON; we
    additionally enforce it on the model side where possible (OpenAI's
    `response_format`) and via prefill on Anthropic."""
    p = _provider()
    if p == "anthropic":
        raw = _anthropic_json(system, user, temperature)
    elif p == "openai":
        raw = _openai_json(system, user, temperature)
    else:
        raise _no_provider()
    return _parse_json_lenient(raw)


# ---------------------------------------------------------------------------
# Anthropic
# ---------------------------------------------------------------------------


_anthropic_client = None


def _clean_key(env_name: str, prefix: str) -> str:
    raw = (os.environ.get(env_name) or "").strip().strip('"').strip("'")
    if not raw:
        raise RuntimeError(f"{env_name} is not set.")
    if not raw.startswith(prefix):
        raise RuntimeError(
            f"{env_name} doesn't look right (starts with {raw[:6]!r}, "
            f"expected '{prefix}...'). Paste only the key itself, not "
            "a URL or anything around it."
        )
    return raw


def _get_anthropic():
    global _anthropic_client
    if _anthropic_client is None:
        from anthropic import Anthropic  # local import keeps cold path cheap

        _anthropic_client = Anthropic(
            api_key=_clean_key("ANTHROPIC_API_KEY", "sk-ant-")
        )
    return _anthropic_client


def _anthropic_model() -> str:
    return os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")


# Per-request timeout for LLM calls. Keeps a hung Anthropic / OpenAI
# request from blocking a worker thread forever. ~60 s is enough for
# any normal completion at our token sizes; if it runs over, that's
# almost always a network or rate-limit issue and we'd rather see it
# now than at 15 minutes in.
_LLM_TIMEOUT = 60.0


def _anthropic_text(system: str, user: str, temperature: float) -> str:
    client = _get_anthropic()
    msg = client.messages.create(
        model=_anthropic_model(),
        max_tokens=2048,
        temperature=temperature,
        system=system,
        messages=[{"role": "user", "content": user}],
        timeout=_LLM_TIMEOUT,
    )
    return _anthropic_join(msg)


def _anthropic_json(system: str, user: str, temperature: float) -> str:
    """Ask the model for JSON. Sonnet 4.6+ doesn't accept assistant
    prefill, so we lean on a strict system prompt and our lenient JSON
    parser extracts the outer object even if there's any chatter."""
    client = _get_anthropic()
    sys_prompt = (
        f"{system}\n\n"
        "CRITICAL: Respond with a single valid JSON object only. Start "
        "your response with { and end with }. No code fences, no "
        "preamble, no commentary, no explanation."
    )
    msg = client.messages.create(
        model=_anthropic_model(),
        max_tokens=4096,
        temperature=temperature,
        system=sys_prompt,
        messages=[{"role": "user", "content": user}],
        timeout=_LLM_TIMEOUT,
    )
    return _anthropic_join(msg)


def _anthropic_join(msg) -> str:
    parts: list[str] = []
    for block in getattr(msg, "content", []) or []:
        text = getattr(block, "text", None)
        if text:
            parts.append(text)
    return "".join(parts).strip()


# ---------------------------------------------------------------------------
# OpenAI
# ---------------------------------------------------------------------------


_openai_client = None


def _get_openai():
    global _openai_client
    if _openai_client is None:
        from openai import OpenAI

        _openai_client = OpenAI(
            api_key=_clean_key("OPENAI_API_KEY", "sk-"),
            base_url=os.getenv("OPENAI_BASE_URL") or None,
        )
    return _openai_client


def _openai_model() -> str:
    return os.getenv("OPENAI_MODEL", "gpt-4o-mini")


def _openai_text(system: str, user: str, temperature: float) -> str:
    client = _get_openai()
    resp = client.chat.completions.create(
        model=_openai_model(),
        temperature=temperature,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        timeout=_LLM_TIMEOUT,
    )
    return (resp.choices[0].message.content or "").strip()


def _openai_json(system: str, user: str, temperature: float) -> str:
    client = _get_openai()
    resp = client.chat.completions.create(
        model=_openai_model(),
        temperature=temperature,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        timeout=_LLM_TIMEOUT,
    )
    return resp.choices[0].message.content or "{}"


# ---------------------------------------------------------------------------
# JSON parsing
# ---------------------------------------------------------------------------


def _parse_json_lenient(raw: str) -> Any:
    raw = (raw or "").strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    # Strip code fences if the model used them despite the prompt.
    fenced = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.IGNORECASE)
    if fenced != raw:
        try:
            return json.loads(fenced)
        except json.JSONDecodeError:
            pass
    # Last resort: extract the outermost {...} or [...].
    for opener, closer in (("{", "}"), ("[", "]")):
        start = raw.find(opener)
        end = raw.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(raw[start : end + 1])
            except json.JSONDecodeError:
                continue
    raise ValueError(f"Could not parse JSON from model response: {raw[:200]!r}")
