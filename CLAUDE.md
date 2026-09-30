# CLAUDE.md — winning-product-agent

Permanent rules for Claude in this project. They apply to every task, every session, and override convenience. If a request conflicts with a rule, say so before acting.

## What this project does

Finds TikTok Shop products worth researching using KaloData's KaloPilot API, then validates, filters, scores (WPS) and reports on them. Runs in the Claude (Cowork) cloud workspace, not on the user's computer.

## Core principle

**Claude interprets; the code calculates.** The same product with the same data must always get the same score (never 82 today and 74 tomorrow). Claude may explain, summarize and recommend based on computed results, but never produces, rounds, "adjusts" or overrides a number itself.

---

## Integrity rules (permanent)

### 1. Query KaloData/KaloPilot before making product claims
No statement about a product's performance, market, creators or videos without fresh KaloData/KaloPilot data behind it. Never answer from memory or general knowledge.

### 2. Never fabricate
Never invent, estimate, extrapolate or "fill in":
- GMV
- sales
- units
- growth
- creator counts
- video counts
- shop counts
- prices
- Amazon data

If the source did not return it, it does not exist for this project.

### 3. Preserve raw API/Skill output
Save every KaloPilot response unchanged in `data/raw/` (JSON, with `task_id`, `report_url`, query text and fetch timestamp) before any processing. Never edit or overwrite raw files; processing writes new files to `data/processed/`.

### 4. Separate FACT / CALCULATION / INFERENCE / MISSING DATA
Every report and every analysis answer labels its content:

| Label | Meaning | Example |
|---|---|---|
| **FACT** | Value returned by KaloData/KaloPilot, as returned | GMV 30d: $4,206,171 |
| **CALCULATION** | Deterministic result of code on facts (formula stated) | WPS 71.40 (scoring engine, wps-v1) |
| **INFERENCE** | Claude's interpretation or hypothesis; never a number presented as data | "Growth likely driven by creator videos" |
| **MISSING DATA** | Not returned by the source | Category product count: N/A |

Never blend them in one sentence in a way that makes an inference look like a fact.

### 5. WPS is calculated by the scoring engine, not guessed by Claude
Only `scripts/score_products.py` with `config/scoring.yaml` produces a WPS or Confidence Score. If the engine has not run, there is no WPS; say so.

### 6. Missing metrics must be N/A
Missing or invalid values are shown as `N/A`: never zero, never averaged, never estimated. N/A metrics score 0 points and lower the Confidence Score.

### 7. Every final recommendation must be traceable to source data
Each recommendation lists its sources: KaloPilot `task_id` / `report_url`, raw file path, fetch date, config versions (`wps-v1`, `filters-v1`, `categories-v1`) and the facts it relies on.

### 8. WPS measures research attractiveness, NOT guaranteed profitability
WPS ranks which products are most worth investigating. It does not include product cost, shipping, ad spend, returns or fees, and does not predict profit.

### 9. Never call a product a guaranteed winner
Use "high WPS", "strong research candidate" or the verdict labels (`winner` / `watch` / `skip`, defined as score bands). Never "guaranteed", "sure thing", "can't lose" or similar.

### 10. US market is the default
Use US unless the user explicitly asks for another market in that request.

### 11. Use USD
All money values in USD. If a source returns another currency, keep the original in raw data and flag it; do not convert unless the user asks.

### 12. Prefer emerging momentum over total GMV
Rank and recommend by momentum (growth, new creators/videos, acceleration) rather than by total GMV alone. A mid-size product that is accelerating beats a large product that is flat or declining.

---

## Operating rules

- **Credits:** before any KaloPilot query that spends credits, show the current balance (`bash scripts/credits.sh`, free), the estimated cost and the exact question, then wait for the user's OK. After it runs, report credits consumed and the new balance.
- **No hardcoded thresholds:** all filter and scoring numbers live in `config/*.yaml` (experimental v1); Python only reads them. Do not change weights, thresholds or rules unless the user asks.
- **Per-category thresholds:** category `overrides` in `config/categories.yaml` take precedence over global `filters.yaml` / `scoring.yaml`. Never assume one threshold fits all categories long-term.
- **Secrets:** never print or commit the token. It lives in `~/.kalopilot/token` or env vars (`KALOPILOT_TOKEN`, `KALODATA_API_KEY`).
- **GitHub:** do not push until the user says the project is finished.

## Pipeline

1. **Discovery (Step L)**: `python3 scripts/discovery.py plan` (free) → user OK on cost → `python3 scripts/kalopilot_client.py discover` (spends credits; one query per enabled category, raw saved read-only in `data/raw/`) → `python3 scripts/discovery.py process data/raw/<files>` (free) → `data/processed/discovery_<ts>.json`: normalized facts, dedupe, PASS/FAIL/REVIEW from `filters.yaml`, max 100 candidates ordered by momentum (not GMV), WPS pending, discovery-stage Confidence. No deep analysis here (Step M).
2. **Validation**: `scripts/validate_data.py` (rules in `prompts/validation.md`) → `data/processed/`
3. **Filters**: `config/filters.yaml` (+ category overrides) → pass / flagged / rejected, with reasons
4. **Deep analysis (Step M)**: `python3 scripts/deep_analysis.py` = DRY-RUN (free, default) → user OK on cost → `--live` (spends credits). Selects PASS then REVIEW from the latest Discovery file (FAIL excluded, max `deep_analysis_max_products`=40), one `deep_product` query per product, balance check before every paid query (stops safely below estimate + reserve), no duplicate paid query per run, 24h cache. Raw → `data/raw/deep_analysis/` (read-only), results → `data/processed/deep_analysis/`. Computes trend (ACCELERATING/GROWING/STABLE/DECLINING/INSUFFICIENT_DATA), concentration, WPS + breakdown, Confidence + breakdown, red flags. Thresholds in `config/deep_analysis.yaml`.
   - **Concentration**: `scripts/concentration.py` → Top 1 / Top 3 creator and video revenue share; flags `CREATOR_DEPENDENCY` / `VIDEO_DEPENDENCY` (thresholds in `filters.yaml`). Missing data → N/A, no flag, no penalty.
4b. **Amazon validation (Step N)**: `python3 scripts/amazon_validation.py` = DRY-RUN (free, default) → user OK → `--live` (spends credits). Only products with WPS ≥ 70 AND Confidence ≥ 60 (discovery FAIL excluded), max 20, 24h cache, balance check before each paid query. Provider returns raw candidates; code computes match confidence (EXACT/STRONG/POSSIBLE/UNRELIABLE; < 60 → NO_RELIABLE_MATCH, never used to fill fields), signals, **AVS** and **Amazon Confidence**. AVS is NEVER added to WPS; Amazon Confidence is separate from TikTok Confidence. KaloPilot has no dedicated Amazon dataset (web search only): unverifiable fields stay N/A. Thresholds in `config/amazon_validation.yaml`.
4c. **Business Viability (Step O)**: `python3 scripts/business_viability.py` (no paid queries). Products with WPS ≥ 70 AND Confidence ≥ 60; Amazon optional. **BVS** (0–100: margin 25, shipping 15, supplier 15, competition 15, return risk 10, advertising 10, compliance/IP 10) and **BVS Confidence** (commercial evidence completeness) are independent of WPS/Confidence/AVS/Amazon Confidence. KaloData has NO supplier economics: product cost, supplier shipping, logistics and supplier metrics come only from `data/raw/business_viability/` (`add-supplier <product_id> <json>`); until integrated they are N/A and BVS Confidence stays low. Never estimate supplier cost. Using the TikTok price as our selling price is labeled an ASSUMPTION. Compliance/IP flags are keyword screening, never legal conclusions. Flags never hide a product. Thresholds in `config/business_viability.yaml`.
5. **Scoring**: `scripts/score_products.py` + `config/scoring.yaml` → **WPS** (engine implemented in Step M, exactly per `scoring.yaml`) and **Confidence Score** (`scripts/confidence.py`, implemented). They are independent: Confidence measures data completeness only, never performance; it never raises or lowers WPS. Always report both: `WPS: X/100` and `Confidence: X/100 (LEVEL)` with the per-component breakdown.
6. **Report (Step P)**: `python3 scripts/generate_report.py` (free, no queries) → `reports/YYYY-MM-DD-winning-products.md` + `.json` (dated files never overwritten; `latest-winning-products.md` replaced). Joins all stages by product_id; scores shown side by side, NEVER combined. Top ranking V1: WPS desc → Confidence desc → BVS desc → AVS desc → product_id (max 10). Sections: Top, Emerging (needs ≥ 2 observations), Watchlist, Rejected/High-risk, Data quality. Every product keeps source traceability; secrets are scrubbed. Rules in `config/report.yaml`.
7. **History (Step Q)**: `scripts/history.py` — append-only, immutable observations in `data/history/products/{id}/{YYYY-MM-DD}T{time}Z.json` (never overwritten or deleted; same product + same timestamp = duplicate, skipped; several per day kept). `history_index.json` is the only file rebuilt. Deltas (NEW_GROWTH when previous = 0, never ÷0), velocity by real elapsed time, 7/14/30-day trends (ACCELERATING/GROWING/STABLE/DECLINING/VOLATILE/INSUFFICIENT_DATA), CV volatility — thresholds in `config/history.yaml`. `product_tracking_age_days` is how long WE have tracked it, not the listing age. Migration of processed files: `python3 scripts/history.py migrate` = DRY-RUN; `--apply` only when the user asks. The report uses the history store when present.

8. **Emerging detector (Step R)**: `python3 scripts/emerging.py` (free) — uses ONLY the Step Q history. Momentum Score (0–100: WPS trajectory 20, GMV 25, units 15, creators 15, videos 15, competition balance 10), Momentum Confidence (evidence quality only) and Emerging Status (EMERGING_STRONG / EMERGING / EMERGING_REVIEW / STABLE / LOSING_MOMENTUM / INSUFFICIENT_HISTORY; needs ≥ 3 observations on ≥ 3 days). Separate from WPS/Confidence/AVS/Amazon Confidence/BVS/BVS Confidence — never combined. Rising competition is not automatically bad: only competition growing faster than demand counts against a product. CREATOR/VIDEO_DEPENDENCY blocks EMERGING_STRONG. Outputs `data/processed/emerging/emerging_<ts>.json` (immutable) + `latest.json`. Thresholds in `config/emerging.yaml`. The report's Emerging section uses this detector (priority: STRONG > EMERGING > REVIEW, then Momentum, Momentum Confidence, WPS, WPS Confidence).

## Live-run safety (Step S)

- `config/runtime.yaml` is the **canonical** source for run mode, limits and safety. It must keep the safe defaults (`live_mode: false`, `dry_run: true`, `explicit_live_confirmation: false`); pre-flight BLOCKS otherwise. Stage-level limit keys must equal runtime.yaml (pre-flight blocks on conflicts).
- **Live Query Safety Gate** (`scripts/safety.py`) runs inside `kalopilot_client.submit()`, the single chokepoint for paid queries: live_mode AND not dry_run AND explicit_live_confirmation AND credentials AND balance − estimate ≥ reserve. Live flags are set only IN MEMORY for one run (`safety.set_run_overrides`) after the user's explicit OK — never by editing runtime.yaml.
- `python3 scripts/preflight.py [--tests]` → READY_FOR_DRY_RUN or BLOCKED (never READY_FOR_LIVE). `python -m winning_product_agent run --dry-run` (Step T; `scripts/pipeline.py dry-run` forwards to it) previews every stage, writes only `runs/{run_id}/manifest.json` + `log.jsonl` (redacted).
- Query budget: estimates are labeled as configured estimates; unknown costs stay UNKNOWN. Logs/manifests/reports are redacted.
- `python3 scripts/config_validation.py` validates weights (WPS/AVS/BVS/Momentum/confidences = 100), ranges and limits.

## Master Runner (Step T) — the ONE way to run the pipeline

- `python -m winning_product_agent preflight | run [--dry-run] | run --live ... | status | report` (from the project root).
  Stage CLIs (`scripts/deep_analysis.py --live`, `kalopilot_client.py discover`, ...) can never spend credits on their own:
  the gate blocks them because only the master runner sets the in-memory live overrides.
- Stage order: PRE-FLIGHT → DISCOVERY → FILTERING → DEEP ANALYSIS → WPS → WPS CONFIDENCE → AMAZON (eligible only) → BVS →
  HISTORICAL STORAGE → EMERGING → FINAL REPORT → RUN SUMMARY. Stage status: PENDING/RUNNING/COMPLETED/PARTIAL/FAILED/SKIPPED/BLOCKED.
- `run` with no flag = DRY RUN (never paid). Live needs a profile with live_mode true + dry_run false, pre-flight READY, valid limits,
  credentials, credit check AND the exact typed phrase `CONFIRM LIVE RUN` (non-interactive: `--confirm-live "CONFIRM LIVE RUN"`).
  yes/y/ok/continue are rejected. Anything missing BLOCKS (never downgraded). In Cowork, Claude passes `--confirm-live` ONLY after
  the user has written exactly `CONFIRM LIVE RUN` in the chat, after seeing the query budget.
- Profiles (`config/runtime_first_live.yaml`) may override only runtime / limits / query_plan and never exceed runtime.yaml limits.
- Credit saving (`query_plan`): combined discovery (1 query instead of 9), deep/amazon batches (5 products per query),
  `max_credits_for_run` hard cap, cache (< 24 h) checked before EVERY paid query; batched answers are split by product_id only.
- Every paid query: cache → run cap → Live Query Safety Gate → submit; recorded as LIVE_QUERY / CACHE_HIT / BLOCKED / FAILED in
  `runs/{run_id}/manifest.json`. Checkpoints in `runs/{run_id}/checkpoints/`; `--resume RUN_ID` never re-buys completed work.
- `data_environment`: every stored record is `LIVE` (real provider) or `SYNTHETIC` (fake providers in tests). A report only uses
  records of the run's environment (a SYNTHETIC record BLOCKS a LIVE report); cache and history views never cross environments.
  Raw files saved before Step T are untagged and count as LIVE (all were real KaloPilot answers).

## Step U calibration (first live runs, 2026-09-29/30)

- KaloPilot pauses on plan restrictions ("Category Overview not included in Professional plan") and the API returns
  "completed" with no data (message_id 0). Prompts forbid Category Overview; the runner flags the pause; a task continued
  in the web UI is imported for free with `--from-task TASK_ID`.
- One answer holds ~8k output tokens: Deep Analysis is 1 product per query (~1.7–2.5 credits each, observed).
- Growth is CALCULATED by the code from `gmv_30d` and `gmv_prev_30d` (provider value kept as growth_30d_provider;
  GROWTH_MISMATCH / GROWTH_UNVERIFIED flags). Discovery's provider growth was wrong by ~100x on 2026-09-29.
- WPS v2 (post-live calibration): Growth Momentum /25 = long-term 10 (CORE) + recent trend 10 + acceleration 5
  (SUPPORTING, from daily GMV). Metric tiers CORE/SUPPORTING/ENHANCEMENT (scoring.yaml data_requirements):
  WPS = 100 x earned / (CORE points + available SUPPORTING points); missing SUPPORTING/ENHANCEMENT data lowers
  Confidence (documented adjustments), never rejects. Tiers follow config/provider_capabilities.yaml (real responses).
- Competition scope PRODUCT_CLUSTER / SUBCATEGORY / CATEGORY / UNKNOWN; only comparable scopes are scored or can
  raise EXTREME_SATURATION (COMPETITION_NOT_COMPARABLE flag otherwise).
- Field provenance (scripts/provenance.py) on every deep/discovery record; history has provider_observation_timestamp,
  retrieved_at, imported_at and source_snapshot_hash (DUPLICATE_SNAPSHOT_SKIPPED).
- Calibration lane (config/calibration_lane.yaml, off): calibration_only Amazon/BVS records, never ranked.
- `python3 scripts/recalibrate_first_live.py` = offline recalculation of a live run (0 queries).
- Discovery order puts low-base / spike products last. History counts one provider answer once (append-only kept).
- Cached answers are reused only if asked with the current prompt version.
- `python -m winning_product_agent audit [RUN_ID]` = calibration audit (read-only).

## Supplier Data Integration (Step W)

- `scripts/suppliers.py` + `config/suppliers.yaml`. NO orders, NO supplier contact, NO invented prices/shipping/duties.
- SupplierProvider interface (search_product / get_offer_details / get_shipping_quote / normalize_offer). Only
  `manual_import` (CSV/JSON) is implemented; AliExpress, CJ, Alibaba, Zendrop, AutoDS, KaloPilot raise
  ProviderNotIntegrated until real access exists.
- Independent scores: Supplier Match Confidence, Supplier Quality Score, Supplier Confidence (never combined).
- Selection for BVS = highest-ranked ELIGIBLE offer (match >= 75, product + shipping cost present, not out of stock);
  rank = eligible > delivery tier > quality > landed cost > confidence > offer_id. Never "cheapest first".
- Landed cost = product + shipping (+ import duty only when explicitly provided; otherwise disclosed as excluded).
- Contribution margin stays N/A while ad cost / refund / chargeback assumptions are null (never invented).
- Storage: data/raw/suppliers (read-only), data/processed/suppliers/<pid>/, data/history/suppliers/<offer_id>/ (append-only).
- `python -m winning_product_agent suppliers import <file.csv|json>` / `suppliers show <product_id>`.

## Competitor Intelligence (Step X)

- `scripts/competitors.py` + `config/competitors.yaml`. NOT combined with WPS / AVS / BVS (BVS adapter `bvs_inputs()`
  prepared, not wired). No scraping or ToS-violating access; only `manual_import` is implemented (Meta Ad Library,
  Google, Minea, Dropship.io, Similarweb, BuiltWith raise ProviderNotIntegrated).
- DIRECT (match >= 75) / ADJACENT (>= 60) / CATEGORY / UNRELATED — never mixed; imports may downgrade, never upgrade.
- Independent scores: Competitor Saturation (30/25/15/15/15), Competitive Opportunity (demand-gated, not the
  inverse), Competitor Confidence, Store Quality (observable yes/no only). Ad spend / sales / traffic never estimated;
  ad longevity = persistence, not profitability. Differentiation opportunities require evidence facts.
- Storage: data/raw/competitors (read-only), data/processed/competitors/<pid>/, data/history/competitors/<pid>/ snapshots;
  competition_velocity only from >= 2 snapshots on different days.
- `python -m winning_product_agent competitors import <file.csv|json>` / `competitors show <product_id>`.

## Useful commands

```bash
python -m winning_product_agent preflight --tests       # READY_FOR_DRY_RUN / BLOCKED
python -m winning_product_agent run --dry-run --profile config/runtime_first_live.yaml --check-balance
python -m winning_product_agent run --live --profile config/runtime_first_live.yaml --max-products 5   # PAID, asks CONFIRM LIVE RUN
python -m winning_product_agent status | report
bash scripts/setup-token.sh      # save/check token (free)
bash scripts/credits.sh          # credit balance (free)
bash scripts/ask.sh "<question>" # run a KaloPilot query (spends credits)
```

## Layout

| Path | Purpose |
|---|---|
| `config/` | `scoring.yaml` (WPS v1), `filters.yaml`, `categories.yaml` |
| `prompts/` | Question templates sent to KaloPilot + validation rules |
| `scripts/` | Python/bash tools for the pipeline |
| `data/raw/` | Unmodified API responses (git-ignored, never edited) |
| `data/processed/` | Validated, normalized data |
| `data/history/` | Dated snapshots for trend tracking |
| `reports/` | Generated reports |
| `tests/` | Tests (incl. determinism tests for the scoring engine) |
| `kalopilot/` | Official KaloPilot skill (MIT), vendored |
