"""Fetch a website and turn it into a structured business profile."""
from __future__ import annotations

import re
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from . import llm


_USER_AGENT = (
    "Mozilla/5.0 (compatible; RedditSalesPOC/0.1; +https://example.com/bot)"
)


def _normalize_url(url: str) -> str:
    url = url.strip()
    if not url:
        raise ValueError("Empty URL")
    if not re.match(r"^https?://", url, re.IGNORECASE):
        url = "https://" + url
    return url


def fetch_site_text(url: str, *, max_chars: int = 8000) -> dict:
    """Download a page and extract title, meta description, and visible text."""
    url = _normalize_url(url)
    with httpx.Client(
        follow_redirects=True,
        timeout=20.0,
        headers={"User-Agent": _USER_AGENT},
    ) as client:
        r = client.get(url)
        r.raise_for_status()
        html = r.text

    soup = BeautifulSoup(html, "lxml")

    for tag in soup(["script", "style", "noscript", "svg", "header", "footer", "nav"]):
        tag.decompose()

    title = (soup.title.string.strip() if soup.title and soup.title.string else "")

    description = ""
    md = soup.find("meta", attrs={"name": "description"})
    if md and md.get("content"):
        description = md["content"].strip()
    if not description:
        og = soup.find("meta", attrs={"property": "og:description"})
        if og and og.get("content"):
            description = og["content"].strip()

    text = soup.get_text(separator=" ", strip=True)
    text = re.sub(r"\s+", " ", text)
    if len(text) > max_chars:
        text = text[:max_chars]

    domain = urlparse(url).netloc

    return {
        "url": url,
        "domain": domain,
        "title": title,
        "description": description,
        "text": text,
    }


_PROFILE_SYSTEM = """You are a market researcher.
Given the raw content of a company's website, produce a JSON object that
captures what the business does, who it serves, and which Reddit
communities are likely to discuss the same problems.

Reply with a single JSON object using this exact schema (no extra keys):
{
  "name": string,                 // company / product name
  "one_liner": string,            // <= 140 chars, what it is
  "summary": string,              // 2-3 sentences
  "category": string,             // e.g. "B2B SaaS - sales enablement"
  "target_audience": [string],    // 2-5 audience segments
  "value_props": [string],        // 3-5 short bullets
  "pain_points": [string],        // 3-6 customer pains it solves
  "keywords": [string],           // 8-15 search keywords/phrases
  "competitors_or_alternatives": [string]  // 0-6 names, [] if unknown
}
Do NOT include any text outside the JSON.
"""


def build_business_profile(site: dict) -> dict:
    user = (
        f"URL: {site['url']}\n"
        f"Domain: {site['domain']}\n"
        f"Title: {site['title']}\n"
        f"Meta description: {site['description']}\n\n"
        f"Page text (truncated):\n{site['text']}"
    )
    profile = llm.chat_json(_PROFILE_SYSTEM, user, temperature=0.3)
    profile.setdefault("name", site["domain"])
    profile["source_url"] = site["url"]
    return profile
