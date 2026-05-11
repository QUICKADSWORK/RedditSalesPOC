"""Fetch a website and turn it into a structured business profile."""
from __future__ import annotations

import re
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from . import llm


# Pretend to be a real Chrome on macOS. Many big sites (Udemy, anything
# behind Cloudflare/Akamai) refuse requests that don't look like a
# browser. This isn't trying to evade rate limits -- we just need the
# default homepage HTML.
_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    # httpx decodes gzip/deflate natively; we deliberately omit "br"
    # because brotli requires an optional dependency, and a few sites
    # return brotli-compressed bytes that would then come back garbled.
    "Accept-Encoding": "gzip, deflate",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}


def _normalize_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        raise ValueError("Empty URL")
    if not re.match(r"^https?://", url, re.IGNORECASE):
        url = "https://" + url
    return url


def _url_variants(url: str) -> list[str]:
    """Generate fallback URLs to try if the first one is blocked."""
    parsed = urlparse(url)
    host = parsed.netloc
    variants = [url]
    if not host.startswith("www."):
        variants.append(parsed._replace(netloc="www." + host).geturl())
    elif host.startswith("www."):
        variants.append(parsed._replace(netloc=host[4:]).geturl())
    if parsed.scheme == "https":
        variants.append(parsed._replace(scheme="http").geturl())
    # De-dupe while preserving order.
    seen: set[str] = set()
    out: list[str] = []
    for v in variants:
        if v not in seen:
            out.append(v)
            seen.add(v)
    return out


def fetch_site_text(url: str, *, max_chars: int = 8000) -> dict:
    """Download a page and extract title, meta description, and visible text.

    Tries a few URL variants and uses realistic browser headers so we
    don't get bounced by Cloudflare-style bot checks. Raises the last
    httpx error if every variant fails -- callers can fall back to
    LLM-only profiling in that case (see `build_business_profile`).
    """
    url = _normalize_url(url)
    last_err: Exception | None = None
    html = ""
    final_url = url

    with httpx.Client(
        follow_redirects=True,
        timeout=20.0,
        headers=_BROWSER_HEADERS,
        http2=False,  # keep deps lean; HTTP/1.1 is fine for homepages
    ) as client:
        for candidate in _url_variants(url):
            try:
                r = client.get(candidate)
                if r.status_code in (403, 401, 451):
                    # Forbidden / blocked. Try the next variant.
                    last_err = httpx.HTTPStatusError(
                        f"{r.status_code}", request=r.request, response=r
                    )
                    continue
                r.raise_for_status()
                html = r.text
                final_url = str(r.url)
                last_err = None
                break
            except Exception as e:  # noqa: BLE001
                last_err = e
                continue

    if not html:
        if last_err is not None:
            raise last_err
        raise RuntimeError(f"could not fetch {url}")

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

    domain = urlparse(final_url).netloc

    return {
        "url": final_url,
        "domain": domain,
        "title": title,
        "description": description,
        "text": text,
    }


def fetch_site_text_or_stub(url: str, *, max_chars: int = 8000) -> dict:
    """Same as `fetch_site_text` but never raises. If the fetch is
    blocked (403, network error, etc.) we return a stub with just the
    URL/domain so the LLM can infer the business from its own
    knowledge of the domain (works well for famous sites like
    udemy.com, notion.so, etc.)."""
    try:
        return fetch_site_text(url, max_chars=max_chars)
    except Exception as exc:
        normalized = _normalize_url(url)
        return {
            "url": normalized,
            "domain": urlparse(normalized).netloc,
            "title": "",
            "description": "",
            "text": "",
            "fetch_error": str(exc),
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
    if not site.get("text") and not site.get("title"):
        # We couldn't scrape the page (Cloudflare, anti-bot, 403, etc.).
        # Ask the LLM to profile the site from its own knowledge of the
        # domain. Works well for well-known sites; for obscure ones the
        # LLM will say so and we'll still return *something*.
        user = (
            f"URL: {site['url']}\n"
            f"Domain: {site['domain']}\n\n"
            f"NOTE: We could not fetch this site's HTML "
            f"(reason: {site.get('fetch_error', 'unknown')}). "
            "Profile the business based on what you already know about "
            "this domain. If the domain isn't familiar to you, return "
            "your best guess based on the domain name alone -- the "
            "operator will correct it if you're wrong."
        )
    else:
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
    if site.get("fetch_error"):
        profile["fetch_warning"] = (
            "Could not fetch the website directly; profile was inferred "
            "from the LLM's prior knowledge of this domain. Review it "
            "and edit if anything looks off."
        )
    return profile
