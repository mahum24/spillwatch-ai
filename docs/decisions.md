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

### 2026-10-09 — Hardening ingestion after three real failures
Three problems surfaced within an hour of running against live data. Each was found by the data-quality checks or row-count reconciliation, not by luck.

1. **Severn Trent listed 412 sites twice** (same site id, different row ids and `LastUpdated`) during a refresh.
   *First diagnosis (wrong):* overlapping pagination. *Evidence that disproved it:* all 2,416 rows had unique ArcGIS object ids.
   *Fix:* pagination is now validated on unique object ids vs the server's count (refetch up to 3×), and duplicate sites are resolved by keeping the most recently updated row. Anglian has 1 genuine duplicate in every run, handled the same way.
2. **Timestamp conversion crashed the whole run.**
   *First diagnosis (wrong):* garbage timestamps from a feed mid-refresh. *Evidence that disproved it:* all values were in range; only one of several float columns with NaNs failed.
   *Root cause:* pandas 2.3.3 / numpy 2.2 raise a spurious `FloatingPointError` when converting float epoch-ms with NaN. *Fix:* convert via nullable `Int64`; out-of-range values still become NaT as a safeguard.
3. **One company's failure stopped every company.** *Fix:* fetch + normalise are isolated per company; a failure is logged and caught by the `companies_in_latest_snapshot` check.

**Lesson (for reflection):** twice my first hypothesis was wrong; checking it against the data before fixing avoided shipping the wrong fix.

### 2026-10-09 — Added Southern Water
Found its live feed (994 overflows), bringing coverage to all 9 English water and sewerage companies (14,199 overflows). Added a `stale_company_feeds` check because its `LastUpdated` appeared months old when first probed.

### 2026-10-09 — Incomplete feeds during company reloads (South West Water)
**Observed:** At 22:30 South West returned exactly 1,000 of 1,342 sites, and the server's own count also said 1,000, so the pagination check could not catch it.
**Evidence:** curl returned 1,342 while Python returned 1,000 at the same moment. Response headers showed a 5-minute ArcGIS CDN cache (`max-age=300`, `x-cache: TCP_HIT`); a `no-cache` request returned 1,342. Python and curl encode the query differently, so they hit different cache entries, and ours held a copy taken mid-reload. The feed also stayed genuinely short for several minutes, then recovered to 1,342.
**Fixes:**
1. Every request sends `Cache-Control: no-cache`.
2. Each company is compared with its median site count over the last 24 h. Below 95% → retry after 30 s and 60 s, then accept with a warning (a company may genuinely remove sites).
**Implication for modelling:** snapshots where a company is below 95% of typical size are flagged in the logs and must be excluded when deriving training labels; otherwise missing sites look like "not spilling".

### 2026-10-09 — Phase 3 training data: event history instead of waiting
**Decision:** Train on published event-level EDM history (Northumbrian 2020–24, United Utilities 2023–25, Severn Trent 2025, Wessex 2025–26, Yorkshire 2026; ~1.8M raw events) rather than waiting for our own live history.
**Evidence:** Found via ArcGIS catalogue search; site ids match the live feeds (except Northumbrian, which needs spatial matching).

### 2026-10-09 — Rainfall source for training: EA Hydrology API (daily)
**Decision:** Daily, quality-checked gauge totals from the Hydrology API (991 gauges, 2020–2026).
**Evidence:** The flood-monitoring archive used for live data only goes back ~10 months (404 before Dec 2025). Hydrology daily data covers every history source.
**Problem met:** 756 of 995 gauge downloads returned `403 Forbidden` within one second: rate limiting from 4 parallel requests, not real access denial (the same URL returned 200 moments later). **Fix:** 2 workers, 0.3 s pause, exponential back-off on 403/429.

### 2026-10-09 — Separate research database
**Decision:** `research.duckdb`, built from parquet, separate from the live warehouse.
**Why:** DuckDB allows one writer; long research jobs must never block the 15-minute collector.

### 2026-10-09 — Harmonising how companies count spills
**Decision:** Merge events per site separated by ≤ 30 min into continuous spells (gaps-and-islands SQL).
**Evidence:** Median raw event length was 0.25 h for Northumbrian (15-minute blocks) vs ~3 h for Yorkshire; Wessex had 8.5 raw rows per spell. Without merging, one long spill would look like many spills, and a spill crossing 09:00 would look like a new spill the next day.

### 2026-10-09 — United Utilities site-id change
**Decision:** Map 2023 ids (`UUG…`) to 2024+ ids (`UUP…`) by location, one-to-one matches within 1 m only.
**Evidence:** 1,884 of 1,944 old ids sit within 1 m of a new id; 1,693 are unambiguous one-to-one matches. Without this, United Utilities appeared to have 4,034 sites (live feed: 2,255); after: 2,341.

### 2026-10-09 — Avoiding the "normalisation trap"
**Decision:** Site features use wet-weather days only, computed out-of-fold; company is not a feature.
**Why:** If the model learned each site's overall spill behaviour, a site that habitually spills in dry weather would look "normal" and never be flagged. Tests prove dry-day spills do not change site features and that no row sees its own month's labels.

### 2026-10-09 — Evaluating without ground truth
**Decision:** Inject synthetic weather-independent spills into held-out data; compare methods at the same flagging budget; report recall broken down by weather band.
**Evidence:** Overall recall was a tie (~38% for rule, model and rain-ranking), because random injections fall mostly on dry days. The weather-band breakdown revealed the real difference: in light rain the rule recovers 0% by design, the model ~30%. Full results: `docs/results/phase3_dry_spill.md`.

### 2026-10-09 — Collect at :07/:22/:37/:52, not on the quarter hour
**Evidence:** South West Water's feed failed at 22:30 (1,000 of 1,342 sites) and 23:00 (count 1,342 but 0 rows returned), both exactly on the half hour, and was fine minutes later. This suggests they reload on a :00/:30 schedule.
**Decision:** Offset collection by 7 minutes to avoid reload windows. Watch `companies_in_latest_snapshot` to confirm the failures stop.

### 2026-10-10 — Honest forecast evaluation with archived forecasts
**Decision:** Train on observed rain (perfect prognosis), test with archived real forecasts from the Open-Meteo Previous Runs API (archive from Jan 2024), and report an oracle upper bound and a no-forecast baseline.
**Why:** Testing with the rain that actually fell would overstate real-world skill (AUC 0.951 oracle vs 0.916 with real forecasts at 1 day).
**Constraint:** Free tier 5,000 weighted calls/hour; ~14 months per location ≈ 31 calls. A 0.25° grid gives 158 cells (~4,900 calls), paced at one request per 4 s.
**Result:** Real forecasts capture about two-thirds of the possible gain from rainfall information at both horizons. See `docs/results/phase4_forecast.md`.

### 2026-10-10 — Target for forecasting is "spill active", not "spill started"
**Why:** Swimmers care whether sewage is flowing, including spills continuing from the day before. Whether the site was spilling on the last observed day is known at issue time, so it is a legitimate feature (persistence baseline AUC 0.79).

### 2026-10-10 — Laptop sleep stops collection
**Evidence:** The Mac slept on battery from ~23:30 to 10:03; no collection ran and training was paused for ~10.5 h (5.6 min of CPU in 10.6 h wall-clock).
**Decision:** Move live collection to a cloud scheduler before the frontend goes public.

### 2026-10-10 — Cloud collection with GitHub Actions
**Decision:** A scheduled workflow (every 15 min, offset to :07) collects all feeds and commits only changed overflows (7 columns) plus a weekly full keyframe to a `data` branch. The laptop cron stays as a backup; both feed the same event model.
**Evidence:** Laptop sleep lost ~10.5 h of data overnight. Per run, 8–1,200 of 14,199 overflows change, so storing changes instead of full snapshots (~850 KB each) keeps growth to roughly 1–2 MB/day.
**Public repository:** unlimited free Actions minutes (a private repo would need ~2,900 min/month against a 2,000 free allowance) and doubles as a portfolio. Checked before publishing: no secrets in the full history; the personal reflective log moved to an untracked `private/` folder, since the ICA is an individual assessment and must not be shared or matched by plagiarism checks.
**Known risk:** GitHub may delay or skip scheduled runs at busy times; gaps are measured in `runs/*.csv` and `fct_collection_gaps`.

### 2026-10-10 — Website: static site on GitHub Pages
**Decision:** Score live data into JSON (`spillwatch publish`) and serve a static Leaflet site from the `gh-pages` branch, replaced by a single commit on each deploy.
**Why:** Free, always online, no server to maintain; force-pushing one commit stops ~5.6 MB of regenerated JSON accumulating in git (served gzip-compressed at ~0.7 MB).
**Found while building:**
- CARTO basemap tiles now require an API key ("API KEY REQUIRED" watermark); switched to OpenStreetMap tiles.
- Yorkshire Water publishes "#N/A" as some receiving-water names; the site falls back to the site id.
- A Southern Water overflow reports continuous spilling since 28 Sept. I first suspected a stale feed, but every company's feed had updated that day. The site now notes spills reported for > 3 days as "a prolonged discharge or a monitor fault", and warns if a company feed is > 24 h old.
- Headless Chrome cannot render narrower than ~500 px, so phone layouts were tested inside a 390 px iframe.

### 2026-10-10 — Live rainfall must match training rainfall
**Problem:** All forecast risks were blank at first. The laptop had slept through the last rain day, leaving it incomplete; the 06:00 archive backfill never ran.
**Fix:** Recent days now come from the readings API (`date=` + `_sorted` paging, ~2 MB/day instead of the ~150 MB archive), re-fetched hourly, so gaps repair themselves after sleep. The API sometimes returns a short page before the end, so paging only stops on an empty page (a day went from 59,997 to the full 88,105 readings).
**Training/serving skew check:** for the same gauges, daily totals from 15-min readings vs the Hydrology API daily totals used in training: mean 1.51 vs 1.46 mm, dry-day agreement 97%, correlation 0.79. Shifting either source by a day drops correlation to ~0.1, confirming the 09:00 rain-day alignment used in training is correct.
**To monitor:** in the first live week the model flagged 2.4% of scored spills (rule 0.8%) vs ~1.3% each in evaluation. One week is a small sample; track over time.

### 2026-10-10 — Scoring and deployment in GitHub Actions
**Decision:** An hourly workflow rebuilds a fresh warehouse from the `data` branch plus a live snapshot, scores with models downloaded from a GitHub Release, and deploys to Pages. The laptop no longer deploys.
**Design points:**
- Models are published as a versioned release asset (3 MB) and loaded with exact pinned library versions (`requirements-cloud.txt`), because joblib pickles are only safe to load with the scikit-learn/numpy they were saved with.
- Site features and the suspicion threshold are exported with the models, so the cloud needs no research database.
- Rainfall is stored on the `data` branch as hourly totals per gauge, one file per complete day (~80 KB), seeded with 31 days from the laptop. Today's partial readings are fetched every run but never stored.
- The readings API keeps only ~4 weeks (15 Sept available, 10 Sept empty on 10 Oct), so older missing days fall back to the daily archive file; each is fetched once and then stored.
- `SPILLWATCH_DATA_DIR` lets the job (and a local rehearsal) run in an isolated directory. The rehearsal ran the whole job from an empty directory in 2.5 min (first run, including 7 archive days).
**Known difference:** cloud event history starts on 10 Oct, so its 7-day counts were initially lower than the laptop's (2,917 vs 3,392 spills); this closes as history accumulates.
