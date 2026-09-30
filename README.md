# winning-product-agent

Find winning TikTok Shop products with KaloData's **KaloPilot** agent, run from Claude (Cowork) in the cloud — no local install needed.

## How to run it (any time)

Open a new Cowork task and type something like:

> Use winning-product-agent: find the top 10 breakout products in the US Beauty category in the last 7 days, priced $15–40.

The `winning-product-agent` skill (saved in your Claude account) loads the token, submits the question to KaloPilot, waits for the answer, and shows you the results + report link. Nothing to paste.

Good starter questions:
- "Top breakout products in the US this week under $30"
- "Which Home & Kitchen products grew fastest in the last 30 days in the US? Show sales, price, and top shops"
- "Extract the scripts of the 5 highest-revenue videos in Pet Supplies (US, last 7 days) and summarize the winning hooks"
- Follow-ups reuse the same task_id automatically: "Now compare #1 with the UK market"

## Running the scripts directly (optional)

```bash
git clone https://github.com/fernandocordoba-ux/winning-product-agent.git
cd winning-product-agent
bash scripts/setup-token.sh <YOUR_KALODATA_TOKEN>   # once per environment; free check, no credits
bash scripts/ask.sh "Top breakout products in the US this week under \$30"
bash scripts/ask.sh "Compare #1 with the UK market" <task_id_from_previous_answer>
```

`setup-token.sh` also picks the token up automatically from a `KALOPILOT_TOKEN` or `KALODATA_API_KEY` environment variable.

## Master runner (Step T)

```bash
python -m winning_product_agent preflight --tests
python -m winning_product_agent run --dry-run --profile config/runtime_first_live.yaml --check-balance   # free
python -m winning_product_agent run --live --profile config/runtime_first_live.yaml --max-products 5    # PAID: type CONFIRM LIVE RUN
python -m winning_product_agent status        # last run: stages, queries, credits
python -m winning_product_agent report        # where the last live report is
```

## Requirements

- `www.kalodata.com` in Claude **Settings → Capabilities → Additional allowed domains**.
- A KaloData token: kalodata.com/pilot → *Agent Skill Installation* → copy the token.
- KaloData credits (each query consumes some; complex reports use more).

## Layout

| Path | What |
|---|---|
| `scripts/setup-token.sh` | Saves token to `~/.kalopilot/token` and runs a free connectivity check |
| `scripts/credits.sh` | Free credit balance check (no credits used) |
| `scripts/ask.sh` | Submit a question, poll until done, print the answer, save raw response to `data/raw/` |
| `scripts/discovery.py` | Discovery (Step L): plan queries, normalize, dedupe, PASS/FAIL/REVIEW, max 100 candidates (tested) |
| `scripts/kalopilot_client.py` | KaloPilot client: free balance check; `discover` runs one query per category (spends credits) |
| `scripts/amazon_validation.py` | Amazon Validation (Step N): match confidence, AVS, Amazon Confidence, signals, flags; dry-run default (tested) |
| `scripts/suppliers.py` | Supplier Data Integration (Step W): manual CSV/JSON offers, match confidence, Supplier Quality / Confidence, ranking, selected offer for BVS, price history (no orders, no supplier contact) |
| `scripts/competitors.py` | Competitor Intelligence (Step X): manual research import, direct/adjacent/category, prices, Meta ads, offers, Saturation / Opportunity / Confidence, differentiation (not combined with scores) |
| `scripts/creatives.py` | Creative Intelligence (Step Y): hooks / angles / formats, concentration, Saturation / Opportunity / Confidence, evidence-backed gaps and test hypotheses (no scripts or ad copy stored) |
| `scripts/decision_engine.py` | Unified Decision Engine (Step Z, rules `config/decision.yaml` Z-v1): hard gates, 6 dimensions (STRONG/ACCEPTABLE/WEAK/UNKNOWN), 5 decision states, Decision Confidence, shortlist ≤ 3 READY, manual checklist, `reports/YYYY-MM-DD-final-decision.md/.json`. CLI: `python -m winning_product_agent decide [--rebuild] [--explain <product_id>]` (offline, no queries) |
| `scripts/business_viability.py` | Business Viability Score (Step O): economics, BVS + breakdown, BVS Confidence, commercial flags; `add-supplier` stores supplier data (tested) |
| `scripts/history.py` | Historical tracking (Step Q): append-only observations, index, deltas, velocity, trends, volatility, migration (dry-run default) (tested) |
| `scripts/emerging.py` | Emerging Product Detector (Step R): Momentum Score, Momentum Confidence, Emerging Status, flags, priority list (tested) |
| `scripts/safety.py` | Live Query Safety Gate, query budget, secret redaction, run logs, manifests (Step S) |
| `scripts/preflight.py` | Pre-flight: READY_FOR_DRY_RUN / BLOCKED + SYSTEM STATUS (`--tests` runs the suite) |
| `winning_product_agent/` | **Master runner** (Step T): preflight / run (dry-run default, confirmed live) / status / report; checkpoints, resume, query budget, data_environment |
| `scripts/pipeline.py` | Legacy dry-run (forwards to the master runner) |
| `scripts/config_validation.py` | Config schema validation (weights, totals, ranges, limit conflicts) |
| `scripts/concentration.py` | Creator/video revenue concentration + dependency flags (tested) |
| `scripts/validate_data.py` | Validate raw data → `data/processed/` (skeleton) |
| `scripts/score_products.py` | Scoring engine: WPS (wps-v1, per metric breakdown) + Confidence Score (tested) |
| `scripts/deep_analysis.py` | Deep Analysis (Step M): dry-run by default, credit protection, trend, concentration, WPS, Confidence, red flags (tested) |
| `scripts/confidence.py` | Confidence Score: data completeness/quality, 0–100 + breakdown (tested) |
| `scripts/generate_report.py` | Final report (Step P): Markdown + JSON, Top 10, emerging, watchlist, rejected, data quality (tested) |
| `config/` | `scoring.yaml`, `filters.yaml`, `categories.yaml`, `deep_analysis.yaml`, `amazon_validation.yaml`, `business_viability.yaml`, `report.yaml`, `history.yaml`, `emerging.yaml`, `runtime.yaml` (canonical run mode/limits/safety) |
| `prompts/` | `discovery.md`, `deep_analysis.md`, `amazon_validation.md`, `validation.md` |
| `data/raw/` | Raw API responses (git-ignored) |
| `data/processed/` | Validated data |
| `data/history/` | Append-only product observations + `history_index.json` |
| `reports/` | Generated reports |
| `runs/` | Per-run manifest + redacted structured log (git-ignored) |
| `tests/` | `python3 -m unittest discover tests` |
| `CLAUDE.md` | Rules Claude follows in this project |
| `kalopilot/` | Official KaloPilot skill by Kalodata (MIT), vendored |

The token and secrets are **never** committed (see `.gitignore`: `.env`, `.env.*`, `secrets/`, `data/raw/`).

## Production (Step AC)

| Command | What it does |
|---|---|
| `python -m winning_product_agent production-config review` | review every proposed change (APPROVE / REJECT / DEFER) with live evidence |
| `python -m winning_product_agent production-config promote` | write the next immutable `config/production/vN/` (+ `manifest.json` hashes) |
| `python -m winning_product_agent production-config verify [vN]` / `list` / `activate vN` | hash check, list versions, rollback / roll forward (nothing deleted) |
| `python -m winning_product_agent production-run` | DRY RUN (default): provider health, degraded mode, stages, budget, limits, report paths |
| `python -m winning_product_agent production-run --live` | paid run; requires exactly `CONFIRM PRODUCTION LIVE RUN` |
| `python -m winning_product_agent production-run --report-only [--run-id ID]` | rebuild reports / decision / audit, no query |

Production data: `data/production/`, runs: `runs/production/`, reports: `reports/production/`.

