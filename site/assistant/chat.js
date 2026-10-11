// SpillWatch assistant: the chat panel on the website.
// Questions go to the SpillWatch Worker (shared key) or, if the visitor added their own
// Gemini key, straight from this browser to Google. Tools run here, over the data the map
// has already loaded, so the model only ever sees SpillWatch's own figures.

import { ask, directGemini, viaWorker, trimHistory, ModelError } from "./agent.js";
import { buildRequest } from "./tools.js";

const DEFAULT_MODEL = "gemini-3.5-flash-lite";
const KEY_NAME = "spillwatch.geminiKey";
const WORKER_URL = document.querySelector('meta[name="spillwatch-assistant"]')?.content?.trim() || "";
const EXAMPLES = [
  "Is anything spilling near Hebden Bridge right now?",
  "Which water company had the most spills this week?",
  "Show me the AI's most unexpected spills",
  "Where is spill risk highest tomorrow?",
];

const $ = (sel, root = document) => root.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

// Visitor's own key: this tab only by default, this device if they tick "remember".
const store = {
  get() {
    try { return sessionStorage.getItem(KEY_NAME) || localStorage.getItem(KEY_NAME) || ""; } catch { return ""; }
  },
  set(key, remember) {
    try {
      this.clear();
      (remember ? localStorage : sessionStorage).setItem(KEY_NAME, key);
    } catch { /* storage blocked: key lives in memory for this page only */ }
    memoryKey = key;
  },
  clear() {
    try { sessionStorage.removeItem(KEY_NAME); localStorage.removeItem(KEY_NAME); } catch { /* ignore */ }
    memoryKey = "";
  },
};
let memoryKey = "";
const ownKey = () => memoryKey || store.get();

const history = [];
let busy = false;

function build() {
  document.body.insertAdjacentHTML("beforeend", `
    <button id="askOpen" class="ask-open" type="button" aria-haspopup="dialog">
      <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 5h16v11H9l-5 4z"/></svg>Ask SpillWatch AI
    </button>
    <section id="ask" class="ask" role="dialog" aria-label="SpillWatch AI assistant" hidden>
      <header class="ask-head">
        <strong>Ask SpillWatch AI</strong>
        <span class="ask-head-btns">
          <button id="askSettingsBtn" class="ask-icon" type="button" aria-label="Assistant settings" title="Settings">⚙</button>
          <button id="askClose" class="ask-icon" type="button" aria-label="Close assistant">×</button>
        </span>
      </header>
      <div id="askSettings" class="ask-settings" hidden></div>
      <div id="askLog" class="ask-log" aria-live="polite"></div>
      <form id="askForm" class="ask-form">
        <input id="askInput" type="text" maxlength="400" autocomplete="off" placeholder="Ask about spills, places or risk…" aria-label="Your question">
        <button id="askSend" type="submit">Ask</button>
      </form>
      <p class="ask-foot">Answers come from SpillWatch's live data via Google Gemini and can be wrong. A flagged spill is not proof of wrongdoing.</p>
    </section>`);
  $("#askOpen").addEventListener("click", open);
  $("#askClose").addEventListener("click", close);
  $("#askSettingsBtn").addEventListener("click", () => toggleSettings());
  $("#askForm").addEventListener("submit", (e) => { e.preventDefault(); submit($("#askInput").value); });
  $("#askLog").addEventListener("click", onLogClick);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !$("#ask").hidden) close(); });
  renderEmpty();
}

function open() {
  $("#ask").hidden = false;
  $("#askOpen").hidden = true;
  if (!WORKER_URL && !ownKey()) toggleSettings(true);
  $("#askInput").focus();
}
function close() {
  $("#ask").hidden = true;
  $("#askOpen").hidden = false;
  $("#askOpen").focus();
}

function renderEmpty() {
  $("#askLog").innerHTML = `
    <p class="ask-intro">Ask in plain English. I look up the answer in SpillWatch's live data and show which overflows I used.</p>
    <div class="ask-examples">${EXAMPLES.map((q) => `<button type="button" class="ask-chip" data-q="${esc(q)}">${esc(q)}</button>`).join("")}</div>`;
}

function toggleSettings(force) {
  const el = $("#askSettings");
  const show = force ?? el.hidden;
  el.hidden = !show;
  if (!show) return;
  const has = !!ownKey();
  el.innerHTML = `
    <h3>Use your own Gemini key <small>(optional)</small></h3>
    <p>${WORKER_URL ? "The assistant works without a key using SpillWatch's shared free allowance. If that runs out, add your own free key." : "Add a free Google Gemini key to use the assistant."}</p>
    <ol>
      <li>Open <a href="https://aistudio.google.com/apikey" target="_blank" rel="noopener">Google AI Studio</a> and sign in with a Google account.</li>
      <li>Click <strong>Create API key</strong> and copy it.</li>
      <li>Paste it below. It's free; no card needed.</li>
    </ol>
    <form id="keyForm" class="key-form">
      <input id="keyInput" type="password" autocomplete="off" spellcheck="false" placeholder="${has ? "Key saved. Paste to replace" : "Paste your key"}" aria-label="Gemini API key">
      <button type="submit">Save</button>
    </form>
    <label class="key-remember"><input id="keyRemember" type="checkbox"> Remember on this device</label>
    ${has ? '<button id="keyForget" type="button" class="link-btn">Forget my key</button>' : ""}
    <p class="key-note">Your key stays in this browser and is sent only to Google, never to SpillWatch. Free-tier questions may be used by Google to improve its models; SpillWatch only sends public data.</p>`;
  $("#keyForm").addEventListener("submit", (e) => {
    e.preventDefault();
    const k = $("#keyInput").value.trim();
    if (!k) return;
    store.set(k, $("#keyRemember").checked);
    toggleSettings(false);
    note("Your key is saved. Questions now go straight from your browser to Google.");
  });
  $("#keyForget")?.addEventListener("click", () => { store.clear(); toggleSettings(true); note("Your key has been removed from this browser."); });
}

function note(text) {
  $("#askLog").insertAdjacentHTML("beforeend", `<p class="ask-note">${esc(text)}</p>`);
  scrollLog();
}
const scrollLog = () => { const l = $("#askLog"); l.scrollTop = l.scrollHeight; };

function onLogClick(e) {
  const chip = e.target.closest(".ask-chip");
  if (chip) return submit(chip.dataset.q);
  const site = e.target.closest("[data-site]");
  if (site) {
    window.SpillWatch?.select(site.dataset.site);
    if (matchMedia("(max-width: 820px)").matches) close();
  }
}

// Minimal, safe formatting: escape first, then **bold**, bullet lists and [[overflow-id]] links.
function format(text, byId) {
  const lines = esc(text).split(/\n+/);
  let html = "", inList = false;
  for (const line of lines) {
    const bullet = /^\s*(?:[-*•]|\d+\.)\s+(.*)/.exec(line);
    if (bullet && !inList) { html += "<ul>"; inList = true; }
    if (!bullet && inList) { html += "</ul>"; inList = false; }
    const body = (bullet ? bullet[1] : line)
      .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
      .replace(/\[\[([a-z_]+:[A-Za-z0-9_.\-/]+)\]\]/g, (m, id) => (byId.has(id) ? `<button type="button" class="site-link" data-site="${id}">show on map</button>` : ""));
    html += bullet ? `<li>${body}</li>` : `<p>${body}</p>`;
  }
  return html + (inList ? "</ul>" : "");
}

const STEP_TEXT = {
  geocode: (a, r) => (r.matches ? `Found ${r.matches[0].name}` : `Couldn't find "${a.place}"`),
  get_summary: (a) => `Read ${a.company ? a.company + " " : "national "}figures`,
  search_overflows: (a, r) => `Searched overflows: ${r.totals?.matching_overflows ?? 0} matched`,
  get_overflow: (a) => `Opened ${a.id}`,
  list_unexpected_spills: (a, r) => `Checked unexpected spills: ${r.totals?.matching_spills ?? 0} found`,
};

async function submit(raw) {
  const q = String(raw || "").trim();
  if (!q || busy) return;
  const key = ownKey();
  if (!key && !WORKER_URL) { toggleSettings(true); return; }
  busy = true;
  $("#askSend").disabled = true;
  $("#askInput").value = "";
  $(".ask-intro")?.remove();
  $(".ask-examples")?.remove();
  const log = $("#askLog");
  log.insertAdjacentHTML("beforeend", `<div class="msg user">${esc(q)}</div><div class="msg bot pending"><span class="dots" aria-label="Thinking"></span><small class="progress"></small></div>`);
  const pending = log.lastElementChild;
  scrollLog();
  try {
    const sw = await window.SpillWatch.ready;
    const callModel = key ? directGemini({ apiKey: key, model: DEFAULT_MODEL }) : viaWorker({ url: `${WORKER_URL.replace(/\/$/, "")}/chat` });
    trimHistory(history);
    const res = await ask(q, {
      history, callModel, data: sw.data,
      buildBody: (contents) => buildRequest(contents),
      onStep: (name) => { pending.querySelector(".progress").textContent = { geocode: "Finding the place…", search_overflows: "Searching overflows…", get_overflow: "Reading overflow details…", list_unexpected_spills: "Checking unexpected spills…", get_summary: "Reading the latest figures…" }[name] || "Working…"; },
    });
    const steps = res.steps.map((s) => (STEP_TEXT[s.tool] || (() => s.tool))(s.args, s.result));
    pending.classList.remove("pending");
    pending.innerHTML = format(res.text || "Sorry, I couldn't produce an answer.", sw.data.byId)
      + (steps.length ? `<details class="how"><summary>How I answered</summary><ul>${steps.map((s) => `<li>${esc(s)}</li>`).join("")}</ul></details>` : "");
  } catch (err) {
    pending.classList.remove("pending");
    pending.classList.add("error");
    const badKey = key && err instanceof ModelError && (err.status === 400 || err.status === 403) && /key/i.test(err.message);
    pending.innerHTML = esc(badKey ? "Google rejected your API key. Check it in settings (⚙)." : err.message || "Something went wrong.")
      + (!key && err.status === 429 ? ' <button type="button" class="link-btn" data-open-settings>Add your own key</button>' : "");
    pending.querySelector("[data-open-settings]")?.addEventListener("click", () => toggleSettings(true));
    console.error(err);
  } finally {
    busy = false;
    $("#askSend").disabled = false;
    scrollLog();
  }
}

build();
