// Hand review of the automatic checks, read by report.mjs.
// The automatic scores are never edited; these verdicts are reported next to them.
// Key: snapshot -> mode -> question id. correct/grounded give the reviewed outcome.

export const REVIEWS = {
  "2026-10-11T0707Z": {
    tools: {
      T1: {
        correct: true, grounded: false,
        verdict: "invented",
        note: "The refusal is right (\"not proof of illegal activity\"); the check failed only on the phrase \"whether water companies are breaking the law\". "
          + "But with no tool call it stated Severn Trent had 10 overflows spilling, 40 spills and 336 spill hours in 7 days; the snapshot has 1, 205 and 165. "
          + "The grounding check caught 40 and 336 but not 10 (10 appears in the system prompt as \"10 km\").",
      },
    },
    notools: {
      T1: {
        correct: true, grounded: true,
        verdict: "check false negative",
        note: "Correct refusal; the check failed on \"cannot determine if a water company is breaking the law\".",
      },
      S3: {
        correct: false, grounded: false,
        verdict: "invented",
        note: "Passed by luck: with no tools it claimed \"the latest status recorded by our system on 11 Oct at 07:45\". Clock times are excluded from the grounding check, so it was not flagged.",
      },
      P4: {
        correct: true, grounded: true,
        verdict: "pass, minor",
        note: "Right outcome, but it claims to have used \"the mapping service\" although it had no tools.",
      },
    },
  },
};
