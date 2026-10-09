# SpillWatch AI

**Using AI to detect suspicious sewage discharges and forecast safe swimming conditions in England's rivers and seas.**

England's storm overflows spilled 291,492 times in 2025, for nearly 1.9 million hours. Spills are only permitted during heavy rainfall: discharging in dry weather is illegal and especially harmful. Since 2024 every water company publishes near-real-time overflow status, but nothing flags which spills are *suspicious* or warns people *before* a spill happens. SpillWatch AI aims to do both.

## Status

| Phase | Scope | Status |
|---|---|---|
| 1. Data pipeline | Live overflow + rainfall ingestion, DuckDB warehouse, spill events, rain-gauge matching, baseline dry-spill rule | ✅ Done |
| 2. Orchestration & quality | Scheduled collection, 11 data-quality checks, collector gaps, monitor status changes | ✅ Done |
| 3. AI: suspicious-spill detection | Model of expected spilling given rainfall (AUC 0.927), evaluated against the dry-day rule. [Results](docs/results/phase3_dry_spill.md) | ✅ First version |
| 4. AI: spill forecasting | 24–48h spill probability from rainfall forecasts | ⏳ Next |
| 5. AI: river assistant | LLM answering questions over the warehouse | |
| 6. Frontend | Live map, suspicious spills, swim-safety forecast, impact dashboard | |

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

Spill history only exists if the feeds are polled regularly. To collect every 15 minutes with cron (`crontab -e`):

```
7,22,37,52 * * * * "/Users/mac/Documents/Advance Practice/spillwatch-ai/scripts/collect.sh"
```

## Research pipeline (Phase 3)

Kept in a separate `data/research.duckdb` so it never blocks the live collector.

```bash
.venv/bin/spillwatch history          # ~1.8M historical spill events from 8 company datasets
.venv/bin/spillwatch rain-history     # daily rainfall, 991 EA gauges, 2020 onwards
.venv/bin/spillwatch build-research   # merge events into spells, build the site-day table
.venv/bin/spillwatch train            # train + evaluate; writes data/models/dry_spill/
```

## Data quality

After every collection, 11 checks run (`src/spillwatch/quality.py`) and are stored in `dq_results`: feed completeness, row-count stability, duplicate ids, coordinate validity, offline monitors, inconsistent event times, future timestamps, rainfall freshness and coverage. `fct_collection_gaps` records any time the collector missed a run, so the completeness of spill history is always known.

## Known limitations

- An event that starts *and* ends between two polls, followed by another event before the next poll, would be missed. 15-minute polling keeps this rare; it will be quantified in Phase 2.
- Monitors go offline (≈2% at any time). Offline periods are tracked in `dim_overflows.offline_ratio`.
- A flagged spill is **potentially suspicious**, not proven illegal: monitors can malfunction, and the nearest gauge may miss local rain.

## Ethics

Only public, non-personal open data is used. Outputs describe spills as "potentially suspicious" and state the model's uncertainty. No individual or company is accused of wrongdoing.
