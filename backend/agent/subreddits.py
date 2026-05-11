"""Recommend subreddits that match a business profile."""
from __future__ import annotations

from . import llm, reddit_client


_SUGGEST_SYSTEM = """You suggest Reddit communities (subreddits) where a
business's *target audience* hangs out and discusses problems the business
solves. We are NOT looking for places to spam ads; we are looking for
places where helpful, on-topic comments would be welcome.

Reply with a single JSON object:
{
  "search_queries": [string],   // 4-8 short queries usable with Reddit
                                // subreddit search to discover communities
                                // (e.g. "sales prospecting", "saas marketing")
  "candidate_subreddits": [
    {"name": string, "why": string}   // 8-15 subreddit names you are
                                      // confident exist (no leading r/)
  ]
}
Only output JSON. Do not invent niche subreddits you are unsure about.
"""


_RANK_SYSTEM = """You are scoring how relevant each subreddit is for a
business that wants to leave *helpful, non-spammy* comments and the
occasional value-add post.

For each subreddit, output:
- relevance: integer 0-100
- audience_fit: short phrase (who's there)
- comment_strategy: 1 sentence on what kind of comments would land well
- post_allowed: boolean (do you think the sub tolerates self-posts/
  discussion posts that aren't outright ads?)

Reply with JSON:
{ "ranked": [ { "name": str, "relevance": int, "audience_fit": str,
"comment_strategy": str, "post_allowed": bool } ] }
Sort by relevance descending. Only output JSON.
"""


def recommend_subreddits(profile: dict, *, max_results: int = 12) -> list[dict]:
    user = (
        f"Business profile:\n"
        f"- name: {profile.get('name')}\n"
        f"- one_liner: {profile.get('one_liner')}\n"
        f"- summary: {profile.get('summary')}\n"
        f"- category: {profile.get('category')}\n"
        f"- audience: {profile.get('target_audience')}\n"
        f"- value_props: {profile.get('value_props')}\n"
        f"- pain_points: {profile.get('pain_points')}\n"
        f"- keywords: {profile.get('keywords')}\n"
    )
    suggestion = llm.chat_json(_SUGGEST_SYSTEM, user, temperature=0.4)

    candidates: dict[str, dict] = {}
    for c in suggestion.get("candidate_subreddits", []) or []:
        name = (c.get("name") or "").strip().lstrip("/").removeprefix("r/")
        if name and name.lower() not in candidates:
            candidates[name.lower()] = {"name": name, "why": c.get("why", "")}

    for q in (suggestion.get("search_queries") or [])[:6]:
        try:
            for sub in reddit_client.search_subreddits(q, limit=8):
                key = sub["name"].lower()
                if key not in candidates:
                    candidates[key] = {"name": sub["name"], "why": ""}
        except Exception:
            continue

    enriched: list[dict] = []
    for cand in candidates.values():
        info = reddit_client.get_subreddit_info(cand["name"])
        if not info:
            continue
        if info.get("over_18"):
            continue
        # In Apify mode (info["_unverified"] == True) we don't have
        # subscriber counts, so we can't filter on size. We keep every
        # candidate the LLM proposed and let the ranker prune the list.
        if not info.get("_unverified") and (info.get("subscribers") or 0) < 1000:
            continue
        enriched.append({**info, "why": cand.get("why", "")})

    if not enriched:
        return []

    def _line(s: dict) -> str:
        subs = s.get("subscribers") or 0
        subs_part = f"subs={subs}" if subs else "subs=?"
        return (
            f"- r/{s['name']} | {subs_part} | {s.get('title', '')} :: "
            f"{(s.get('description') or s.get('why') or '')[:200]}"
        )

    listing = "\n".join(_line(s) for s in enriched[:30])
    rank_user = (
        f"Business one-liner: {profile.get('one_liner')}\n"
        f"Audience: {profile.get('target_audience')}\n"
        f"Pains solved: {profile.get('pain_points')}\n\n"
        f"Subreddits to score:\n{listing}"
    )
    def _norm(n: str) -> str:
        return (n or "").strip().lstrip("/").removeprefix("r/").lower()

    try:
        ranked = llm.chat_json(_RANK_SYSTEM, rank_user, temperature=0.2)
        ranking = {_norm(r.get("name", "")): r for r in ranked.get("ranked", [])}
    except Exception:
        ranking = {}

    out: list[dict] = []
    for s in enriched:
        meta = ranking.get(_norm(s["name"]), {})
        out.append(
            {
                **s,
                "relevance": int(meta.get("relevance", 50)),
                "audience_fit": meta.get("audience_fit", ""),
                "comment_strategy": meta.get("comment_strategy", ""),
                "post_allowed": bool(meta.get("post_allowed", True)),
            }
        )

    out.sort(key=lambda s: (-s["relevance"], -(s.get("subscribers") or 0)))
    return out[:max_results]
