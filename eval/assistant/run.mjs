// Evaluate the SpillWatch assistant (RQ3).
//
//   GEMINI_API_KEY=... node eval/assistant/run.mjs [--model gemini-3.5-flash-lite] [--mode tools|notools]
//                                                  [--data DIR] [--only L1,P1] [--delay 4000]
//
// The first run freezes site/data into eval/assistant/snapshots/<time>/ (not committed) and
// later runs default to the newest snapshot, so models and modes are compared on the same
// data. "notools" sends the same questions with no tools: the baseline for how much
// grounding matters. Results go to docs/results/assistant/.

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { ask, directGemini, ModelError } from "../../site/assistant/agent.js";
import { buildRequest, systemPrompt } from "../../site/assistant/tools.js";
import { makeQuestions, unsupportedNumbers } from "./questions.mjs";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const SNAPSHOTS = path.join(ROOT, "eval/assistant/snapshots");
const OUT = path.join(ROOT, "docs/results/assistant");

function args() {
  const a = { model: "gemini-3.5-flash-lite", mode: "tools", delay: 4000 };
  const argv = process.argv.slice(2);
  for (let i = 0; i < argv.length; i += 2) a[argv[i].replace(/^--/, "")] = argv[i + 1];
  a.delay = +a.delay;
  return a;
}

function apiKey() {
  if (process.env.GEMINI_API_KEY) return process.env.GEMINI_API_KEY;
  const env = path.join(ROOT, ".env");
  if (fs.existsSync(env)) {
    const m = /^GEMINI_API_KEY=(.+)$/m.exec(fs.readFileSync(env, "utf8"));
    if (m) return m[1].trim().replace(/^["']|["']$/g, "");
  }
  throw new Error("Set GEMINI_API_KEY (environment or .env file).");
}

function snapshotDir(requested) {
  if (requested) return path.resolve(requested);
  const existing = fs.existsSync(SNAPSHOTS) ? fs.readdirSync(SNAPSHOTS).sort() : [];
  if (existing.length) return path.join(SNAPSHOTS, existing[existing.length - 1]);
  const src = path.join(ROOT, "site/data");
  const stamp = JSON.parse(fs.readFileSync(path.join(src, "summary.json"))).generated_at.replace(/[:]/g, "");
  const dst = path.join(SNAPSHOTS, stamp);
  fs.mkdirSync(dst, { recursive: true });
  for (const f of ["overflows.json", "suspicious.json", "summary.json"]) fs.copyFileSync(path.join(src, f), path.join(dst, f));
  console.log(`Froze site/data into ${path.relative(ROOT, dst)}`);
  return dst;
}

const load = (dir, name) => JSON.parse(fs.readFileSync(path.join(dir, `${name}.json`)));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// Free-tier keys hit per-minute limits; wait and retry rather than score a failure.
function patient(callModel, delay) {
  let last = 0;
  return async (body) => {
    for (let attempt = 0; ; attempt++) {
      const wait = last + delay - Date.now();
      if (wait > 0) await sleep(wait);
      last = Date.now();
      try {
        return await callModel(body);
      } catch (err) {
        const retryable = err instanceof ModelError && (err.status === 429 || err.status >= 500);
        if (!retryable || attempt >= 6) throw err;
        const s = err.retryAfter ?? 15 * (attempt + 1);
        console.log(`    ${err.status}: waiting ${s}s`);
        await sleep(s * 1000);
      }
    }
  };
}

const shorten = (v) => {
  const s = JSON.stringify(v);
  return s.length > 1500 ? JSON.parse(JSON.stringify(v, (k, x) => (Array.isArray(x) && x.length > 5 ? [...x.slice(0, 5), `…${x.length - 5} more`] : x))) : v;
};

async function main() {
  const opt = args();
  const dir = snapshotDir(opt.data);
  const data = { overflows: load(dir, "overflows"), suspicious: load(dir, "suspicious"), summary: load(dir, "summary") };
  // Pretend "now" is the snapshot time, so "today" means the same day in every run.
  const now = new Date(data.summary.generated_at);
  const useTools = opt.mode !== "notools";
  const callModel = patient(directGemini({ apiKey: apiKey(), model: opt.model }), opt.delay);
  let questions = await makeQuestions(data);
  if (opt.only) questions = questions.filter((q) => opt.only.split(",").includes(q.id));

  console.log(`Model ${opt.model}, ${useTools ? "with tools" : "NO tools"}, snapshot ${path.basename(dir)}, ${questions.length} questions\n`);
  const results = [];
  for (const q of questions) {
    const t0 = Date.now();
    let res, error;
    try {
      res = await ask(q.question, { history: [], callModel, data, buildBody: (c) => buildRequest(c, { now, useTools }) });
    } catch (err) {
      error = err.message;
    }
    const answer = res?.text || "";
    const check = error ? { pass: false, note: `error: ${error}` } : q.check(answer, res.steps);
    const unsupported = error ? [] : unsupportedNumbers(answer, res.steps, q.question, systemPrompt(now));
    results.push({
      id: q.id, category: q.category, question: q.question, answer, pass: check.pass, note: check.note,
      unsupported_numbers: unsupported, tools: res?.steps.map((s) => ({ tool: s.tool, args: s.args, result: shorten(s.result) })) || [],
      model_calls: res?.modelCalls || 0, tokens: res?.usage || {}, seconds: (Date.now() - t0) / 1000, error,
    });
    console.log(`${check.pass ? "PASS" : "FAIL"} ${q.id.padEnd(3)} ${q.question}`);
    console.log(`      ${answer.replace(/\s+/g, " ").slice(0, 220)}`);
    if (!check.pass) console.log(`      -> ${check.note}`);
    if (unsupported.length) console.log(`      unsupported numbers: ${unsupported.join(", ")}`);
  }

  const cats = [...new Set(results.map((r) => r.category))];
  const rate = (rs, f) => `${rs.filter(f).length}/${rs.length}`;
  console.log("\ncategory   correct  grounded");
  for (const c of [...cats, "ALL"]) {
    const rs = c === "ALL" ? results : results.filter((r) => r.category === c);
    console.log(`${c.padEnd(10)} ${rate(rs, (r) => r.pass).padStart(7)}  ${rate(rs, (r) => !r.error && !r.unsupported_numbers.length).padStart(8)}`);
  }
  const ok = results.filter((r) => !r.error);
  const mean = (f) => (ok.reduce((s, r) => s + f(r), 0) / Math.max(1, ok.length));
  console.log(`\nmean model calls ${mean((r) => r.model_calls).toFixed(1)}, mean seconds ${mean((r) => r.seconds).toFixed(1)}, mean tokens in/out ${Math.round(mean((r) => r.tokens.prompt || 0))}/${Math.round(mean((r) => r.tokens.output || 0))}`);

  fs.mkdirSync(OUT, { recursive: true });
  const file = path.join(OUT, `${path.basename(dir)}_${opt.model}_${useTools ? "tools" : "notools"}${opt.only ? "_partial" : ""}.json`);
  fs.writeFileSync(file, JSON.stringify({ model: opt.model, mode: useTools ? "tools" : "notools", snapshot: path.basename(dir), run_at: new Date().toISOString(), results }, null, 2));
  console.log(`Saved ${path.relative(ROOT, file)}`);
}

main().catch((err) => { console.error(err.message); process.exit(1); });
