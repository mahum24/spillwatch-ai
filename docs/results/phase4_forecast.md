# Phase 4 results: spill forecasting (RQ2)

*Generated from `data/models/forecast/metrics.json` on 2026-10-10. Re-run with `spillwatch forecast-history && spillwatch train-forecast`.*

## Research question
**RQ2:** How accurately can storm overflow spilling be predicted 1–2 days ahead from rainfall forecasts, and how much skill is lost because real forecasts are imperfect?

## Set-up
- **Target:** is a site spilling at any point during a rain day (09:00–09:00 GMT)? This includes spills continuing from the day before, which is what matters to swimmers. Test-period active rate: 8.5% of site-days.
- **Issue time:** 09:00 GMT, when the previous rain day is fully observed. Horizon 1 = the rain day starting now ("today/tonight"); horizon 2 = the next one ("tomorrow").
- **Known at issue time:** observed rainfall (last day, 7/14/30-day totals), whether the site was spilling on the last observed day, site behaviour (out-of-fold, training years only), season.
- **Future rain:** trained on observed rain ("perfect prognosis", standard in hydrological forecasting), tested with **archived real forecasts** (Open-Meteo Previous Runs API: what the weather model predicted ~24 h / ~48 h beforehand) on a 0.25° grid (158 cells).
- **Train:** rain days before 2025-07-01. **Test:** 2025-07-01 to 2026-08-31, 1,486,260 site-days, 5 companies.
- Warnings are issued on the 10% of site-days with the highest predicted risk (slightly above the 8.5% base rate).

## Results
| Method | 1-day AUC | 1-day PR-AUC | 1-day hit rate / false alarms | 2-day AUC | 2-day PR-AUC | 2-day hit rate / false alarms |
|---|---|---|---|---|---|---|
| climatology (site rate) | 0.696 | 0.180 | 27.2% / 77.0% | 0.694 | 0.180 | 27.1% / 77.1% |
| persistence (active on last observed day) | 0.792 | 0.379 | 50.7% / 57.0% | 0.757 | 0.298 | 41.7% / 64.7% |
| model, no rain forecast | 0.845 | 0.454 | 51.7% / 56.2% | 0.807 | 0.352 | 43.1% / 63.5% |
| **model + archived forecast (realistic)** | **0.916** | **0.577** | **60.4% / 48.9%** | **0.894** | **0.496** | **54.5% / 53.8%** |
| model + observed rain (oracle) | 0.951 | 0.683 | 68.9% / 41.6% | 0.939 | 0.635 | 65.1% / 44.8% |

**Findings**
1. Every step adds skill in the expected order: climatology < persistence < model without rain forecast < model with a real forecast < model with perfect rain, at both horizons.
2. With **real archived forecasts**, the model captures **67%** (1 day) and **66%** (2 days) of the improvement that perfect rainfall knowledge would give over no forecast.
3. At a 10% warning rate, a warning catches about 60% of spill-days at 1 day ahead; roughly half of warnings are false alarms. Persistence alone ("it was spilling yesterday") already catches about half, so the forecast adds most value at the *start* of spill episodes.

## How good were the rain forecasts?
| | Correlation with gauge rain | Mean forecast vs observed | Wet days (≥1 mm) forecast as wet |
|---|---|---|---|
| ~24 h ahead | 0.73 | 2.81 vs 2.67 mm | 83% |
| ~48 h ahead | 0.68 | 3.01 vs 2.67 mm | 84% |

Forecasts slightly over-predict rainfall and lose accuracy with lead time, which explains the gap between the realistic and oracle results.

## Threats to validity
1. **Conservative lead times:** the horizon-1 forecast uses the run issued ~24 h before each hour, not the freshest run; real-time performance may be slightly better.
2. **Grid mismatch:** forecasts are for a 0.25° cell; observed rain comes from gauges within 10 km.
3. **Perfect-prognosis training** never sees forecast errors; training on archived forecasts (available from 2024) could improve calibration.
4. Same data caveats as Phase 3: unknown monitor downtime; sites with no spills in a period are missing.

## Use in the product
Horizon-1 and horizon-2 models are saved in `data/models/forecast/`. With the current forecast from the Open-Meteo forecast API, they produce the "Is it safe to swim tomorrow?" risk for each overflow (Phase 6 frontend).
