# Discovery prompt (Step L)

Purpose: cheap, broad first pass to find candidate products BEFORE deep analysis.
One query per enabled category in `config/categories.yaml`. `scripts/discovery.py`
fills the `{placeholders}` from `config/filters.yaml` and `config/categories.yaml`;
thresholds are never typed by hand.

Rules for this prompt:
- Ask for momentum, NOT the highest total GMV.
- Ask for a machine-readable JSON block (the pipeline parses only that block).
- No creator/video/shop deep dives, no script extraction, no review analysis (Step M).

Everything between the two `===` lines is sent to KaloPilot.

===
Using real current TikTok Shop {region} data (currency {currency}) for the last {period_days} days, find up to {limit} emerging products in the category "{category_name}" (KaloData category: {kalodata_match}).

Selection (discovery only, keep it lightweight):
- Prefer products with strong recent revenue growth vs. the prior {period_days} days and increasing sales velocity.
- Only include products with GMV of at least {gmv_min} {currency} and at least {units_min} units sold in the period, and an average price between {price_min} and {price_max} {currency}.
- Do NOT simply rank by total GMV. Rank by revenue growth (highest first).
- Prefer products where creator participation and video activity are increasing, when that data is available.

Do not run deep analysis, video script extraction, review/comment analysis, or per-creator/per-video breakdowns.
Do not estimate or invent missing values. Use null for any value that is not available. A real zero must be returned as 0, not null.

Return the result as ONE fenced JSON code block (```json ... ```), an array of objects with exactly these keys:
"product_id" (KaloData/TikTok product ID as a string), "product_name", "product_url", "shop_id", "shop_name", "category_path", "category_id", "price_min", "price_max", "gmv_30d", "units_30d", "growth_30d_pct" (revenue growth vs. prior period, %), "creator_count", "selling_creator_count", "creator_growth_pct", "video_count", "video_growth_pct", "shop_count" (sellers offering this product, if available), "launch_date" (YYYY-MM-DD), "data_window_end" (YYYY-MM-DD).
Numbers must be plain numbers in {currency} with no symbols or abbreviations (write 4206171, not "$4.2M").
===
