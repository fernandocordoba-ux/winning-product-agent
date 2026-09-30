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
| `scripts/validate_data.py` | Validate raw data → `data/processed/` (skeleton) |
| `scripts/score_products.py` | Score products with `config/scoring.yaml` (not implemented yet) |
| `scripts/generate_report.py` | Build reports in `reports/` (skeleton) |
| `config/` | `scoring.yaml`, `filters.yaml`, `categories.yaml` |
| `prompts/` | `discovery.md`, `deep_analysis.md`, `validation.md` |
| `data/raw/` | Raw API responses (git-ignored) |
| `data/processed/`, `data/history/` | Validated data and dated snapshots |
| `reports/`, `tests/` | Generated reports, tests |
| `CLAUDE.md` | Rules Claude follows in this project |
| `kalopilot/` | Official KaloPilot skill by Kalodata (MIT), vendored |

The token and secrets are **never** committed (see `.gitignore`: `.env`, `.env.*`, `secrets/`, `data/raw/`).
