# SpillWatch AI

**Using AI to detect suspicious sewage discharges and forecast safe swimming conditions in England's rivers and seas.**

🌊 **Live site: [mahum24.github.io/spillwatch-ai](https://mahum24.github.io/spillwatch-ai/)**

England's storm overflows spilled 291,492 times in 2025, for nearly 1.9 million hours. Spills are only permitted during heavy rainfall: discharging in dry weather is illegal and especially harmful. Since 2024 every water company publishes near-real-time overflow status, but nothing flags which spills are *suspicious* or warns people *before* a spill happens. SpillWatch AI aims to do both.

## Links

| What | Where |
|---|---|
| Live website | [mahum24.github.io/spillwatch-ai](https://mahum24.github.io/spillwatch-ai/) |
| Source code (this repository) | [github.com/mahum24/spillwatch-ai](https://github.com/mahum24/spillwatch-ai) |
| Website hosting | GitHub Pages, deployed by the [Score and publish site](https://github.com/mahum24/spillwatch-ai/actions/workflows/publish.yml) workflow (hourly) |
| Cloud data collection | [Collect overflow data](https://github.com/mahum24/spillwatch-ai/actions/workflows/collect.yml) workflow (every 15 min), stored on the [`data` branch](https://github.com/mahum24/spillwatch-ai/tree/data) |
| Trained models | [`models-v1` release](https://github.com/mahum24/spillwatch-ai/releases/tag/models-v1) |
| Assistant server | Cloudflare Worker `spillwatch-assistant` ([code](worker/src/index.js)); manage at [dash.cloudflare.com](https://dash.cloudflare.com) → Workers & Pages |
| Gemini API keys | [aistudio.google.com/apikey](https://aistudio.google.com/apikey) |
| Design decisions and evidence | [docs/decisions.md](docs/decisions.md) |
| Model results | [Suspicious spills](docs/results/phase3_dry_spill.md) · [Forecasting](docs/results/phase4_forecast.md) |

New computer or new account? Follow **[Setup from scratch](#setup-from-scratch)**.

## Status

| Phase | Scope | Status |
|---|---|---|
| 1. Data pipeline | Live overflow + rainfall ingestion, DuckDB warehouse, spill events, rain-gauge matching, baseline dry-spill rule | ✅ Done |
| 2. Orchestration & quality | Scheduled collection, 11 data-quality checks, collector gaps, monitor status changes | ✅ Done |
| 3. AI: suspicious-spill detection | Model of expected spilling given rainfall (AUC 0.927), evaluated against the dry-day rule. [Results](docs/results/phase3_dry_spill.md) | ✅ First version |
| 4. AI: spill forecasting | 1–2 day spill risk; AUC 0.916 / 0.894 with real archived forecasts. [Results](docs/results/phase4_forecast.md) | ✅ First version |
| 5. AI: river assistant | Gemini answering questions through five tools over the live data; 28/28 correct after hand review with tools vs 10/28 without, one invented figure. [Results](docs/results/phase5_assistant.md) | ✅ First version |
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

## Website (Phase 6)

`spillwatch publish` scores live data with both models and writes JSON to `site/data/`. It is a static site (Leaflet + vanilla JS), so there is no server to run. Shareable links: `#view=suspicious&site=<overflow id>`.

In production the [`Score and publish site`](.github/workflows/publish.yml) workflow does this hourly in GitHub Actions: it rebuilds a warehouse from the `data` branch plus the live feeds, downloads the trained models from the [`models-v1` release](https://github.com/mahum24/spillwatch-ai/releases/tag/models-v1), scores, and deploys to GitHub Pages. To update the site immediately, run that workflow by hand from the Actions tab. (`scripts/deploy_site.sh`, which pushes to a `gh-pages` branch, only applies if Pages is set to deploy from that branch instead of from GitHub Actions.)

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

**Result.** With tools: 27/28 correct automatically, 28/28 after hand review; 27/28 grounded. Without tools: 10/28 correct. The one grounding failure is a legality question the model correctly declined but padded with invented company figures without calling a tool. [Full results](docs/results/phase5_assistant.md)

How to get a key, deploy the server and run the evaluation: see [Setup from scratch](#setup-from-scratch), steps 8–9.

## Setup from scratch

Everything needed to run SpillWatch AI on a new computer, recreate the cloud set-up in a new GitHub account, or replace a lost or leaked API key. Do the steps in order; each one says how to check it worked. Steps 1–4 are enough to run it locally. Steps 6–8 are the online parts.

### 0. What you need

| Thing | Why | Get it |
|---|---|---|
| Git | Download the code | macOS: `xcode-select --install` · others: [git-scm.com](https://git-scm.com/downloads) |
| Python 3.10 or newer | The pipeline and models | [python.org/downloads](https://www.python.org/downloads/) (check: `python3 --version`) |
| Node.js 22 or newer | The assistant, its tests and the Cloudflare tool | [nodejs.org](https://nodejs.org/) or `brew install node` (check: `node -v`) |
| GitHub account | Code, cloud collection, website hosting | [github.com/signup](https://github.com/signup) |
| GitHub CLI (`gh`) *(optional)* | Creating the models release from the terminal | `brew install gh`, then `gh auth login` |
| Google account | Free Gemini API key for the assistant | Any Gmail account |
| Cloudflare account | Free server that keeps the Gemini key secret | [dash.cloudflare.com/sign-up](https://dash.cloudflare.com/sign-up) (no card needed) |

Disk: about 1 GB for live data and 5 GB if you rebuild the research datasets (step 5).

### 1. Get the code

```bash
git clone https://github.com/mahum24/spillwatch-ai.git
cd spillwatch-ai
```

All commands below are run from this `spillwatch-ai` folder unless they say otherwise.

### 2. Install and test

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest -q          # Python tests: should end with "27 passed"
npm test                     # assistant tests: should end with "# pass 19" and "# fail 0"
```

If `pytest` says `No module named 'spillwatch'`, you ran a different Python: always use `.venv/bin/...`.

### 3. Collect live data on your computer

```bash
.venv/bin/spillwatch stations               # rain gauge locations (~1,000 gauges)
.venv/bin/spillwatch collect                # one snapshot of all ~14,200 overflows + latest rainfall
.venv/bin/spillwatch backfill-rain --days 30
.venv/bin/spillwatch transform              # build the warehouse tables
.venv/bin/spillwatch check                  # 11 data-quality checks
.venv/bin/spillwatch health                 # overall status
.venv/bin/spillwatch summary                # headline numbers
```

Data goes to `data/` (not in git). Set `SPILLWATCH_DATA_DIR=/some/folder` to put it elsewhere.

### 4. Keep collecting every 15 minutes (laptop backup)

Spill history only exists if the feeds are polled regularly. To collect while your computer is awake:

1. Run `crontab -e` and add this line, with your real folder path:
   ```
   7,22,37,52 * * * * "/full/path/to/spillwatch-ai/scripts/collect.sh"
   ```
2. **macOS only:** cron cannot read the Documents folder until allowed. Open *System Settings → Privacy & Security → Full Disk Access*, click **+**, press <kbd>Cmd</kbd>+<kbd>Shift</kbd>+<kbd>G</kbd>, type `/usr/sbin/cron` and add it.
3. Check after 15 minutes: `tail data/logs/collect_$(date +%Y-%m-%d).log` should show `DQ ... PASS` lines.

The cloud collector (step 7) is the main one; this is a backup for when GitHub's schedule is late.

### 5. Train the models (optional: only to retrain)

The trained models are already published in the [`models-v1` release](https://github.com/mahum24/spillwatch-ai/releases/tag/models-v1), so the website does not need this step. To rebuild them yourself (takes a few hours, mostly downloads):

```bash
.venv/bin/spillwatch history          # ~1.8M historical spill events (8 company datasets)
.venv/bin/spillwatch rain-history     # daily rainfall, 991 gauges, 2020 onwards (slow: rate-limited)
.venv/bin/spillwatch build-research   # spells + site-day table in data/research.duckdb
.venv/bin/spillwatch train            # suspicious-spill model -> data/models/dry_spill/
.venv/bin/spillwatch forecast-history # archived weather forecasts (Open-Meteo)
.venv/bin/spillwatch train-forecast   # 1-2 day forecast model -> data/models/forecast/
.venv/bin/spillwatch export-models    # bundle everything into data/models.tar.gz
```

Results are written to `docs/results/`. If `rain-history` reports HTTP 403 errors, it is being rate-limited; re-run it later and it continues where it stopped.

### 6. Preview the website on your computer

Needs trained models in `data/models/`: either step 5, or download the release:

```bash
gh release download models-v1 --pattern models.tar.gz --dir data   # or download it from the release page
tar -xzf data/models.tar.gz -C data
.venv/bin/spillwatch publish --out site/data    # score live data into site/data/*.json
cd site && python3 -m http.server 8000          # open http://localhost:8000
```

The assistant works locally too: with no server address set, click ⚙ in the chat and paste your own Gemini key (step 8a).

### 7. Put it online with GitHub (cloud collection + website)

Skip this if you are only using the existing repository. To set it up in a **new** GitHub account or repository:

1. **Create the repository** on GitHub (public is best: GitHub Actions minutes are free and unlimited for public repositories), then push the code:
   ```bash
   git remote set-url origin https://github.com/<your-username>/spillwatch-ai.git
   git push -u origin main
   ```
2. **Create the `data` branch** (where the cloud collector stores data; it shares no history with `main`):
   ```bash
   git switch --orphan data
   echo "# SpillWatch collected data" > README.md
   git add README.md && git commit -m "Initialise data branch"
   git push -u origin data
   git switch main
   ```
3. **Upload the models** as a release called `models-v1` (the website workflow downloads them from there):
   ```bash
   gh release create models-v1 data/models.tar.gz --title "Models v1" --notes "Trained models"
   ```
   Or on GitHub: *Releases → Draft a new release → tag `models-v1`* → attach `data/models.tar.gz` → *Publish*.
4. **Allow workflows to write:** *Settings → Actions → General → Workflow permissions →* **Read and write permissions** → *Save*.
5. **Turn on the website:** *Settings → Pages → Build and deployment → Source:* **GitHub Actions**.
6. **Start both workflows once by hand:** *Actions → Collect overflow data → Run workflow*, wait for the green tick, then *Actions → Score and publish site → Run workflow*. After about 5 minutes the site is at `https://<your-username>.github.io/spillwatch-ai/`.
7. *(Optional)* Seed 30 days of rainfall so the first risk forecasts are complete. Without this, the cloud downloads missing days itself, more slowly:
   ```bash
   git worktree add ../spillwatch-data data
   .venv/bin/spillwatch export-rain --data-dir ../spillwatch-data
   cd ../spillwatch-data && git add -A && git commit -m "Seed rainfall" && git push && cd -
   ```

After that, the workflows run on their own: collection every 15 minutes, website hourly. **GitHub often delays scheduled runs** (sometimes by hours, especially for new repositories), which is why the laptop backup exists. Check the *Actions* tab to see when they last ran.

### 8. Set up the AI assistant (Gemini key + Cloudflare)

#### 8a. Get a free Gemini API key

1. Go to **[aistudio.google.com/apikey](https://aistudio.google.com/apikey)** and sign in with a Google account.
2. Accept the terms if asked, then click **Create API key** (choose *Create API key in new project* if it asks for a project).
3. Copy the key. **Treat it like a password:** never put it in code, in a commit, in a chat or in a screenshot.

It is free: the free tier has per-minute and per-day limits (shown under *Rate limit* in AI Studio). On the free tier Google may use prompts to improve its models; SpillWatch only sends public data and the visitor's question.

#### 8b. Put the key on the Cloudflare server

```bash
cd worker
npm install                                  # installs Cloudflare's "wrangler" tool (first time only)
npx wrangler login                           # opens the browser: log in to Cloudflare and click Allow
npx wrangler secret put GEMINI_API_KEY       # type ONLY this; it then asks for the value
```

When it shows `Enter a secret value:`, paste the key and press <kbd>Enter</kbd> (nothing appears as you paste; that's normal). The **name** is always `GEMINI_API_KEY`; the key itself is only ever pasted at that prompt.

#### 8c. Deploy the server

```bash
npx wrangler deploy                          # still inside worker/
```

It prints an address like `https://spillwatch-assistant.<your-subdomain>.workers.dev`. Check it: open `<that address>/health` in a browser; it should show `"ok":true` and `"assistant_ready":true`.

If your site is not at `mahum24.github.io`, change `ALLOWED_ORIGINS` in `worker/wrangler.toml` to your site's address (for example `https://<your-username>.github.io`) and deploy again; the server refuses requests from any other website.

#### 8d. Connect the website to the server

In `site/index.html`, put the address in the `spillwatch-assistant` tag:

```html
<meta name="spillwatch-assistant" content="https://spillwatch-assistant.<your-subdomain>.workers.dev">
```

Commit and push. The site updates on its next publish run (or run *Score and publish site* by hand). Then open the site, click **Ask SpillWatch AI** and try an example question.

#### 8e. Make GitHub's jobs run on time (GitHub token)

GitHub often runs scheduled workflows hours late. The same Cloudflare Worker has a timer (Cron Triggers, in `worker/wrangler.toml`) that starts *Collect overflow data* at :07/:22/:37/:52 and *Score and publish site* at :12, using a GitHub token that can only run this repository's workflows:

1. Open **[github.com/settings/personal-access-tokens/new](https://github.com/settings/personal-access-tokens/new)** (fine-grained token).
2. *Token name:* `spillwatch-scheduler` · *Expiration:* the longest offered (note the date: the timer stops when it expires).
3. *Repository access:* **Only select repositories** → `spillwatch-ai`.
4. *Permissions → Repository permissions → Actions:* **Read and write**. Leave everything else as "No access".
5. **Generate token** and copy it (it starts with `github_pat_`).
6. Store it on the Worker and deploy:
   ```bash
   cd worker
   npx wrangler secret put GITHUB_TOKEN       # paste the token at the prompt
   npx wrangler deploy
   ```
7. Check: `<worker address>/health` shows `"scheduler_ready":true`, and after 15 minutes the [Actions tab](https://github.com/mahum24/spillwatch-ai/actions) shows runs with the event **workflow_dispatch** every 15 minutes.

When the token expires, create a new one the same way and repeat step 6.

#### 8f. Key for the evaluation script

The evaluation runs on your computer and reads the key from a file called `.env` in the `spillwatch-ai` folder (git ignores this file, so it is never uploaded). Create it in a text editor, saved as plain text, containing one line:

```
GEMINI_API_KEY=paste-your-key-here
```

Then run:

```bash
npm run eval:assistant                       # 28 test questions with tools
npm run eval:assistant -- --mode notools     # the same questions without tools (baseline)
npm run report:assistant                     # write docs/results/phase5_assistant.md
```

Raw answers are saved in `docs/results/assistant/`; the report summarises every complete run on the newest snapshot.

### 9. Replace a Gemini key (lost, leaked or expired)

If a key was ever pasted somewhere public (a chat, a commit, a screenshot), replace it straight away:

1. **[aistudio.google.com/apikey](https://aistudio.google.com/apikey)** → find the old key → **Delete**. It stops working immediately.
2. **Create API key** → copy the new one.
3. Update the server (it takes effect at once; no redeploy needed):
   ```bash
   cd worker
   npx wrangler secret put GEMINI_API_KEY    # paste the NEW key at the prompt
   ```
4. Update `GEMINI_API_KEY=` in your `.env` file.
5. If you saved the old key in the website's ⚙ settings, click **Forget my key** there.

If a key was committed to git by mistake, deleting the key (step 1) is what matters; removing it from git history afterwards does not make an exposed key safe again.

### 10. Everyday commands and troubleshooting

| Task | Command / place |
|---|---|
| Pull cloud-collected data into the local warehouse | `.venv/bin/spillwatch sync-cloud` |
| Check pipeline health | `.venv/bin/spillwatch health` |
| Update the website now | *Actions → Score and publish site → Run workflow* |
| See when the cloud jobs last ran | [Actions tab](https://github.com/mahum24/spillwatch-ai/actions) |
| See the assistant server's logs | `cd worker && npx wrangler tail` (then ask a question on the site) |
| Change the Gemini model | `GEMINI_MODEL` in `worker/wrangler.toml` and `DEFAULT_MODEL` in `site/assistant/chat.js`, then `npx wrangler deploy` |

| Problem | Fix |
|---|---|
| `cd: no such file or directory: worker` | You are in the wrong folder. `cd` into `spillwatch-ai` first. |
| `Required Worker name missing` | Run wrangler from inside `worker/` (where `wrangler.toml` is). |
| Assistant says "not configured yet" | The secret is missing: redo step 8b. |
| Cloud jobs run only every few hours | The scheduler token is missing or expired: step 8e. `<worker address>/health` shows `scheduler_ready`. |
| Assistant says the shared allowance is used up | The free key hit Google's limit; wait (limits reset daily) or add your own key in ⚙. |
| Assistant says "only serves the SpillWatch website" | Your site's address is not in `ALLOWED_ORIGINS` (step 8c). |
| Website shows "Could not load data" | The publish workflow has not run yet, or failed: check the Actions tab. |
| Laptop log is empty | macOS Full Disk Access for cron (step 4). |
| `database is locked` | Two jobs ran at once; it retries for 2 minutes. If it persists, check nothing else has `data/warehouse.duckdb` open. |

## Data quality

After every collection, 11 checks run (`src/spillwatch/quality.py`) and are stored in `dq_results`: feed completeness, row-count stability, duplicate ids, coordinate validity, offline monitors, inconsistent event times, future timestamps, rainfall freshness and coverage. `fct_collection_gaps` records any time the collector missed a run, so the completeness of spill history is always known.

## Known limitations

- An event that starts *and* ends between two polls, followed by another event before the next poll, would be missed. 15-minute polling keeps this rare; it will be quantified in Phase 2.
- Monitors go offline (≈2% at any time). Offline periods are tracked in `dim_overflows.offline_ratio`.
- A flagged spill is **potentially suspicious**, not proven illegal: monitors can malfunction, and the nearest gauge may miss local rain.

## Ethics

Only public, non-personal open data is used. Outputs describe spills as "potentially suspicious" and state the model's uncertainty. No individual or company is accused of wrongdoing.
