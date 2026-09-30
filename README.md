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
| `scripts/concentration.py` | Creator/video revenue concentration + dependency flags (tested) |
| `scripts/validate_data.py` | Validate raw data → `data/processed/` (skeleton) |
| `scripts/score_products.py` | Scoring engine: WPS (not implemented yet → N/A) + Confidence Score |
| `scripts/confidence.py` | Confidence Score: data completeness/quality, 0–100 + breakdown (tested) |
| `scripts/generate_report.py` | Build reports in `reports/` (skeleton) |
| `config/` | `scoring.yaml`, `filters.yaml`, `categories.yaml` |
| `prompts/` | `discovery.md`, `deep_analysis.md`, `validation.md` |
| `data/raw/` | Raw API responses (git-ignored) |
| `data/processed/`, `data/history/` | Validated data and dated snapshots |
| `reports/` | Generated reports |
| `tests/` | `python3 -m unittest discover tests` |
| `CLAUDE.md` | Rules Claude follows in this project |
| `kalopilot/` | Official KaloPilot skill by Kalodata (MIT), vendored |

The token and secrets are **never** committed (see `.gitignore`: `.env`, `.env.*`, `secrets/`, `data/raw/`).
