// SpillWatch assistant: the tools the language model may call, and the request it is sent.
//
// Shared by three runtimes, so it has no imports and no DOM access:
//   - the browser chat (site/assistant/chat.js) runs the tools over the data the map loaded
//   - the Cloudflare Worker (worker/) builds the Gemini request, so the prompt and tools
//     cannot be swapped by a caller using it as a free proxy
//   - the evaluation harness (eval/assistant/run.mjs) runs the same tools under Node
//
// The model never sees the raw data. It sees only what these functions return, which keeps
// answers grounded and lets the evaluation check every number against the tool outputs.

export const COMPANIES = {
  anglian: "Anglian Water", northumbrian: "Northumbrian Water", severn_trent: "Severn Trent",
  south_west: "South West Water", southern: "Southern Water", thames: "Thames Water",
  united_utilities: "United Utilities", wessex: "Wessex Water", yorkshire: "Yorkshire Water",
};

const JUNK_NAMES = new Set(["", "#N/A", "N/A", "NA", "UNKNOWN", "NULL", "-"]);
export function overflowName(o) {
  const w = (o.water || "").trim();
  const site = o.site || String(o.id || "").split(":")[1] || o.id;
  return JUNK_NAMES.has(w.toUpperCase()) ? `Overflow ${site}` : w.replace(/\b\w+/g, (x) => x[0] + x.slice(1).toLowerCase());
}

const ukTime = new Intl.DateTimeFormat("en-GB", {
  weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", timeZone: "Europe/London",
});
const uk = (iso) => (iso ? ukTime.format(new Date(iso)) : null);
const pct = (p) => (p == null ? null : Math.round(p * 1000) / 10);
const round1 = (x) => (x == null ? null : Math.round(x * 10) / 10);
const clamp = (x, lo, hi, dflt) => (Number.isFinite(+x) ? Math.min(hi, Math.max(lo, +x)) : dflt);

function km(lat1, lon1, lat2, lon2) {
  const r = Math.PI / 180;
  const a = Math.sin(((lat2 - lat1) * r) / 2) ** 2
    + Math.cos(lat1 * r) * Math.cos(lat2 * r) * Math.sin(((lon2 - lon1) * r) / 2) ** 2;
  return 12742 * Math.asin(Math.sqrt(a));
}

function companyKey(c) {
  if (!c) return null;
  const s = String(c).toLowerCase().replace(/water|plc|ltd/g, "").replace(/[^a-z]+/g, " ").trim().replace(/ /g, "_");
  if (COMPANIES[s]) return s;
  const hit = Object.keys(COMPANIES).find((k) => k.startsWith(s) || s.startsWith(k) || k.replace("_", "") === s.replace("_", ""));
  return hit || "unknown";
}

const STATUS = { 1: "spilling", 0: "not spilling", "-1": "monitor offline" };

function brief(o, origin) {
  const out = {
    id: o.id,
    name: overflowName(o),
    company: COMPANIES[o.company] || o.company,
    status: STATUS[o.status] ?? "unknown",
  };
  if (origin) out.distance_km = round1(km(origin.lat, origin.lon, o.lat, o.lon));
  if (o.status === 1) out.spilling_since_uk = uk(o.since);
  out.spills_last_7_days = o.spills_7d;
  out.spill_hours_last_7_days = round1(o.hours_7d);
  if (o.flagged_7d) out.unexpected_spills_last_7_days = o.flagged_7d;
  out.risk_today_pct = pct(o.risk_today);
  out.risk_tomorrow_pct = pct(o.risk_tomorrow);
  return out;
}

// ---------------------------------------------------------------------------------------
// Declarations sent to Gemini (OpenAPI-style schema subset)

const S = (description, extra = {}) => ({ type: "STRING", description, ...extra });
const N = (description) => ({ type: "NUMBER", description });
const I = (description) => ({ type: "INTEGER", description });
const companyParam = S("Water company, e.g. 'Thames Water' or 'yorkshire'. Omit for all of England.");

export const TOOL_DECLARATIONS = [
  {
    name: "geocode",
    description: "Find the latitude and longitude of a place in England (town, village, beach, river town) or a UK postcode. Call this before searching near a place; never guess coordinates.",
    parameters: { type: "OBJECT", properties: { place: S("Place name or postcode, e.g. 'Hebden Bridge' or 'LS1 4AP'.") }, required: ["place"] },
  },
  {
    name: "get_summary",
    description: "National or per-company totals: overflows monitored, spilling now, monitors offline, spills and spill hours in the last 7 days, AI-flagged unexpected spills, high-risk counts, when each company's feed last updated, and the models' evaluation scores.",
    parameters: { type: "OBJECT", properties: { company: companyParam } },
  },
  {
    name: "search_overflows",
    description: "Search storm overflows. Filter by distance from a point, company, river or sea name, status or risk; sort and return the top matches plus totals for everything that matched.",
    parameters: {
      type: "OBJECT",
      properties: {
        lat: N("Latitude of the search centre (from geocode)."),
        lon: N("Longitude of the search centre (from geocode)."),
        radius_km: N("Search radius in km around lat/lon. Default 5, maximum 50."),
        company: companyParam,
        water_name: S("Part of the receiving water's name, e.g. 'Wharfe' or 'Thames'."),
        status: S("Filter by live status.", { enum: ["spilling", "not_spilling", "offline", "any"] }),
        risk_level: S("Only overflows at this forecast risk level or higher.", { enum: ["high", "elevated"] }),
        risk_day: S("Which day risk_level and sort refer to. Default tomorrow.", { enum: ["today", "tomorrow"] }),
        sort_by: S("Ordering of the returned list. Default: distance if a point is given, else spills_last_7_days.",
          { enum: ["distance", "spills_last_7_days", "spill_hours_last_7_days", "risk", "spilling_longest"] }),
        limit: I("How many overflows to list (default 10, maximum 25). Totals always cover all matches."),
      },
    },
  },
  {
    name: "get_overflow",
    description: "Full details for one overflow by its id (e.g. 'yorkshire:YWS00435'): status, last spill, last 7 days, risk today and tomorrow with forecast rain, data caveats, and any unexpected-spill explanations.",
    parameters: { type: "OBJECT", properties: { id: S("Overflow id as returned by other tools.") }, required: ["id"] },
  },
  {
    name: "list_unexpected_spills",
    description: "Spills in the last 7 days that the AI model judged unlikely for the rain that fell, and/or that broke the simple dry-day rule. Each comes with the rainfall and the model's probability. These are potentially suspicious, not proven illegal.",
    parameters: {
      type: "OBJECT",
      properties: {
        company: companyParam,
        lat: N("Optional centre latitude (from geocode)."),
        lon: N("Optional centre longitude (from geocode)."),
        radius_km: N("Radius around lat/lon, default 10, maximum 50."),
        flag: S("'model' = flagged by the AI, 'rule' = broke the dry-day rule, 'either' (default).", { enum: ["model", "rule", "either"] }),
        limit: I("How many to list (default 10, maximum 25). The total always covers all matches."),
      },
    },
  },
];

// ---------------------------------------------------------------------------------------
// Implementations. `data` = { overflows, suspicious, summary } exactly as the website loads
// them; `env.fetch` is used for geocoding (injected so tests can run offline).

async function geocode({ place }, _data, env) {
  const q = String(place || "").trim();
  if (!q) return { error: "No place given." };
  const fetchFn = env.fetch || fetch;
  const postcode = /^[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}$/i.test(q);
  if (postcode) {
    const r = await fetchFn(`https://api.postcodes.io/postcodes/${encodeURIComponent(q)}`);
    const j = await r.json();
    if (j.status !== 200) return { error: `Postcode ${q} not found.` };
    const p = j.result;
    return { matches: [{ name: p.postcode, area: [p.admin_district, p.region].filter(Boolean).join(", "), lat: p.latitude, lon: p.longitude }] };
  }
  const r = await fetchFn(`https://api.postcodes.io/places?q=${encodeURIComponent(q)}&limit=10`);
  const j = await r.json();
  const rank = { City: 0, Town: 1, Village: 2, Hamlet: 3, "Suburban Area": 2, "Other Settlement": 4 };
  const lc = q.toLowerCase();
  const matches = (j.result || [])
    .filter((p) => p.country === "England" || p.country === "Wales")
    .sort((a, b) => (b.name_1.toLowerCase() === lc) - (a.name_1.toLowerCase() === lc) || (rank[a.local_type] ?? 5) - (rank[b.local_type] ?? 5))
    .slice(0, 3)
    .map((p) => ({ name: p.name_1, type: p.local_type, area: [p.district_borough || p.county_unitary, p.region].filter(Boolean).join(", "), lat: round4(p.latitude), lon: round4(p.longitude) }));
  return matches.length ? { matches } : { error: `No place called '${q}' found in England.` };
}
const round4 = (x) => Math.round(x * 1e4) / 1e4;

function getSummary({ company }, { summary }) {
  const models = {
    unexpected_spill_model_auc: summary.models?.suspicion_auc,
    unexpected_spill_model_light_rain_recall_pct: pct(summary.models?.suspicion_light_rain_recall),
    forecast_auc_today: summary.models?.forecast_auc_today,
    forecast_auc_tomorrow: summary.models?.forecast_auc_tomorrow,
  };
  const base = { data_updated_uk: uk(summary.generated_at), last_complete_rain_day: summary.last_full_rain_day };
  const key = companyKey(company);
  if (key === "unknown") return { error: `Unknown company '${company}'. Known: ${Object.values(COMPANIES).join(", ")}.` };
  if (key) {
    const c = (summary.by_company || []).find((x) => x.company === key);
    if (!c) return { error: `No data for ${COMPANIES[key]}.` };
    return {
      ...base,
      company: COMPANIES[key],
      overflows_monitored: c.overflows,
      spilling_now: c.spilling_now,
      monitors_offline: c.offline,
      spills_last_7_days: c.spills_7d,
      spill_hours_last_7_days: Math.round(c.hours_7d),
      ai_flagged_unexpected_last_7_days: c.flagged_7d,
      publishes_spill_history: c.with_history > 0,
      feed_last_updated_uk: uk(summary.feeds?.[key]),
      models,
    };
  }
  return {
    ...base,
    area: "England",
    overflows_monitored: summary.overflows,
    spilling_now: summary.spilling_now,
    monitors_offline: summary.offline_now,
    spills_last_7_days: summary.spills_7d,
    ai_flagged_unexpected_last_7_days: summary.model_flagged_7d,
    dry_day_rule_breaches_last_7_days: summary.rule_flagged_7d,
    high_risk_today: summary.high_risk_today,
    high_risk_tomorrow: summary.high_risk_tomorrow,
    by_company: (summary.by_company || []).map((c) => ({
      company: COMPANIES[c.company] || c.company, overflows: c.overflows, spilling_now: c.spilling_now,
      spills_last_7_days: c.spills_7d, ai_flagged_unexpected_last_7_days: c.flagged_7d,
    })),
    models,
  };
}

function searchOverflows(args, { overflows }) {
  const key = companyKey(args.company);
  if (key === "unknown") return { error: `Unknown company '${args.company}'. Known: ${Object.values(COMPANIES).join(", ")}.` };
  const hasPoint = Number.isFinite(+args.lat) && Number.isFinite(+args.lon) && args.lat !== null && args.lon !== null;
  const origin = hasPoint ? { lat: +args.lat, lon: +args.lon } : null;
  const radius = clamp(args.radius_km, 0.1, 50, 5);
  const water = args.water_name ? String(args.water_name).toLowerCase() : null;
  const day = args.risk_day === "today" ? "risk_today" : "risk_tomorrow";
  const minRisk = args.risk_level === "high" ? 0.5 : args.risk_level === "elevated" ? 0.2 : null;
  const statusCode = { spilling: 1, not_spilling: 0, offline: -1 }[args.status];

  let rows = overflows.map((o) => ({ o, d: origin ? km(origin.lat, origin.lon, o.lat, o.lon) : null }));
  if (origin) rows = rows.filter((r) => r.d <= radius);
  if (key) rows = rows.filter((r) => r.o.company === key);
  if (water) rows = rows.filter((r) => (r.o.water || "").toLowerCase().includes(water));
  if (statusCode !== undefined) rows = rows.filter((r) => r.o.status === statusCode);
  if (minRisk != null) rows = rows.filter((r) => r.o[day] != null && r.o[day] >= minRisk);

  const sort = args.sort_by || (origin ? "distance" : "spills_last_7_days");
  const by = {
    distance: (a, b) => (a.d ?? 0) - (b.d ?? 0),
    spills_last_7_days: (a, b) => b.o.spills_7d - a.o.spills_7d || b.o.hours_7d - a.o.hours_7d,
    spill_hours_last_7_days: (a, b) => b.o.hours_7d - a.o.hours_7d,
    risk: (a, b) => (b.o[day] ?? -1) - (a.o[day] ?? -1),
    spilling_longest: (a, b) => (a.o.status === 1 ? a.o.since || "" : "~").localeCompare(b.o.status === 1 ? b.o.since || "" : "~"),
  }[sort] || ((a, b) => (a.d ?? 0) - (b.d ?? 0));
  rows.sort(by);

  const limit = clamp(args.limit, 1, 25, 10);
  return {
    filters_applied: {
      ...(origin && { centre: origin, radius_km: radius }), ...(key && { company: COMPANIES[key] }),
      ...(water && { water_name: args.water_name }), ...(args.status && args.status !== "any" && { status: args.status }),
      ...(minRisk != null && { risk_at_least: args.risk_level, risk_day: day === "risk_today" ? "today" : "tomorrow" }),
      sort_by: sort,
    },
    totals: {
      matching_overflows: rows.length,
      spilling_now: rows.filter((r) => r.o.status === 1).length,
      monitors_offline: rows.filter((r) => r.o.status === -1).length,
      spills_last_7_days: rows.reduce((s, r) => s + (r.o.spills_7d || 0), 0),
      spill_hours_last_7_days: Math.round(rows.reduce((s, r) => s + (r.o.hours_7d || 0), 0)),
      high_risk_tomorrow: rows.filter((r) => r.o.risk_tomorrow >= 0.5).length,
    },
    listed: Math.min(limit, rows.length),
    overflows: rows.slice(0, limit).map((r) => brief(r.o, origin)),
  };
}

function getOverflow({ id }, { overflows, suspicious, summary }) {
  const o = overflows.find((x) => x.id === id) || overflows.find((x) => x.site && String(id).toUpperCase().endsWith(x.site.toUpperCase()));
  if (!o) return { error: `No overflow with id '${id}'. Use search_overflows to find ids.` };
  const feedAgeH = summary.feeds?.[o.company] ? (Date.parse(summary.generated_at) - Date.parse(summary.feeds[o.company])) / 3.6e6 : null;
  const caveats = [];
  if (!o.history) caveats.push("This company does not publish spill history, so the models cannot learn this overflow's usual behaviour (lower confidence).");
  if (o.risk_today == null) caveats.push("No rain gauge within 10 km, so there is no risk forecast or unexpected-spill check for this overflow.");
  if (feedAgeH != null && feedAgeH > 24) caveats.push(`The company's feed had not updated for ${Math.round(feedAgeH)} hours, so the status may be out of date.`);
  if (o.status === 1 && o.since && (Date.parse(summary.generated_at) - Date.parse(o.since)) / 3.6e6 > 72) {
    caveats.push("Reported as spilling continuously for more than 3 days: a prolonged discharge or possibly a monitor fault.");
  }
  return {
    ...brief(o),
    receiving_water: o.water,
    lat: o.lat, lon: o.lon,
    status_since_uk: uk(o.since),
    last_spill: o.last_start ? { started_uk: uk(o.last_start), ended_uk: o.last_end ? uk(o.last_end) : "ongoing" } : null,
    risk_level_today: o.level_today,
    risk_level_tomorrow: o.level_tomorrow,
    forecast_rain_today_mm: o.rain_fc_today_mm,
    forecast_rain_tomorrow_mm: o.rain_fc_tomorrow_mm,
    caveats,
    unexpected_spills: suspicious.filter((s) => s.id === o.id).map(susItem),
  };
}

function susItem(s) {
  return {
    started_uk: uk(s.start),
    duration_hours: s.hours,
    ongoing: s.ongoing,
    flagged_by_ai_model: s.model_flag,
    broke_dry_day_rule: s.rule_flag,
    model_probability_of_spill_pct: pct(s.p_spill),
    rain_on_day_mm: s.rain_day_mm,
    rain_previous_day_mm: s.rain_prev_day_mm,
    rain_last_7_days_mm: s.rain_7d_mm,
    explanation: s.explanation,
  };
}

function listUnexpected(args, { suspicious }) {
  const key = companyKey(args.company);
  if (key === "unknown") return { error: `Unknown company '${args.company}'. Known: ${Object.values(COMPANIES).join(", ")}.` };
  const hasPoint = Number.isFinite(+args.lat) && Number.isFinite(+args.lon) && args.lat !== null && args.lon !== null;
  const radius = clamp(args.radius_km, 0.1, 50, 10);
  const flag = args.flag || "either";
  let rows = suspicious.slice();
  if (key) rows = rows.filter((s) => s.company === key);
  if (hasPoint) rows = rows.filter((s) => km(+args.lat, +args.lon, s.lat, s.lon) <= radius);
  if (flag === "model") rows = rows.filter((s) => s.model_flag);
  if (flag === "rule") rows = rows.filter((s) => s.rule_flag);
  rows.sort((a, b) => (b.model_flag - a.model_flag) || (a.p_spill ?? 1) - (b.p_spill ?? 1));
  const limit = clamp(args.limit, 1, 25, 10);
  return {
    filters_applied: { ...(key && { company: COMPANIES[key] }), ...(hasPoint && { centre: { lat: +args.lat, lon: +args.lon }, radius_km: radius }), flag },
    totals: {
      matching_spills: rows.length,
      distinct_overflows: new Set(rows.map((s) => s.id)).size,
      flagged_by_ai_model: rows.filter((s) => s.model_flag).length,
      broke_dry_day_rule: rows.filter((s) => s.rule_flag).length,
    },
    listed: Math.min(limit, rows.length),
    spills: rows.slice(0, limit).map((s) => ({
      id: s.id, name: overflowName(s), company: COMPANIES[s.company] || s.company,
      ...(hasPoint && { distance_km: round1(km(+args.lat, +args.lon, s.lat, s.lon)) }),
      ...susItem(s),
    })),
  };
}

const IMPL = {
  geocode,
  get_summary: getSummary,
  search_overflows: searchOverflows,
  get_overflow: getOverflow,
  list_unexpected_spills: listUnexpected,
};

export async function runTool(name, args, data, env = {}) {
  const fn = IMPL[name];
  if (!fn) return { error: `Unknown tool '${name}'.` };
  try {
    return await fn(args || {}, data, env);
  } catch (err) {
    return { error: `Tool failed: ${err.message}` };
  }
}

// ---------------------------------------------------------------------------------------
// The request. Built in one place so the browser (own key), the Worker (shared key) and
// the evaluation send exactly the same prompt and tools.

export function systemPrompt(now = new Date()) {
  return `You are the SpillWatch AI assistant on a public website that monitors sewage storm overflows in England.
Today is ${ukTime.format(now)} (UK time).

How to answer:
- Use ONLY facts returned by your tools. Never invent overflow names, numbers, dates or places. If the tools do not give the answer, say what you could not find.
- For questions about a place, call geocode first, then search with its coordinates. If geocode returns several matches, use the first unless the user said otherwise.
- Mention overflows using their id in double square brackets, e.g. [[yorkshire:YWS00435]]; the website turns these into links on the map. Put the readable name before it.
- Be brief: two to six sentences or a short list. Use plain English for the public. Give times in UK time as the tools return them.
- When totals were cut off by a list limit, say how many matched in total.

Facts about the data and models (you may state these):
- Water companies publish near-real-time monitor data for about 14,000 storm overflows; SpillWatch collects it every 15 minutes. Event Duration Monitors can fail or report wrongly.
- An "unexpected spill" is one the AI model judged unlikely given the rain measured by gauges within 10 km (on the day, the day before and the past 30 days) and the overflow's usual behaviour. The "dry-day rule" is the simple check: less than 0.25 mm of rain on the day and the day before.
- A flagged spill is potentially suspicious, NOT proof of illegal activity. Never say a company broke the law or acted illegally; say it may merit investigation by the Environment Agency.
- Risk today/tomorrow is the model's chance that the overflow spills during that rain day (09:00 to 09:00 UK time). High is 50% or more, elevated is 20-50%. Forecasts cover only today and tomorrow.
- Anglian, South West, Southern and Thames Water do not publish spill history, so their overflows are scored with lower confidence.
- SpillWatch has no water-quality measurements (no bacteria or pollution readings). It cannot say whether water is safe to swim in; for bathing advice point to the Environment Agency's Swimfo service and the local council. A spill nearby, or high spill risk, is a reason for caution.

Refuse politely anything unrelated to sewage overflows, rivers, rainfall or this website.`;
}

export function buildRequest(contents, { now = new Date(), useTools = true } = {}) {
  return {
    systemInstruction: { parts: [{ text: systemPrompt(now) }] },
    contents,
    ...(useTools && { tools: [{ functionDeclarations: TOOL_DECLARATIONS }] }),
    generationConfig: { maxOutputTokens: 2048 },
  };
}
