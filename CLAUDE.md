# CLAUDE.md — winning-product-agent

Instructions for Claude when working in this project.

## What this project does

Finds winning TikTok Shop products using KaloData's KaloPilot API, then validates, scores and reports on them. Runs in the Claude (Cowork) cloud workspace, not on the user's computer.

## Pipeline

1. **Discovery**: `prompts/discovery.md` → KaloPilot → raw JSON in `data/raw/`
2. **Validation**: `scripts/validate_data.py` (rules in `prompts/validation.md`) → `data/processed/`
3. **Deep analysis**: `prompts/deep_analysis.md` → KaloPilot, for shortlisted products
4. **Scoring**: `scripts/score_products.py` using `config/scoring.yaml` (WPS v1 rules defined; engine not implemented yet)
5. **Report**: `scripts/generate_report.py` → `reports/`
6. **History**: snapshots in `data/history/` to track products over time

## Core principle

**Claude interprets; the code calculates.** The same product with the same data must always get the same score (never 82 today and 74 tomorrow). Scores come only from `scripts/score_products.py` + `config/scoring.yaml`. Claude may explain, summarize and recommend based on those results, but never produces, rounds, "adjusts" or overrides a number itself.

## Rules (always)

- **Credits:** before any KaloPilot query that spends credits, show the current balance (`bash scripts/credits.sh`, free), the estimated cost and the exact question, then wait for the user's OK. After it runs, report credits consumed and the new balance.
- **No hardcoded thresholds:** all filter and scoring numbers live in `config/*.yaml` (experimental v1); Python only reads them.
- **No invented data:** never estimate or fill in missing values; mark them `N/A`.
- Never answer TikTok Shop data questions from memory; always query KaloPilot.
- Never print or commit the token. Secrets live in `~/.kalopilot/token` or env vars (`KALOPILOT_TOKEN`, `KALODATA_API_KEY`).
- **Scoring (WPS v1, `config/scoring.yaml`):** every score is calculated only from the numeric rules in that file. Claude never assigns, adjusts or judges a score subjectively. Missing data → metric `N/A`, 0 points, lower Confidence Score, raw data preserved. Do not change weights or rules unless the user asks.
- Do not push to GitHub until the user says the project is finished.

## Useful commands

```bash
bash scripts/setup-token.sh      # save/check token (free)
bash scripts/credits.sh          # credit balance (free)
bash scripts/ask.sh "<question>" # run a KaloPilot query (spends credits)
```

## Layout

| Path | Purpose |
|---|---|
| `config/` | Scoring weights, filters, target categories (YAML) |
| `prompts/` | Question templates sent to KaloPilot |
| `scripts/` | Python/bash tools for the pipeline |
| `data/raw/` | Unmodified API responses (git-ignored) |
| `data/processed/` | Validated, normalized data |
| `data/history/` | Dated snapshots for trend tracking |
| `reports/` | Generated reports |
| `tests/` | Tests for the scripts |
| `kalopilot/` | Official KaloPilot skill (MIT), vendored |
