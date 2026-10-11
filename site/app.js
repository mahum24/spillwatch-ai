"use strict";

const COMPANIES = {
  anglian: "Anglian Water", northumbrian: "Northumbrian Water", severn_trent: "Severn Trent",
  south_west: "South West Water", southern: "Southern Water", thames: "Thames Water",
  united_utilities: "United Utilities", wessex: "Wessex Water", yorkshire: "Yorkshire Water",
};
const VIEWS = {
  now: { title: "Spilling now", help: "Live status of every storm overflow, updated every 15 minutes." },
  suspicious: { title: "Unexpected spills (last 7 days)", help: "Spills the AI judged unlikely given the rain that fell. Potentially suspicious, not proof of wrongdoing." },
  today: { title: "Highest spill risk today", help: "Chance an overflow spills between 09:00 today and 09:00 tomorrow, from recent rain and the latest forecast." },
  tomorrow: { title: "Highest spill risk tomorrow", help: "Chance an overflow spills during tomorrow's rain day (09:00–09:00). Useful before swimming or paddling." },
};

const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const fmtTime = new Intl.DateTimeFormat("en-GB", { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", timeZone: "Europe/London" });
const when = (iso) => (iso ? fmtTime.format(new Date(iso)) : "–");
const pct = (p) => (p == null ? "–" : p < 0.01 ? "<1%" : `${Math.round(p * 100)}%`);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
// Some feeds publish placeholders such as "#N/A" for the receiving water
const JUNK_NAMES = new Set(["", "#N/A", "N/A", "NA", "UNKNOWN", "NULL", "-"]);
const title = (o) => {
  const w = (o.water || "").trim();
  return JUNK_NAMES.has(w.toUpperCase()) ? `Overflow ${o.site}` : w.replace(/\b\w+/g, (x) => x[0] + x.slice(1).toLowerCase());
};
const hoursSince = (iso) => (iso ? (Date.now() - new Date(iso).getTime()) / 3.6e6 : 0);

const state = { feeds: {}, view: "now", company: "", overflows: [], byId: new Map(), suspicious: [], susById: new Map(), markers: [], selected: null };

const map = L.map("map", { preferCanvas: true, zoomControl: true }).setView([52.9, -1.6], 6);
L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
  maxZoom: 19,
  attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
}).addTo(map);
const renderer = L.canvas({ padding: 0.5 });
const layer = L.layerGroup().addTo(map);

function recentlySpilled(o) {
  if (!o.last_start) return false;
  const end = o.last_end ? new Date(o.last_end) : new Date();
  return Date.now() - end.getTime() < 48 * 3600 * 1000;
}

function styleFor(o) {
  const v = state.view;
  if (v === "now") {
    if (o.status === 1) return { color: css("--spill"), r: 6, z: 3 };
    if (o.status === -1) return { color: css("--offline"), r: 2.5, z: 0 };
    if (recentlySpilled(o)) return { color: css("--recent"), r: 4, z: 2 };
    return { color: css("--idle"), r: 2.5, z: 1 };
  }
  if (v === "suspicious") {
    const s = state.susById.get(o.id);
    if (s && s.some((x) => x.model_flag)) return { color: css("--sus"), r: 7, z: 3 };
    if (s && s.some((x) => x.rule_flag)) return { color: css("--rule"), r: 5, z: 2, ring: true };
    return { color: css("--idle"), r: 2, z: 0, faint: true };
  }
  const level = v === "today" ? o.level_today : o.level_tomorrow;
  if (level === "high") return { color: css("--risk-high"), r: 6, z: 3 };
  if (level === "elevated") return { color: css("--risk-mid"), r: 4.5, z: 2 };
  if (level === "low") return { color: css("--risk-low"), r: 2.5, z: 1 };
  return { color: css("--offline"), r: 2, z: 0, faint: true };
}

function visible(o) {
  return !state.company || o.company === state.company;
}

function draw() {
  layer.clearLayers();
  const items = state.overflows.filter(visible).map((o) => ({ o, s: styleFor(o) })).sort((a, b) => a.s.z - b.s.z);
  for (const { o, s } of items) {
    const m = L.circleMarker([o.lat, o.lon], {
      renderer, radius: s.r, color: s.color, weight: s.ring ? 2.5 : 1,
      fillColor: s.color, fillOpacity: s.ring ? 0 : s.faint ? 0.35 : 0.85, opacity: s.faint ? 0.4 : 1,
    });
    m.bindTooltip(`${esc(title(o))}<br><small>${esc(COMPANIES[o.company] || o.company)}</small>`);
    m.on("click", () => select(o.id, false));
    m.addTo(layer);
  }
  drawLegend();
  drawList();
}

function drawLegend() {
  const sw = (v, label, ring) => `<span><i class="swatch${ring ? " ring" : ""}" style="background:${css(v)};border-color:${css(v)}"></i>${label}</span>`;
  const v = state.view;
  const legend = {
    now: sw("--spill", "Spilling now") + sw("--recent", "Spilled in last 48 h") + sw("--idle", "Not spilling") + sw("--offline", "Monitor offline"),
    suspicious: sw("--sus", "Unexpected (AI)") + sw("--rule", "Dry-day rule only", true),
    today: sw("--risk-high", "High (≥50%)") + sw("--risk-mid", "Elevated (20–50%)") + sw("--risk-low", "Low") + sw("--offline", "No rain data"),
  };
  legend.tomorrow = legend.today;
  document.getElementById("legend").innerHTML = legend[v];
}

function drawList() {
  const list = document.getElementById("list");
  const v = state.view;
  document.getElementById("listTitle").textContent = VIEWS[v].title;
  let rows = [];
  if (v === "now") {
    rows = state.overflows.filter((o) => visible(o) && o.status === 1)
      .sort((a, b) => (a.since || "").localeCompare(b.since || ""))
      .map((o) => ({ o, color: css("--spill"), val: `since ${when(o.since).replace(/^\w+ /, "")}` }));
  } else if (v === "suspicious") {
    rows = state.suspicious.filter((s) => (!state.company || s.company === state.company))
      .map((s) => ({ o: state.byId.get(s.id) || s, color: s.model_flag ? css("--sus") : css("--rule"), val: s.p_spill == null ? "" : pct(s.p_spill), sub: when(s.start) }));
  } else {
    const key = v === "today" ? "risk_today" : "risk_tomorrow";
    rows = state.overflows.filter((o) => visible(o) && o[key] != null && o[key] >= 0.2)
      .sort((a, b) => b[key] - a[key])
      .map((o) => ({ o, color: o[key] >= 0.5 ? css("--risk-high") : css("--risk-mid"), val: pct(o[key]) }));
  }
  if (!rows.length) {
    list.innerHTML = `<li class="empty">${v === "now" ? "No overflows are spilling right now." : "Nothing to show for this view."}</li>`;
    return;
  }
  list.innerHTML = rows.slice(0, 200).map((r, i) => `
    <li><button type="button" data-id="${esc(r.o.id)}" data-i="${i}">
      <i class="swatch" style="background:${r.color}"></i>
      <span class="name">${esc(title(r.o))}<small>${esc(COMPANIES[r.o.company] || r.o.company)}${r.sub ? " · " + esc(r.sub) : ""}</small></span>
      <span class="val">${esc(r.val)}</span>
    </button></li>`).join("") + (rows.length > 200 ? `<li class="empty">…and ${rows.length - 200} more. Zoom the map or filter by company.</li>` : "");
}

function riskRow(label, p, level) {
  const color = level === "high" ? css("--risk-high") : level === "elevated" ? css("--risk-mid") : css("--risk-low");
  return `<div class="risk-row"><span>${label}</span><span class="bar"><i style="width:${Math.max(2, Math.round((p ?? 0) * 100))}%;background:${color}"></i></span><strong>${pct(p)}</strong></div>`;
}

function select(id, fly = true) {
  const o = state.byId.get(id);
  if (!o) return;
  state.selected = id;
  writeHash();
  if (fly) map.flyTo([o.lat, o.lon], Math.max(map.getZoom(), 12), { duration: 0.6 });
  const status = o.status === 1 ? '<span class="badge spill">Spilling now</span>'
    : o.status === -1 ? '<span class="badge off">Monitor offline</span>' : '<span class="badge ok">Not spilling</span>';
  const sus = state.susById.get(id) || [];
  const levelText = { high: "Sewage discharge likely", elevated: "Discharge possible", low: "Discharge unlikely" };
  const el = document.getElementById("details");
  el.innerHTML = `
    <button class="close-details" type="button" aria-label="Close details">×</button>
    <h2>${esc(title(o))}</h2>
    <p class="sub">${esc(COMPANIES[o.company] || o.company)} · overflow ${esc(o.site)}</p>
    ${status}
    <dl class="facts">
      <dt>Last spill</dt><dd>${o.last_start ? `${when(o.last_start)} → ${o.last_end ? when(o.last_end) : "ongoing"}` : "none recorded"}</dd>
      <dt>Last 7 days</dt><dd>${o.spills_7d} spill${o.spills_7d === 1 ? "" : "s"}, ${o.hours_7d} h</dd>
    </dl>
    ${o.risk_today == null ? '<p class="note">No rain gauge within 10 km, so no risk forecast.</p>' : `
      ${riskRow("Today", o.risk_today, o.level_today)}
      ${riskRow("Tomorrow", o.risk_tomorrow, o.level_tomorrow)}
      <p class="note">${levelText[o.level_tomorrow] || ""} tomorrow. Forecast rain: ${o.rain_fc_today_mm ?? "–"} mm today, ${o.rain_fc_tomorrow_mm ?? "–"} mm tomorrow.</p>`}
    ${o.status === 1 && hoursSince(o.since) > 72 ? `<p class="note">Reported as spilling continuously for ${Math.round(hoursSince(o.since) / 24)} days. This is unusually long: it may be a prolonged discharge or a monitor fault.</p>` : ""}
    ${hoursSince(state.feeds[o.company]) > 24 ? `<p class="note">${esc(COMPANIES[o.company])}'s feed has not updated since ${when(state.feeds[o.company])}, so this status may be out of date.</p>` : ""}
    ${o.history ? "" : '<p class="note">Lower confidence: this company does not publish spill history, so the models cannot learn this overflow\'s usual behaviour.</p>'}
    ${sus.map((s) => `<div class="explain"><span class="badge ${s.model_flag ? "sus" : "rule"}">${s.model_flag ? "Unexpected (AI)" : "Dry-day rule"}</span> ${esc(s.explanation)}</div>`).join("")}`;
  el.hidden = false;
  el.querySelector(".close-details").addEventListener("click", () => { el.hidden = true; state.selected = null; writeHash(); });
  el.scrollIntoView({ block: "nearest", behavior: "smooth" });
}

// Shareable links: #view=suspicious&site=united_utilities:UUP00003
function readHash() {
  const h = new URLSearchParams(location.hash.slice(1));
  return { view: VIEWS[h.get("view")] ? h.get("view") : "now", site: h.get("site") };
}
function writeHash() {
  const h = new URLSearchParams({ view: state.view });
  if (state.selected) h.set("site", state.selected);
  history.replaceState(null, "", `#${h}`);
}

function setView(v) {
  state.view = v;
  document.querySelectorAll(".view-btn").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.view === v)));
  document.getElementById("viewHelp").textContent = VIEWS[v].help;
  draw();
  writeHash();
}

function nearMe() {
  if (!navigator.geolocation) return alert("Location is not available in this browser.");
  navigator.geolocation.getCurrentPosition((pos) => {
    const { latitude: lat, longitude: lon } = pos.coords;
    map.flyTo([lat, lon], 12);
    L.circleMarker([lat, lon], { radius: 7, color: css("--brand"), fillColor: css("--brand"), fillOpacity: 1 }).addTo(map).bindTooltip("You are here");
    const km = (o) => Math.hypot((o.lat - lat) * 111, (o.lon - lon) * 111 * Math.cos(lat * Math.PI / 180));
    const near = state.overflows.map((o) => ({ o, d: km(o) })).filter((x) => x.d <= 5).sort((a, b) => a.d - b.d).slice(0, 30);
    document.getElementById("listTitle").textContent = "Overflows within 5 km of you";
    document.getElementById("list").innerHTML = near.length ? near.map(({ o, d }) => `
      <li><button type="button" data-id="${esc(o.id)}">
        <i class="swatch" style="background:${o.status === 1 ? css("--spill") : o.level_tomorrow === "high" ? css("--risk-high") : o.level_tomorrow === "elevated" ? css("--risk-mid") : css("--idle")}"></i>
        <span class="name">${esc(title(o))}<small>${d.toFixed(1)} km · ${o.status === 1 ? "spilling now" : "tomorrow " + pct(o.risk_tomorrow)}</small></span>
        <span class="val"></span></button></li>`).join("") : '<li class="empty">No storm overflows within 5 km.</li>';
  }, () => alert("Could not get your location."));
}

async function load() {
  const [overflows, suspicious, summary] = await Promise.all(
    ["overflows", "suspicious", "summary"].map((n) => fetch(`data/${n}.json`, { cache: "no-cache" }).then((r) => r.json())));
  state.overflows = overflows;
  state.byId = new Map(overflows.map((o) => [o.id, o]));
  state.suspicious = suspicious;
  for (const s of suspicious) {
    if (!state.susById.has(s.id)) state.susById.set(s.id, []);
    state.susById.get(s.id).push(s);
  }
  state.summary = summary;
  state.feeds = summary.feeds || {};
  const n = (x) => x.toLocaleString("en-GB");
  document.getElementById("kpiNow").textContent = n(summary.spilling_now);
  document.getElementById("kpi7d").textContent = n(summary.spills_7d);
  document.getElementById("kpiSus").textContent = n(summary.model_flagged_7d);
  document.getElementById("kpiRisk").textContent = n(summary.high_risk_tomorrow);
  document.getElementById("updated").textContent = `Updated ${when(summary.generated_at)}`;
  document.querySelectorAll("[data-metric]").forEach((el) => {
    const v = summary.models[el.dataset.metric];
    el.textContent = el.hasAttribute("data-pct") ? `${Math.round(v * 100)}%` : v.toFixed(2);
  });
  const sel = document.getElementById("company");
  Object.entries(COMPANIES).forEach(([k, label]) => sel.insertAdjacentHTML("beforeend", `<option value="${k}">${label}</option>`));
  const initial = readHash();
  setView(initial.view);
  if (initial.site) select(initial.site);
}

document.querySelectorAll(".view-btn").forEach((b) => b.addEventListener("click", () => setView(b.dataset.view)));
document.getElementById("company").addEventListener("change", (e) => {
  state.company = e.target.value;
  draw();
  const pts = state.overflows.filter(visible).map((o) => [o.lat, o.lon]);
  if (state.company && pts.length) map.fitBounds(pts, { padding: [20, 20] });
});
document.getElementById("list").addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-id]");
  if (btn) select(btn.dataset.id);
});
document.getElementById("nearBtn").addEventListener("click", nearMe);
document.getElementById("aboutBtn").addEventListener("click", () => document.getElementById("about").showModal());
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", draw);
// The assistant (assistant/chat.js) reuses the loaded data and can open an overflow on the map.
window.SpillWatch = {
  select,
  ready: load().then(() => ({ data: { overflows: state.overflows, suspicious: state.suspicious, summary: state.summary, byId: state.byId } })),
};
window.SpillWatch.ready.catch((err) => {
  document.getElementById("updated").textContent = "Could not load data";
  console.error(err);
});
