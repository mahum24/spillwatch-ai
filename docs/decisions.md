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
