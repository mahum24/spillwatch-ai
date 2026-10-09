# Decision log

Record significant technical decisions here, with the evidence behind them. This is direct evidence for the ICA learning outcome *"use judgement to make evidence-based decisions"*.

---

### 2026-10-09 — Data source for overflow status
**Decision:** Ingest each water company's ArcGIS "Storm Overflow Activity" feed directly.
**Evidence:** Probed 8 feeds: all share the same 10-field schema (differing only in casing) and together cover ~13,200 overflows.
**Alternatives:** Scraping the National Hub map (fragile); annual EDM returns only (aggregates per year, no individual events).

### 2026-10-09 — Build history by polling snapshots
**Decision:** Poll every 15 minutes and store every snapshot, then derive events in SQL.
**Evidence:** Feeds expose only current status and the latest event. No event history endpoint exists.
**Trade-off:** Very short back-to-back events can be missed. Accepted and documented; to be measured later.

### 2026-10-09 — DuckDB as the warehouse
**Decision:** DuckDB file + parquet landing zone.
**Evidence:** No server or Docker needed; filters a 150 MB rainfall archive CSV in ~5 s; SQL skills transfer to Snowflake/BigQuery.
**Revisit if:** the frontend needs concurrent writes (then Postgres + PostGIS).

### 2026-10-09 — Rainfall archive ingestion
**Decision:** Download each day's archive to a temp file and filter with DuckDB.
**Evidence:** Streaming the CSV through pandas failed ("I/O operation on closed file") and was slower. The DuckDB approach loads ~88k readings/day in ~5 s.

### 2026-10-09 — Linking overflows to rainfall
**Decision:** Average of the ≤3 nearest EA gauges within 10 km (haversine).
**Evidence:** 12,458 of 13,205 overflows (94%) have a gauge within 10 km; mean nearest distance 4.7 km.
**Open question:** compare against gridded rainfall (e.g. Open-Meteo / HadUK) for the 6% with no gauge.

### 2026-10-09 — Baseline dry-spill rule
**Decision:** < 0.25 mm rain in the 48 h before plus during a spill = "dry spill (rule)". Missing rain data → NULL, never TRUE.
**Why:** Mirrors the simple threshold heuristics used in public analyses. This is the benchmark the ML model must beat (RQ1).
**To do:** cite the exact threshold definition used by the EA / campaigners in the literature review.

### 2026-10-09 — Scheduling with cron (for now)
**Decision:** cron runs `scripts/collect.sh` every 15 minutes on the laptop; a daily 06:00 run re-loads the last 2 days of rainfall archive to fill gaps.
**Evidence:** Zero extra infrastructure; the whole run takes ~10 s. Collector gaps are measured in `fct_collection_gaps`, so downtime is visible, not silent.
**Revisit when:** the frontend goes live — then move collection to a cloud scheduler so it runs 24/7.

### 2026-10-09 — Data-quality checks after every run
**Decision:** 11 SQL checks with pass/warn/fail thresholds (`src/spillwatch/quality.py`), stored in `dq_results`.
**Evidence:** The first run found 5 overflows whose latest event end precedes its start (stale end times in the source feeds). Checks also cover feed completeness, row-count drops, coordinates, freshness and rainfall coverage (77% of expected 15-min readings over 7 days).
**Why it matters for the AI:** a dry-spill prediction is only as trustworthy as the data behind it. DQ history will be used to exclude unreliable sites and periods from training.
