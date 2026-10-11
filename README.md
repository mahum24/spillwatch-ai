# SpillWatch AI

**Using AI to detect suspicious sewage discharges and forecast safe swimming conditions in England's rivers and seas.**

🌊 **Live site: [mahum24.github.io/spillwatch-ai](https://mahum24.github.io/spillwatch-ai/)**

England's storm overflows spilled 291,492 times in 2025, for nearly 1.9 million hours. Spills are only permitted during heavy rainfall: discharging in dry weather is illegal and especially harmful. Since 2024 every water company publishes near-real-time overflow status, but nothing flags which spills are *suspicious* or warns people *before* a spill happens. SpillWatch AI aims to do both.

## Status

| Phase | Scope | Status |
|---|---|---|
| 1. Data pipeline | Live overflow + rainfall ingestion, DuckDB warehouse, spill events, rain-gauge matching, baseline dry-spill rule | ✅ Done |
| 2. Orchestration & quality | Scheduled collection, 11 data-quality checks, collector gaps, monitor status changes | ✅ Done |
| 3. AI: suspicious-spill detection | Model of expected spilling given rainfall (AUC 0.927), evaluated against the dry-day rule. [Results](docs/results/phase3_dry_spill.md) | ✅ First version |
| 4. AI: spill forecasting | 1–2 day spill risk; AUC 0.916 / 0.894 with real archived forecasts. [Results](docs/results/phase4_forecast.md) | ✅ First version |
| 5. AI: river assistant | Gemini answering questions through five tools over the live data, with a test suite of 28 questions including trap questions. [Details](#assistant-phase-5) | 🚧 Built, evaluation pending |
| 6. Frontend | Live map: status now, AI-flagged unexpected spills with explanations, risk today/tomorrow, near-me search. [Live](https://mahum24.github.io/spillwatch-ai/) | ✅ First version |

## Architecture (Phase 1)

```
9 water-company ArcGIS feeds ─┐                     ┌─ dim_overflows
  (~14,200 overflows, live)   ├─► raw parquet ──►   ├─ fct_spill_events
EA rainfall API + archive ────┘   landing zone      ├─ bridge_overflow_gauge (≤3 gauges within 10 km)
  (~1,000 gauges, 15-min)          + DuckDB raw     └─ fct_event_rainfall (+ baseline dry-spill rule)
```

- **Raw landing zone** (`data/raw/…/date=YYYY-MM-DD/*.parquet`): an immutable copy of every pull, so the warehouse can always be rebuilt.
- **Idempotent loads**: primary keys + `INSERT OR IGNORE`, so re-running any job never duplicates data.
- **Event reconstruction**: the feeds only expose current state, so spill history is rebuilt from repeated snapshots.
- **Missing data ≠ dry**: an event with no rain-gauge coverage gets `is_dry_spill_rule = NULL`, never `TRUE`.

## Data sources

| Source | What | Licence |
|---|---|---|
| Water company Storm Overflow Activity feeds (National Storm Overflow Hub) | Live status and latest event for each overflow | Open data |
| [Environment Agency flood-monitoring API](https://environment.data.gov.uk/flood-monitoring/doc/reference) | 15-minute rainfall from ~1,000 gauges, plus daily archive | Open Government Licence v3 |

Covers all 9 English water and sewerage companies (~14,200 overflows). Not yet covered: Welsh Water / Hafren Dyfrdwy.

## Quick start

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"

.venv/bin/spillwatch stations               # rain gauge locations
.venv/bin/spillwatch collect                # one live snapshot of overflows + rainfall
.venv/bin/spillwatch backfill-rain --days 30
.venv/bin/spillwatch transform              # build modelled tables
.venv/bin/spillwatch check                  # data-quality checks
.venv/bin/spillwatch health                 # pipeline + data-quality status
.venv/bin/spillwatch summary

.venv/bin/pytest
```

### Collect continuously

Spill history only exists if the feeds are polled regularly, so collection runs in two places:

- **Cloud (primary):** the [`Collect overflow data`](.github/workflows/collect.yml) GitHub Actions workflow runs every 15 minutes and commits changed overflows (plus a weekly full keyframe) to the `data` branch. Pull it into the local warehouse with `spillwatch sync-cloud`.
- **Laptop (backup):** cron runs `scripts/collect.sh` at :07/:22/:37/:52 while the machine is awake:

```
7,22,37,52 * * * * "/path/to/spillwatch-ai/scripts/collect.sh"
```

## Research pipeline (Phase 3)

Kept in a separate `data/research.duckdb` so it never blocks the live collector.

```bash
.venv/bin/spillwatch history          # ~1.8M historical spill events from 8 company datasets
.venv/bin/spillwatch rain-history     # daily rainfall, 991 EA gauges, 2020 onwards
.venv/bin/spillwatch build-research   # merge events into spells, build the site-day table
.venv/bin/spillwatch train            # suspicious-spill model; writes data/models/dry_spill/
.venv/bin/spillwatch forecast-history # archived rain forecasts for the test period (Open-Meteo)
.venv/bin/spillwatch train-forecast   # 1-2 day spill forecasting; writes data/models/forecast/
```

## Website (Phase 6)

`spillwatch publish` scores live data with both models and writes JSON to `site/data/`; `scripts/deploy_site.sh` publishes `site/` to the `gh-pages` branch (GitHub Pages). It is a static site (Leaflet + vanilla JS), so there is no server to run. Shareable links: `#view=suspicious&site=<overflow id>`.

In production the [`Score and publish site`](.github/workflows/publish.yml) workflow does this hourly in GitHub Actions: it rebuilds a warehouse from the `data` branch plus the live feeds, downloads the trained models from the [`models-v1` release](https://github.com/mahum24/spillwatch-ai/releases/tag/models-v1), scores, and deploys to GitHub Pages. `scripts/deploy_site.sh` remains for manual deploys from a laptop.

## Assistant (Phase 5)

"Ask SpillWatch AI" on the website answers plain-English questions ("Is anything spilling near Hebden Bridge?", "Why was this overflow flagged?") using Google Gemini with **function calling**. The model never sees the raw data: it can only call five tools that run in the visitor's browser over the data the map has loaded:

| Tool | Does |
|---|---|
| `geocode` | Place name or postcode → coordinates (postcodes.io, open data) |
| `get_summary` | National or per-company totals, feed freshness, model scores |
| `search_overflows` | Filter by distance, company, river, status or risk; returns totals for all matches |
| `get_overflow` | One overflow's status, history, risk and caveats |
| `list_unexpected_spills` | AI-flagged / dry-day-rule spills with rainfall and explanations |

Answers cite overflows as `[[id]]`, which become "show on map" links, and every answer shows the steps it took.

**Keys.** Questions go to a small [Cloudflare Worker](worker/src/index.js) that holds a free-tier Gemini key as a secret. The Worker builds the prompt and tool list itself (callers can only send the conversation), allows only the site's origin and rate-limits each visitor, so the key cannot be reused as a free general-purpose proxy. Visitors can also add their own free Gemini key in the assistant's settings; it is stored only in their browser and sent straight to Google.

**One implementation everywhere.** [`site/assistant/tools.js`](site/assistant/tools.js) (tools, prompt, request) and [`agent.js`](site/assistant/agent.js) (the tool loop) are shared by the website, the Worker and the evaluation harness.

**Evaluation (RQ3).** [`eval/assistant/`](eval/assistant/) asks 28 questions in 7 categories (lookups, places, single sites, anomalies, forecasts, method, and *traps*: legality, swimming safety, next week, E. coli, an unknown company, a false premise, off-topic). Expected answers are computed from a frozen data snapshot with the same tool code. Each answer is scored for correctness and **grounding**: every number in it must appear in a tool output. The same questions run with no tools give the ungrounded baseline.

```bash
npm test                                            # tool, loop and Worker tests (no key needed)
GEMINI_API_KEY=... npm run eval:assistant           # or put GEMINI_API_KEY=... in .env
npm run eval:assistant -- --mode notools            # baseline without tools
```

Deploying the Worker: `cd worker && npx wrangler login && npx wrangler secret put GEMINI_API_KEY && npx wrangler deploy`, then put its URL in the `spillwatch-assistant` meta tag in `site/index.html`.

## Data quality

After every collection, 11 checks run (`src/spillwatch/quality.py`) and are stored in `dq_results`: feed completeness, row-count stability, duplicate ids, coordinate validity, offline monitors, inconsistent event times, future timestamps, rainfall freshness and coverage. `fct_collection_gaps` records any time the collector missed a run, so the completeness of spill history is always known.

## Known limitations

- An event that starts *and* ends between two polls, followed by another event before the next poll, would be missed. 15-minute polling keeps this rare; it will be quantified in Phase 2.
- Monitors go offline (≈2% at any time). Offline periods are tracked in `dim_overflows.offline_ratio`.
- A flagged spill is **potentially suspicious**, not proven illegal: monitors can malfunction, and the nearest gauge may miss local rain.

## Ethics

Only public, non-personal open data is used. Outputs describe spills as "potentially suspicious" and state the model's uncertainty. No individual or company is accused of wrongdoing.
