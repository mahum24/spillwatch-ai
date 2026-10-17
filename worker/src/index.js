// SpillWatch assistant proxy (Cloudflare Worker).
//
// Holds the Gemini API key so the static website never contains it. The Worker builds the
// prompt and tool list itself (from the same tools.js the website uses); callers can only
// send the conversation. That, a per-visitor rate limit and an origin allow-list stop the
// key being reused as a general-purpose free Gemini proxy.
//
// The same Worker also runs on a schedule (Cron Triggers) and starts the GitHub Actions
// workflows on time, because GitHub's own scheduler delays them by hours on this repo.

import { buildRequest } from "../../site/assistant/tools.js";

const MAX_TURNS = 40;
const MAX_BYTES = 120_000;
const PART_KEYS = new Set(["text", "functionCall", "functionResponse", "thought", "thoughtSignature"]);

function allowedOrigin(origin, env) {
  if (!origin) return null;
  const allowed = (env.ALLOWED_ORIGINS || "").split(",").map((s) => s.trim()).filter(Boolean);
  if (allowed.includes(origin)) return origin;
  if (/^http:\/\/(localhost|127\.0\.0\.1)(:\d+)?$/.test(origin)) return origin;
  return null;
}

function json(body, status, origin) {
  const headers = { "content-type": "application/json", "cache-control": "no-store" };
  if (origin) Object.assign(headers, { "access-control-allow-origin": origin, vary: "Origin" });
  return new Response(JSON.stringify(body), { status, headers });
}
const fail = (status, message, origin) => json({ error: { code: status, message } }, status, origin);

function validContents(contents) {
  if (!Array.isArray(contents) || contents.length < 1 || contents.length > MAX_TURNS) return false;
  for (const c of contents) {
    if (!c || (c.role !== "user" && c.role !== "model") || !Array.isArray(c.parts) || !c.parts.length) return false;
    for (const p of c.parts) {
      if (!p || typeof p !== "object" || !Object.keys(p).every((k) => PART_KEYS.has(k))) return false;
    }
  }
  return contents[contents.length - 1].role === "user";
}

// Which workflow each cron expression (worker/wrangler.toml) starts.
export const SCHEDULE = {
  "7,22,37,52 * * * *": "collect.yml",
  "12 * * * *": "publish.yml",
};

export async function dispatchWorkflow(workflow, env, fetchFn = fetch) {
  const repo = env.GITHUB_REPO || "mahum24/spillwatch-ai";
  const r = await fetchFn(`https://api.github.com/repos/${repo}/actions/workflows/${workflow}/dispatches`, {
    method: "POST",
    headers: {
      authorization: `Bearer ${env.GITHUB_TOKEN}`,
      accept: "application/vnd.github+json",
      "x-github-api-version": "2022-11-28",
      "user-agent": "spillwatch-scheduler",
    },
    body: JSON.stringify({ ref: "main" }),
  });
  if (r.status !== 204) throw new Error(`GitHub dispatch of ${workflow} failed: ${r.status} ${(await r.text()).slice(0, 200)}`);
}

export default {
  async scheduled(event, env) {
    const workflow = SCHEDULE[event.cron];
    if (!workflow || !env.GITHUB_TOKEN) return;
    await dispatchWorkflow(workflow, env);
  },

  async fetch(request, env) {
    const origin = allowedOrigin(request.headers.get("origin"), env);
    const url = new URL(request.url);

    if (request.method === "OPTIONS") {
      if (!origin) return new Response(null, { status: 403 });
      return new Response(null, {
        status: 204,
        headers: {
          "access-control-allow-origin": origin, "access-control-allow-methods": "POST, OPTIONS",
          "access-control-allow-headers": "content-type", "access-control-max-age": "86400", vary: "Origin",
        },
      });
    }
    if (url.pathname === "/health") {
      return json({ ok: true, model: env.GEMINI_MODEL, assistant_ready: !!env.GEMINI_API_KEY, scheduler_ready: !!env.GITHUB_TOKEN }, 200, origin);
    }
    if (url.pathname !== "/chat" || request.method !== "POST") return fail(404, "Not found", origin);
    if (!origin) return fail(403, "This assistant only serves the SpillWatch website.", null);
    if (!env.GEMINI_API_KEY) return fail(503, "The assistant is not configured yet.", origin);

    if (env.RATE_LIMITER) {
      const ip = request.headers.get("cf-connecting-ip") || "unknown";
      const { success } = await env.RATE_LIMITER.limit({ key: ip });
      if (!success) return fail(429, "You are asking questions very quickly. Please wait a minute and try again.", origin);
    }

    const raw = await request.text();
    if (raw.length > MAX_BYTES) return fail(413, "This conversation is too long. Start a new chat.", origin);
    let body;
    try { body = JSON.parse(raw); } catch { return fail(400, "Invalid JSON.", origin); }
    if (!validContents(body.contents)) return fail(400, "Invalid conversation.", origin);

    const model = env.GEMINI_MODEL || "gemini-3.5-flash-lite";
    const upstream = await fetch(`https://generativelanguage.googleapis.com/v1beta/models/${model}:generateContent`, {
      method: "POST",
      headers: { "content-type": "application/json", "x-goog-api-key": env.GEMINI_API_KEY },
      body: JSON.stringify(buildRequest(body.contents)),
    });
    const text = await upstream.text();
    if (upstream.status === 429) {
      return fail(429, "The shared free AI allowance is used up for now. Try again later, or add your own free Gemini key in the assistant's settings.", origin);
    }
    if (!upstream.ok) {
      // Don't relay Google's error text verbatim: it can mention the key or project.
      console.log("gemini error", upstream.status, text.slice(0, 500));
      return fail(502, `The AI service returned an error (${upstream.status}). Please try again.`, origin);
    }
    return new Response(text, {
      status: 200,
      headers: { "content-type": "application/json", "cache-control": "no-store", "access-control-allow-origin": origin, vary: "Origin" },
    });
  },
};
