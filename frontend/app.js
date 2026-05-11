"use strict";

const $ = (sel) => document.querySelector(sel);

const state = {
  business: null,
  subreddits: [],
  selected: new Set(),
};

// ---------------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------------

(async function init() {
  try {
    const r = await fetch("/api/health");
    const data = await r.json();
    const el = $("#health");
    const llm = data.llm || {};
    const rd = data.reddit || {};
    if (!llm.anthropic_configured && !llm.openai_configured) {
      el.classList.add("warn");
      el.textContent = "missing ANTHROPIC_API_KEY (or OPENAI_API_KEY)";
    } else if (rd.backend === "apify") {
      el.classList.add("ok");
      el.textContent = `${llm.provider} • apify`;
    } else if (rd.backend === "praw") {
      el.classList.add("ok");
      el.textContent = `${llm.provider} • reddit api`;
    } else if (rd.anon_reachable) {
      el.classList.add("ok");
      el.textContent = `${llm.provider} • anonymous reddit`;
    } else {
      el.classList.add("warn");
      el.textContent = `${llm.provider} • reddit blocked here — set APIFY_TOKEN`;
    }
  } catch (_e) {
    $("#health").textContent = "offline";
  }
})();

// ---------------------------------------------------------------------------
// Step 1: Analyze
// ---------------------------------------------------------------------------

$("#analyze-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const url = $("#website").value.trim();
  if (!url) return;

  const btn = $("#analyze-btn");
  const status = $("#analyze-status");
  btn.disabled = true;
  status.className = "status";
  status.innerHTML = '<span class="dots">analyzing your site</span>';

  try {
    const data = await postJSON("/api/analyze", { website_url: url });
    state.business = data.business;
    state.subreddits = data.subreddits || [];
    state.selected = new Set(
      state.subreddits.slice(0, 5).map((s) => s.name),
    );
    renderBusiness(state.business);
    renderSubs(state.subreddits);
    status.classList.add("ok");
    status.textContent = `Found ${state.subreddits.length} candidate subreddits.`;
    updateActionButtons();
  } catch (err) {
    status.classList.add("error");
    status.textContent = err.message || "Failed.";
  } finally {
    btn.disabled = false;
  }
});

function renderBusiness(b) {
  const section = $("#business-section");
  const body = $("#business-body");
  body.innerHTML = "";

  const grid = el("div", "profile-grid");

  grid.appendChild(field("Name", b.name || "—"));
  grid.appendChild(field("Category", b.category || "—"));
  grid.appendChild(field("One-liner", b.one_liner || "—", true));
  grid.appendChild(field("Summary", b.summary || "—", true));
  grid.appendChild(chips("Audience", b.target_audience));
  grid.appendChild(chips("Pain points", b.pain_points));
  grid.appendChild(chips("Value props", b.value_props));
  grid.appendChild(chips("Keywords", b.keywords));

  body.appendChild(grid);
  section.classList.remove("hidden");
}

function field(label, value, full = false) {
  const wrap = el("div", full ? "full" : "");
  wrap.appendChild(el("div", "label", label));
  const v = el("div", "", value);
  wrap.appendChild(v);
  return wrap;
}

function chips(label, items) {
  const wrap = el("div", "full");
  wrap.appendChild(el("div", "label", label));
  const c = el("div", "chips");
  (items || []).forEach((t) => c.appendChild(el("span", "chip", String(t))));
  if (!items || !items.length) c.appendChild(el("span", "muted small", "—"));
  wrap.appendChild(c);
  return wrap;
}

// ---------------------------------------------------------------------------
// Step 2: Subreddits
// ---------------------------------------------------------------------------

function renderSubs(subs) {
  const section = $("#subs-section");
  const body = $("#subs-body");
  body.innerHTML = "";

  if (!subs.length) {
    body.appendChild(el("p", "muted", "No subreddits found. Try another URL."));
    section.classList.remove("hidden");
    return;
  }

  subs.forEach((s) => {
    const card = el("label", "sub");
    card.dataset.name = s.name;

    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = state.selected.has(s.name);
    cb.addEventListener("change", () => {
      if (cb.checked) state.selected.add(s.name);
      else state.selected.delete(s.name);
      card.classList.toggle("selected", cb.checked);
      updateActionButtons();
    });
    card.appendChild(cb);
    if (cb.checked) card.classList.add("selected");

    const name = el("div", "name");
    name.appendChild(el("span", "", `r/${s.name}`));
    const rel = el(
      "span",
      `relevance ${s.relevance >= 75 ? "high" : s.relevance >= 50 ? "mid" : ""}`,
      `${s.relevance}/100`,
    );
    name.appendChild(rel);
    card.appendChild(name);

    const subsPart = s.subscribers
      ? `${formatNumber(s.subscribers)} members · `
      : "";
    card.appendChild(
      el(
        "div",
        "meta",
        `${subsPart}${s.audience_fit || s.title || ""}`,
      ),
    );
    if (s.description) card.appendChild(el("div", "desc", s.description));
    if (s.comment_strategy)
      card.appendChild(el("div", "strategy", `→ ${s.comment_strategy}`));

    body.appendChild(card);
  });

  section.classList.remove("hidden");
}

function updateActionButtons() {
  const has = state.business && state.selected.size > 0;
  $("#threads-btn").disabled = !has;
  $("#posts-btn").disabled = !has;
}

// ---------------------------------------------------------------------------
// Step 3a: Threads
// ---------------------------------------------------------------------------

$("#threads-btn").addEventListener("click", async () => {
  const status = $("#action-status");
  const btn = $("#threads-btn");
  btn.disabled = true;
  status.className = "status";
  status.innerHTML =
    '<span class="dots">scanning recent threads &amp; drafting replies</span>';

  try {
    const data = await postJSON("/api/threads", {
      business: state.business,
      subreddits: [...state.selected],
      replies_per_thread: 3,
      max_threads: 8,
    });
    renderThreads(data.threads || []);
    status.classList.add("ok");
    status.textContent = `Found ${data.threads.length} threads.`;
  } catch (err) {
    status.classList.add("error");
    status.textContent = err.message || "Failed.";
  } finally {
    updateActionButtons();
  }
});

function renderThreads(threads) {
  const section = $("#threads-section");
  const body = $("#threads-body");
  body.innerHTML = "";

  if (!threads.length) {
    body.appendChild(
      el(
        "p",
        "muted",
        "No good thread fits right now. Try selecting different subreddits or lowering the relevance bar.",
      ),
    );
    section.classList.remove("hidden");
    return;
  }

  threads.forEach((t) => {
    const card = el("div", "thread");

    const head = el("div", "thread-head");
    const title = el("div", "thread-title");
    const link = document.createElement("a");
    link.href = t.url;
    link.target = "_blank";
    link.rel = "noopener";
    link.textContent = t.title;
    title.appendChild(link);
    head.appendChild(title);
    head.appendChild(
      el(
        "span",
        `relevance ${t.relevance >= 75 ? "high" : t.relevance >= 50 ? "mid" : ""}`,
        `${t.relevance}/100 · ${t.intent}`,
      ),
    );
    card.appendChild(head);

    card.appendChild(
      el(
        "div",
        "thread-meta",
        `r/${t.subreddit} · ${t.num_comments} comments · ${
          formatAge(t.created_utc)
        }`,
      ),
    );

    if (t.angle) card.appendChild(el("div", "thread-angle", t.angle));
    if (t.selftext_preview)
      card.appendChild(el("div", "thread-preview", t.selftext_preview));

    if (t.replies && t.replies.length) {
      const wrap = el("div", "replies");
      t.replies.forEach((r) => {
        const rcard = el("div", "reply");
        const angle = el("div", "angle");
        angle.appendChild(el("span", "", r.angle || "draft"));
        const copy = el("button", "copy-btn", "copy");
        copy.addEventListener("click", (e) => {
          e.preventDefault();
          navigator.clipboard.writeText(r.text).then(() => {
            copy.textContent = "copied";
            setTimeout(() => (copy.textContent = "copy"), 1200);
          });
        });
        angle.appendChild(copy);
        rcard.appendChild(angle);
        rcard.appendChild(el("div", "text", r.text));
        wrap.appendChild(rcard);
      });
      card.appendChild(wrap);
    }

    body.appendChild(card);
  });

  section.classList.remove("hidden");
  section.scrollIntoView({ behavior: "smooth", block: "start" });
}

// ---------------------------------------------------------------------------
// Step 3b: Posts
// ---------------------------------------------------------------------------

$("#posts-btn").addEventListener("click", async () => {
  const status = $("#action-status");
  status.className = "status";
  status.innerHTML = '<span class="dots">drafting posts</span>';

  try {
    const data = await postJSON("/api/posts", {
      business: state.business,
      subreddits: [...state.selected],
      count: 4,
    });
    renderPosts(data.posts || []);
    status.classList.add("ok");
    status.textContent = `Drafted ${data.posts.length} posts.`;
  } catch (err) {
    status.classList.add("error");
    status.textContent = err.message || "Failed.";
  }
});

function renderPosts(posts) {
  const section = $("#posts-section");
  const body = $("#posts-body");
  body.innerHTML = "";

  if (!posts.length) {
    body.appendChild(el("p", "muted", "No posts generated."));
    section.classList.remove("hidden");
    return;
  }

  posts.forEach((p) => {
    const card = el("div", "post");
    const head = el("div", "post-head");
    head.appendChild(el("span", "post-sub", `r/${p.subreddit}`));
    head.appendChild(el("span", "post-type", p.post_type || "post"));
    card.appendChild(head);
    card.appendChild(el("div", "post-title", p.title));
    card.appendChild(el("div", "post-body", p.body));

    const actions = el("div", "post-meta");
    const copyAll = el(
      "button",
      "copy-btn",
      "copy title + body",
    );
    copyAll.addEventListener("click", () => {
      navigator.clipboard.writeText(`${p.title}\n\n${p.body}`).then(() => {
        copyAll.textContent = "copied";
        setTimeout(() => (copyAll.textContent = "copy title + body"), 1200);
      });
    });
    actions.appendChild(copyAll);
    if (p.why_this_works) {
      actions.appendChild(
        el("span", "muted", `   why this works: ${p.why_this_works}`),
      );
    }
    if (p.mentions_product) {
      actions.appendChild(el("span", "muted", "   · mentions product"));
    }
    card.appendChild(actions);
    body.appendChild(card);
  });

  section.classList.remove("hidden");
  section.scrollIntoView({ behavior: "smooth", block: "start" });
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

async function postJSON(path, body) {
  const r = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!r.ok) {
    let msg = `${r.status}`;
    try {
      const j = await r.json();
      if (j.detail) msg = j.detail;
    } catch (_e) {}
    throw new Error(msg);
  }
  return r.json();
}

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

function formatNumber(n) {
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(1) + "M";
  if (n >= 1_000) return (n / 1_000).toFixed(1) + "k";
  return String(n);
}

function formatAge(epoch) {
  if (!epoch) return "recent";
  const diff = Date.now() / 1000 - epoch;
  if (diff < 3600) return `${Math.max(1, Math.round(diff / 60))}m ago`;
  if (diff < 86400) return `${Math.round(diff / 3600)}h ago`;
  return `${Math.round(diff / 86400)}d ago`;
}
