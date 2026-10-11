// The question-answering loop: send the conversation, run any tools the model asks for,
// send the results back, until the model replies in text. Shared by the browser and the
// evaluation harness; only `callModel` (how the request reaches Gemini) differs.

import { runTool } from "./tools.js";

const MAX_STEPS = 6;

export class ModelError extends Error {
  constructor(message, { status, retryAfter } = {}) {
    super(message);
    this.status = status;
    this.retryAfter = retryAfter;
  }
}

// Calls Gemini directly. Used with a visitor's own key and by the evaluation.
export function directGemini({ apiKey, model, fetchFn = fetch }) {
  return async (body) => {
    const url = `https://generativelanguage.googleapis.com/v1beta/models/${encodeURIComponent(model)}:generateContent`;
    const r = await fetchFn(url, { method: "POST", headers: { "content-type": "application/json", "x-goog-api-key": apiKey }, body: JSON.stringify(body) });
    return parseGemini(r);
  };
}

// Calls the SpillWatch Worker, which adds the shared key, prompt and tools itself.
export function viaWorker({ url, fetchFn = fetch }) {
  return async (body) => {
    const r = await fetchFn(url, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ contents: body.contents }) });
    return parseGemini(r);
  };
}

async function parseGemini(r) {
  let j = null;
  try { j = await r.json(); } catch { /* non-JSON error page */ }
  if (!r.ok) {
    const retry = j?.error?.details?.find((d) => d.retryDelay)?.retryDelay;
    throw new ModelError(j?.error?.message || `HTTP ${r.status}`, { status: r.status, retryAfter: retry ? parseFloat(retry) : undefined });
  }
  const cand = j?.candidates?.[0];
  if (!cand?.content?.parts?.length) {
    throw new ModelError(`The model returned no answer (${cand?.finishReason || j?.promptFeedback?.blockReason || "empty"}).`);
  }
  return { content: { role: "model", parts: cand.content.parts }, usage: j.usageMetadata || {} };
}

/**
 * Answer one question.
 * @param history   prior turns (Gemini `contents`), extended in place
 * @param callModel (requestBody) => {content, usage}
 * @param buildBody (contents) => requestBody
 * @param data      {overflows, suspicious, summary}
 * @returns {text, steps: [{tool, args, result}], modelCalls, usage}
 */
export async function ask(question, { history, callModel, buildBody, data, env = {}, onStep }) {
  const start = history.length;
  history.push({ role: "user", parts: [{ text: question }] });
  const steps = [];
  const usage = { prompt: 0, output: 0 };
  try {
    for (let i = 0; i < MAX_STEPS; i++) {
      const { content, usage: u } = await callModel(buildBody(history));
      usage.prompt += u.promptTokenCount || 0;
      usage.output += (u.candidatesTokenCount || 0) + (u.thoughtsTokenCount || 0);
      history.push(content);
      const calls = content.parts.filter((p) => p.functionCall);
      if (!calls.length) {
        const text = content.parts.filter((p) => p.text && !p.thought).map((p) => p.text).join("").trim();
        return { text, steps, modelCalls: i + 1, usage };
      }
      const responses = [];
      for (const { functionCall: fc } of calls) {
        onStep?.(fc.name, fc.args || {});
        const result = await runTool(fc.name, fc.args || {}, data, env);
        steps.push({ tool: fc.name, args: fc.args || {}, result });
        responses.push({ functionResponse: { name: fc.name, ...(fc.id && { id: fc.id }), response: result } });
      }
      history.push({ role: "user", parts: responses });
    }
    throw new ModelError("The assistant needed too many steps for this question. Try asking something more specific.");
  } catch (err) {
    history.length = start; // leave the conversation as it was, so the user can retry
    throw err;
  }
}

// Keep the conversation small: drop the oldest whole questions beyond `keep`.
export function trimHistory(history, keep = 4) {
  const starts = history.map((c, i) => (c.role === "user" && c.parts.some((p) => p.text) ? i : -1)).filter((i) => i >= 0);
  if (starts.length > keep) history.splice(0, starts[starts.length - keep]);
}
