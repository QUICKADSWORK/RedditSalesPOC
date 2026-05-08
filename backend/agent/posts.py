"""Generate human-style, non-promotional posts for target subreddits."""
from __future__ import annotations

from . import llm, reddit_client


_POSTS_SYSTEM = """You write Reddit posts that *do not look like
marketing*. They should look like a real human in the audience sharing a
story, asking a genuine question, or starting a discussion. Rules:

1. Never name-drop the company in the title. The body may include ONE
   subtle, optional mention only when the subreddit clearly tolerates it
   ("we built a small tool for this internally" style) -- otherwise no
   mention at all.
2. No links unless absolutely natural. No call-to-action. No "DM me".
3. Match the subreddit's vibe: casual phrasing, lowercase ok, mild typos
   ok if appropriate. Avoid corporate buzzwords ("leverage", "synergy",
   "game-changer", "revolutionize", "in today's fast-paced world").
4. Vary post types across the batch: e.g. one "question to the
   community", one "story / lesson learned", one "hot take / opinion",
   one "honest comparison or process share".
5. Each post: title 8-14 words, body 90-220 words, paragraphs short.
6. Be specific and concrete (numbers, names of tools, real situations).
   Specifics are what make posts feel human.

Reply with JSON:
{
  "posts": [
    {
      "subreddit": string,            // exact name from the input list
      "post_type": string,            // "question" | "story" | "opinion" | "process_share" | "discussion"
      "title": string,
      "body": string,
      "mentions_product": bool,
      "why_this_works": string        // 1 sentence for the operator
    }
  ]
}
Generate exactly the number of posts requested. Only output JSON.
"""


def generate_posts(
    profile: dict,
    subreddits: list[str],
    *,
    count: int = 4,
) -> list[dict]:
    sub_briefs: list[str] = []
    sub_names: list[str] = []
    for s in subreddits[:6]:
        info = reddit_client.get_subreddit_info(s)
        if not info:
            continue
        sub_names.append(info["name"])
        sub_briefs.append(
            f"- r/{info['name']} ({info.get('subscribers', 0)} subs): "
            f"{info.get('title', '')} -- {(info.get('description') or '')[:200]}"
        )

    if not sub_names:
        return []

    user = (
        f"Business (for context only -- do NOT promote it):\n"
        f"- name: {profile.get('name')}\n"
        f"- one_liner: {profile.get('one_liner')}\n"
        f"- audience: {profile.get('target_audience')}\n"
        f"- pains solved: {profile.get('pain_points')}\n"
        f"- value props: {profile.get('value_props')}\n\n"
        f"Target subreddits:\n" + "\n".join(sub_briefs) + "\n\n"
        f"Generate {count} posts total. Spread them across the subreddits "
        f"(some subreddits may get more than one if they fit best). Use "
        f"the exact subreddit name in the 'subreddit' field."
    )

    try:
        data = llm.chat_json(_POSTS_SYSTEM, user, temperature=0.85)
        raw_posts = data.get("posts") or []
    except Exception:
        raw_posts = []

    out: list[dict] = []
    for p in raw_posts[:count]:
        title = (p.get("title") or "").strip()
        body = (p.get("body") or "").strip()
        sub = (p.get("subreddit") or "").strip().lstrip("/").removeprefix("r/")
        if not title or not body or sub not in sub_names:
            continue
        out.append(
            {
                "subreddit": sub,
                "post_type": p.get("post_type", "discussion"),
                "title": title,
                "body": body,
                "mentions_product": bool(p.get("mentions_product", False)),
                "why_this_works": p.get("why_this_works", ""),
            }
        )
    return out
