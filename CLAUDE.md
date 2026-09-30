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
6. **Report**: `scripts/generate_report.py` → `reports/` (FACT / CALCULATION / INFERENCE / MISSING DATA)
7. **History**: dated snapshots in `data/history/` to track momentum over time

## Useful commands

```bash
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
