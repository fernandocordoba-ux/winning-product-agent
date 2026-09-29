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
| `scripts/ask.sh` | Submit a question, poll until done, print + save the answer to `reports/` |
| `kalopilot/` | Official KaloPilot skill by Kalodata (MIT), vendored |

The token is **never** committed (see `.gitignore`).
