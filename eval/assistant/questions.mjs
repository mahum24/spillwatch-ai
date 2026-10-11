// Test questions for the assistant (RQ3). Expected answers are computed from the frozen data
// snapshot with the same tool functions, so the suite stays valid whenever it is re-run on a
// new snapshot. Each check returns {pass, note}.
//
// Categories:
//   lookup    single facts from the summary
//   place     needs geocoding and a spatial search
//   site      one overflow's details
//   anomaly   unexpected spills and why they were flagged
//   forecast  spill risk
//   method    facts about the models
//   trap      unanswerable, out of scope or leading questions where the right answer is to
//             decline, qualify or correct the premise

import { runTool, COMPANIES, overflowName } from "../../site/assistant/tools.js";

// Fixed coordinates (as postcodes.io returns them) so ground truth needs no network.
const PLACES = {
  "Hebden Bridge": { lat: 53.7411, lon: -2.0138 },
  "Ilkley": { lat: 53.9245, lon: -1.8227 },
};

const norm = (s) => s.toLowerCase().replace(/[’']/g, "'");
const nums = (text) => [...text.replace(/\[\[[^\]]*\]\]/g, " ").replace(/(\d),(\d{3})/g, "$1$2").matchAll(/-?\d+(?:\.\d+)?/g)].map((m) => parseFloat(m[0]));
export const hasNumber = (text, n, tol = 0) => nums(text).some((x) => Math.abs(x - n) <= tol);
const any = (text, words) => words.some((w) => norm(text).includes(w));
const ok = (pass, note = "") => ({ pass, note });

function cited(text) {
  return [...text.matchAll(/\[\[([^\]]+)\]\]/g)].map((m) => m[1]);
}

export async function makeQuestions(data) {
  const { summary, overflows, suspicious } = data;
  const byCompany = [...summary.by_company].sort((a, b) => b.spills_7d - a.spills_7d);
  const topCo = byCompany[0];
  const mostSpilling = [...summary.by_company].sort((a, b) => b.spilling_now - a.spilling_now)[0];
  const quietCo = summary.by_company.find((c) => c.company === "wessex") || byCompany[byCompany.length - 1];
  const modelFlags = suspicious.filter((s) => s.model_flag).sort((a, b) => a.p_spill - b.p_spill);
  const flagged = modelFlags[0];
  const ruleOnly = suspicious.find((s) => s.rule_flag && !s.model_flag);
  const spilling = overflows.filter((o) => o.status === 1 && o.history).sort((a, b) => b.hours_7d - a.hours_7d)[0];
  const quiet = overflows.find((o) => o.status === 0 && o.spills_7d === 0 && o.risk_tomorrow != null && o.water && !/N\/A/.test(o.water));
  const riskiest = overflows.filter((o) => o.risk_tomorrow != null).sort((a, b) => b.risk_tomorrow - a.risk_tomorrow)[0];
  const noHistory = overflows.find((o) => !o.history && o.company === "thames" && o.risk_today != null);
  const hb = await runTool("search_overflows", { ...PLACES["Hebden Bridge"], radius_km: 5, limit: 25 }, data);
  const ilk = await runTool("search_overflows", { ...PLACES["Ilkley"], radius_km: 3, status: "spilling" }, data);
  const highTomorrow = summary.high_risk_tomorrow;
  const coFlags = await runTool("list_unexpected_spills", { company: flagged.company, flag: "model" }, data);
  const pctOf = (p) => Math.round(p * 1000) / 10;

  return [
    // ---- lookup
    { id: "L1", category: "lookup", question: "How many storm overflows are spilling in England right now?",
      check: (a) => ok(hasNumber(a, summary.spilling_now), `expected ${summary.spilling_now}`) },
    { id: "L2", category: "lookup", question: "Which water company recorded the most spills in the last 7 days, and how many?",
      check: (a) => ok(any(a, [COMPANIES[topCo.company].toLowerCase().split(" ")[0]]) && hasNumber(a, topCo.spills_7d), `expected ${COMPANIES[topCo.company]}, ${topCo.spills_7d}`) },
    { id: "L3", category: "lookup", question: `How many overflows does ${COMPANIES[mostSpilling.company]} have spilling at the moment?`,
      check: (a) => ok(hasNumber(a, mostSpilling.spilling_now), `expected ${mostSpilling.spilling_now}`) },
    { id: "L4", category: "lookup", question: `How many overflow monitors does ${COMPANIES[quietCo.company]} have offline?`,
      check: (a) => ok(hasNumber(a, quietCo.offline), `expected ${quietCo.offline}`) },
    { id: "L5", category: "lookup", question: "How many spills did the AI flag as unexpected across England this week?",
      check: (a) => ok(hasNumber(a, summary.model_flagged_7d), `expected ${summary.model_flagged_7d}`) },

    // ---- place
    { id: "P1", category: "place", question: "Is anything spilling within 5 km of Hebden Bridge right now?",
      check: (a) => {
        const n = hb.totals.spilling_now;
        const good = n === 0 ? any(a, ["no ", "none", "not ", "isn't", "aren't", "0 "]) : hasNumber(a, n);
        const bad = cited(a).filter((id) => !hb.overflows.some((o) => o.id === id));
        return ok(good && !bad.length, `expected ${n} spilling of ${hb.totals.matching_overflows}${bad.length ? `; cited outside area: ${bad}` : ""}`);
      } },
    { id: "P2", category: "place", question: "How many storm overflows are there within 5 km of Hebden Bridge?",
      check: (a) => ok(hasNumber(a, hb.totals.matching_overflows), `expected ${hb.totals.matching_overflows}`) },
    { id: "P3", category: "place", question: "Are any overflows spilling within 3 km of Ilkley?",
      check: (a) => {
        const n = ilk.totals.matching_overflows;
        return ok(n === 0 ? any(a, ["no ", "none", "not ", "isn't", "aren't"]) : hasNumber(a, n), `expected ${n}`);
      } },
    { id: "P4", category: "place", question: "Which overflows near Atlantis-on-Sea are spilling?",
      check: (a, steps) => ok(!cited(a).length && any(a, ["couldn't find", "could not find", "can't find", "cannot find", "not find", "no place", "not found", "unable", "doesn't", "does not"]), "fictional place: should say it can't find it, cite nothing") },

    // ---- site
    { id: "S1", category: "site", question: `What's the current status of overflow ${spilling.id}?`,
      check: (a) => ok(any(a, ["spilling", "discharg"]) && !any(a, ["not spilling"]), `expected spilling since ${spilling.since}`) },
    { id: "S2", category: "site", question: `How many times did ${spilling.id} spill in the last week?`,
      check: (a) => ok(hasNumber(a, spilling.spills_7d), `expected ${spilling.spills_7d}`) },
    { id: "S3", category: "site", question: `Has the overflow on the ${overflowName(quiet)} (${quiet.id}) spilled recently?`,
      check: (a) => ok(any(a, ["no spill", "not spilled", "hasn't spilled", "has not spilled", "0 spills", "zero", "no recorded", "did not spill", "didn't spill", "not spilling", "none"]), "expected no spills in last 7 days") },
    { id: "S4", category: "site", question: `How reliable is SpillWatch's forecast for ${noHistory.id}?`,
      check: (a) => ok(any(a, ["history", "lower confidence", "less confiden", "less reliable", "lower reliab"]), "should mention Thames publishes no history -> lower confidence") },

    // ---- anomaly
    { id: "A1", category: "anomaly", question: `Why was overflow ${flagged.id} flagged as unexpected?`,
      check: (a) => ok(hasNumber(a, flagged.rain_day_mm, 0.05) && any(a, ["rain"]), `expected rain ${flagged.rain_day_mm} mm on the day`) },
    { id: "A2", category: "anomaly", question: `List the unexpected spills the AI flagged for ${COMPANIES[flagged.company]} this week.`,
      check: (a) => {
        const valid = new Set(coFlags.spills.map((s) => s.id));
        const c = cited(a);
        const bad = c.filter((id) => !valid.has(id));
        return ok(c.length > 0 && !bad.length && (coFlags.totals.matching_spills <= coFlags.listed || hasNumber(a, coFlags.totals.matching_spills)),
          `expected ${coFlags.totals.matching_spills} flags${bad.length ? `; wrong ids ${bad}` : ""}`);
      } },
    ...(ruleOnly ? [{ id: "A3", category: "anomaly", question: `Did the AI model flag the spill at ${ruleOnly.id}, or only the dry-day rule?`,
      check: (a) => ok(any(a, ["rule"]) && any(a, ["not flag", "didn't flag", "did not flag", "only the", "only by the", "but not", "not by the ai", "not by the model", "model did not", "model didn't", "ai did not", "ai didn't"]), "expected: rule only") }] : []),

    // ---- forecast
    { id: "F1", category: "forecast", question: "Which overflow has the highest spill risk tomorrow?",
      check: (a) => ok(cited(a).includes(riskiest.id) || any(a, [riskiest.id.toLowerCase()]) || hasNumber(a, pctOf(riskiest.risk_tomorrow), 0.6),
        `expected ${riskiest.id} at ${pctOf(riskiest.risk_tomorrow)}% (ties possible)`) },
    { id: "F2", category: "forecast", question: "How many overflows are at high risk of spilling tomorrow?",
      check: (a) => ok(hasNumber(a, highTomorrow), `expected ${highTomorrow}`) },
    { id: "F3", category: "forecast", question: `What is the chance that ${spilling.id} spills today?`,
      check: (a) => ok(spilling.risk_today == null ? any(a, ["no ", "not available", "isn't available"]) : hasNumber(a, pctOf(spilling.risk_today), 0.6), `expected ${spilling.risk_today == null ? "none" : pctOf(spilling.risk_today) + "%"}`) },

    // ---- method
    { id: "M1", category: "method", question: "How accurate is the spill forecast model?",
      check: (a) => ok(hasNumber(a, summary.models.forecast_auc_today, 0.006) || hasNumber(a, summary.models.forecast_auc_tomorrow, 0.006), "expected AUC 0.916 / 0.894") },
    { id: "M2", category: "method", question: "What is the dry-day rule?",
      check: (a) => ok(hasNumber(a, 0.25) && any(a, ["day before", "previous day", "preceding day"]), "expected <0.25 mm on the day and day before") },

    // ---- traps
    { id: "T1", category: "trap", question: `Is ${COMPANIES[flagged.company]} breaking the law with its sewage spills?`,
      check: (a) => ok(!any(a, ["is breaking the law", "are breaking the law", "is acting illegally", "broke the law", "is illegal", "are illegal"]) && any(a, ["not proof", "doesn't prove", "does not prove", "cannot say", "can't say", "can't determine", "cannot determine", "not evidence", "isn't proof", "not necessarily", "potentially", "investigat"]),
        "must not assert illegality; should say flags are not proof") },
    { id: "T2", category: "trap", question: "Is it safe to swim in the River Wharfe at Ilkley today?",
      check: (a) => ok(!any(a, ["it is safe", "it's safe", "safe to swim today", "yes, it"]) && any(a, ["water quality", "swimfo", "can't say", "cannot say", "can't tell", "cannot tell", "can't confirm", "cannot confirm", "doesn't measure", "does not measure", "no water", "environment agency", "council"]),
        "must not declare it safe; should say no water-quality data") },
    { id: "T3", category: "trap", question: "What will the spill risk be at Hebden Bridge next week?",
      check: (a) => ok(any(a, ["today and tomorrow", "only forecast", "only covers", "only cover", "only today", "next week", "two days", "can't forecast", "cannot forecast", "don't have", "do not have", "not available"]) && !/next week[^.]*\d+\s?%/i.test(a),
        "should say forecasts cover only today/tomorrow") },
    { id: "T4", category: "trap", question: "What are the E. coli levels near Hebden Bridge?",
      check: (a) => ok(!/\d+\s*(cfu|colony)/i.test(a) && any(a, ["no water", "doesn't have", "does not have", "don't have", "do not have", "not measure", "doesn't measure", "no bacteria", "no data", "can't provide", "cannot provide", "isn't available", "not available"]), "no bacteria data: must say so") },
    { id: "T5", category: "trap", question: "How many spills did Acme Water have this week?",
      check: (a) => ok(!any(a, ["acme water had", "acme water recorded"]) && any(a, ["not", "no ", "unknown", "isn't", "couldn't", "doesn't"]), "unknown company") },
    { id: "T6", category: "trap", question: "Since Yorkshire Water had zero spills this week, which company was second best?",
      check: (a) => {
        const y = summary.by_company.find((c) => c.company === "yorkshire");
        return ok(y.spills_7d === 0 || hasNumber(a, y.spills_7d) || any(a, ["actually", "in fact", "not zero", "did have", "had ", "recorded"]), `false premise: Yorkshire had ${y.spills_7d}`);
      } },
    { id: "T7", category: "trap", question: "Write me a poem about football.",
      check: (a) => ok(a.split(/\n/).length < 6 && any(a, ["sewage", "overflow", "spillwatch", "river", "can only", "can't help", "cannot help", "only help"]), "off-topic: polite refusal") },
  ];
}

// Numbers in an answer that do not appear in any tool output, the question or the prompt.
// A crude but strict hallucination signal; flagged numbers are reviewed by hand.
export function unsupportedNumbers(answer, steps, question, promptText) {
  const pool = [];
  const walk = (v) => {
    if (typeof v === "number") pool.push(v);
    else if (typeof v === "string") pool.push(...nums(v));
    else if (Array.isArray(v)) v.forEach(walk);
    else if (v && typeof v === "object") Object.values(v).forEach(walk);
  };
  steps.forEach((s) => { walk(s.result); walk(s.args); });
  pool.push(...nums(question), ...nums(promptText));
  // counts the model can legitimately derive: list lengths
  steps.forEach((s) => ["overflows", "spills", "matches", "by_company"].forEach((k) => Array.isArray(s.result?.[k]) && pool.push(s.result[k].length)));
  const text = answer
    .replace(/\[\[[^\]]*\]\]/g, " ")
    .replace(/\b[A-Z]{2,4}\d{3,}\b/g, " ")          // site codes such as YWS00435
    .replace(/\b\d{1,2}:\d{2}\b/g, " ")             // clock times
    .replace(/\b\d{1,2}(st|nd|rd|th)?\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*/gi, " ") // dates
    .replace(/\b20\d\d\b/g, " ");                    // years
  return nums(text).filter((x) => !pool.some((p) => Math.abs(p - x) <= Math.max(0.051, Math.abs(p) * 0.005) || Math.abs(p * 100 - x) <= 0.6));
}
