# Reddit Sales Agent (POC)

A small AI agent that turns **a website URL** into:

1. **A business profile** — what you do, who you serve, what pains you
   solve, the keywords you rank for in customers' heads.
2. **Recommended subreddits** — communities where your audience already
   discusses these problems, ranked by how welcoming they are to
   helpful (non-spammy) participation.
3. **Recent threads worth a comment** — actual posts in those
   subreddits, each with **2–4 different draft replies** that read like a
   real human in the space (analyzes the existing top comments so the
   drafts don't repeat what's already been said).
4. **Human-style post drafts** — discussion / story / question / opinion
   posts you can drop into a target subreddit. The prompt is
   deliberately tuned to *not* look like marketing.

The agent suggests, you decide. Always read each subreddit's rules
before posting.

---

## Stack

- **Backend**: Python 3.11+, FastAPI, OpenAI (chat completions),
  PRAW (with a graceful anonymous fallback to reddit.com JSON).
- **Frontend**: a single static HTML/CSS/JS page served by FastAPI.

```
backend/
  main.py            FastAPI app + endpoints
  agent/
    llm.py           OpenAI wrapper (text + JSON modes)
    website.py       Scrape URL -> business profile
    subreddits.py    LLM + Reddit search -> ranked subreddits
    threads.py       Find threads + draft 2-4 replies each
    posts.py         Generate human-style posts
    reddit_client.py PRAW with anon fallback
  requirements.txt
  .env.example
frontend/
  index.html
  styles.css
  app.js
```

---

## Setup

### 1. Get the keys

- **OpenAI** API key: <https://platform.openai.com/api-keys>
- **Reddit** (optional but recommended): create a "script" app at
  <https://www.reddit.com/prefs/apps> and grab `client_id` /
  `client_secret`. Without these the agent falls back to anonymous
  reddit.com JSON, which works for low volume.

### 2. Install

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# edit .env and paste your keys
```

### 3. Run

```bash
# from /backend with the venv active
python main.py
# or, with auto-reload during development:
RELOAD=1 python main.py
```

Open <http://localhost:8000>.

---

## How to use it

1. Paste a website URL (e.g. `https://linear.app`) and hit **Analyze**.
2. Review the inferred business profile and the ranked subreddits.
3. Tick the subreddits you actually want to engage with.
4. Click **Find threads to comment on** — you'll get recent threads
   with 2–4 reply drafts each. Use the *copy* button on any reply.
5. Click **Generate human-style posts** — you'll get post drafts you
   can paste straight into Reddit (after you sanity-check them and
   tweak the voice).

---

## API

All endpoints accept and return JSON.

### `GET /api/health`

Returns whether `OPENAI_API_KEY` and Reddit creds are configured.

### `POST /api/analyze`

```json
{ "website_url": "https://example.com", "max_subreddits": 12 }
```

Returns `{ "business": {...}, "subreddits": [...] }`.

### `POST /api/threads`

```json
{
  "business": { /* the profile from /analyze */ },
  "subreddits": ["sales", "smallbusiness"],
  "replies_per_thread": 3,
  "max_threads": 8,
  "min_relevance": 55
}
```

Returns `{ "threads": [{ title, url, subreddit, relevance, intent,
angle, top_comments_sampled, replies: [{ angle, text, mentions_product }]
}] }`.

### `POST /api/posts`

```json
{
  "business": { /* the profile from /analyze */ },
  "subreddits": ["sales", "smallbusiness"],
  "count": 4
}
```

Returns `{ "posts": [{ subreddit, post_type, title, body,
mentions_product, why_this_works }] }`.

---

## Notes / caveats

- Reddit aggressively rate-limits / blocks anonymous traffic from
  data-center IPs. If you run this on a cloud VM, set
  `REDDIT_CLIENT_ID` / `REDDIT_CLIENT_SECRET`. On a normal home
  connection the anonymous fallback is usually fine. The header in the
  UI will tell you which mode is active.
- This is a **POC**, not a posting bot. It never logs into Reddit and
  never auto-posts. It only reads.
- The reply prompts are tuned to feel human and to *avoid* the usual
  "thanks for sharing! check out our product 🚀" tone. They include at
  most one soft product mention, only when it's actually the best
  answer, and only when the subreddit tolerates self-promotion.
- The post prompts go further: never name-drop in the title, never
  include a CTA or DM line, and forbid corporate buzzwords. The output
  reads as a community member, not a brand.
- The agent does not try to bypass subreddit rules. Always read them
  yourself before commenting or posting.
