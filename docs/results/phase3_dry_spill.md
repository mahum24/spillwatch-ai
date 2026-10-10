# Phase 3 results: suspicious-spill detection (RQ1)

*Generated from `data/models/dry_spill/metrics.json` on 2026-10-09. Re-run with `spillwatch build-research && spillwatch train`.*

## Research question
**RQ1:** Does a rainfall-conditioned ML model identify suspicious (weather-independent) storm overflow spills more effectively than the fixed dry-day rule used in public analyses?

## Data
| | |
|---|---|
| Sites | 8,372 overflows across 5 companies (northumbrian, severn_trent, united_utilities, wessex, yorkshire) |
| Spill history | ~1.8M raw EDM events → merged into continuous spells (gaps ≤ 30 min) |
| Rainfall | EA Hydrology daily totals, 991 gauges, 2020–2026; ≤ 3 gauges within 10 km per site |
| Unit of analysis | site × rain day (09:00–09:00 GMT); label = a spill started that day |
| Train | rain days before 2025-07-01: 4,606,648 site-days, 284,631 spill starts |
| Test (held out, later in time) | 1,449,797 site-days, 88,305 spill starts |

Model: gradient-boosted trees (scikit-learn `HistGradientBoostingClassifier`) with monotonic constraints (more rain can only increase spill probability). Features: rain on the day, previous day, 3/7/14/30-day totals, max day in 7 days, season, and two site features computed **from wet-weather days only and out-of-fold**, so habitual dry spilling is never learned as "normal" for a site.

## Result 1: the model understands normal (rain-driven) spilling
| Method | ROC AUC (test) |
|---|---|
| Dry-day rule as a predictor | 0.683 |
| Rain on day + previous day | 0.880 |
| **Model** | **0.927** (PR AUC 0.577 vs base rate 0.061) |
| Model, sites seen in training | 0.945 |
| Model, sites never seen in training | 0.899 |

## Result 2: detection of injected weather-independent spills
No ground-truth labels for illegal spills exist, so 20,000 synthetic spills were injected on random held-out site-days and mixed with 88,305 real spills. Every method flags the same share (8.04%, the rule's natural rate).

**Overall recall is a tie** (rule 37.8%, model 37.8%), because random injections fall mostly on dry days, where any "less rain = more suspicious" method succeeds. Broken down by weather on the injected day:

| Weather band | Injected | Real spills | Rule recall | Model recall | Rain-ranking recall | Real spills flagged (rule / model) |
|---|---|---|---|---|---|---|
| dry (<0.25 mm both days) | 7,560 | 1,145 | 100.0% | **64.2%** | 98.6% | 1,145 / 375 |
| light (<25% of site's usual spill rain) | 8,872 | 23,702 | 0.0% | **29.9%** | 1.5% | 0 / 760 |
| moderate (25-100% of usual) | 3,166 | 41,079 | 0.0% | **1.7%** | 0.0% | 0 / 5 |
| wet (>= site's usual spill rain) | 402 | 22,379 | 0.0% | **0.7%** | 0.0% | 0 / 2 |

**Finding:** the rule is blind by construction to spills after light rain that is far below what a site normally needs to spill; the model recovers ~30% of these. In exchange it deprioritises some dry-day spills.

## Result 3: where the methods disagree on real spills
| | Rule only (n=770) | Model only (n=767) |
|---|---|---|
| Median rain on day / previous day | 0.0 / 0.0 mm | 0.6 / 0.9 mm |
| Median rain in previous 14 days | 46 mm | 23 mm |
| Site's usual 3-day rain when spilling | 16 mm | 36 mm |
| Model probability of spilling | 0.98% | 0.16% |

Rule-only flags are mostly spills soon after heavy rain (plausibly network drain-down). Model-only flags are spills at "insensitive" sites after only a trace of rain.

## Threats to validity
1. **No ground truth.** Injection measures sensitivity to weather-independent spills, not illegality. Real illegal spills may cluster on dry days, which favours the rule.
2. **Band definition uses a model feature** (site's usual spill rain), which partly favours the model in the band breakdown.
3. **Sites with no spills in a period are absent** from the history files, so the panel slightly over-represents spill-prone sites.
4. **Monitor downtime is unknown** in the history: a "no spill" day may be an offline monitor.
5. Northumbrian timestamps are assumed to be UK local time (not documented by the publisher).
6. Training reached the iteration cap (400) without early stopping; more capacity may help slightly.

## Next steps
- Hybrid detector: rule ∪ model light-rain flags; evaluate at matched budgets.
- External validation: compare flagged sites with EA annual-return fields (`highSpillOperationalReview`, investigations).
- Apply the model to live spills in the frontend (Phase 6).
