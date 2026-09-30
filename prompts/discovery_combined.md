# Discovery prompt — COMBINED (Step T, credit-saving mode)

Purpose: the same lightweight Discovery as `prompts/discovery.md`, but ONE query for
several categories at once instead of one query per category (9 queries -> 1).
Used when the run profile sets `query_plan.discovery_mode: combined`
(e.g. `config/runtime_first_live.yaml`). Thresholds come from `config/filters.yaml`;
nothing is typed by hand. Each record must say which category key it belongs to so
per-category filters still apply.

Everything between the two `===` lines is sent to KaloPilot.

===
Using real current TikTok Shop {region} data (currency {currency}) for the last {period_days} days, find up to {limit} emerging products in total across these categories (key: name — KaloData category):
{category_lines}

Selection (discovery only, keep it lightweight):
- Prefer products with strong recent revenue growth vs. the prior {period_days} days and increasing sales velocity.
- Only include products with GMV of at least {gmv_min} {currency} and at least {units_min} units sold in the period, and an average price between {price_min} and {price_max} {currency}.
- Do NOT simply rank by total GMV. Rank by revenue growth (highest first).
- No more than {per_category_max} products from any single category.
- Prefer products where creator participation and video activity are increasing, when that data is available.

Do not run deep analysis, video script extraction, review/comment analysis, or per-creator/per-video breakdowns.
Do not use the Category Overview module (it is not included in this account's plan). If any value is only available from a module the plan does not include, do not pause or ask for confirmation: skip it and use null for that value.
Do not estimate or invent missing values. Use null for any value that is not available. A real zero must be returned as 0, not null.

Return the result as ONE fenced JSON code block (```json ... ```), an array of objects with exactly these keys:
"category_key" (one of the keys listed above), "product_id" (KaloData/TikTok product ID as a string), "product_name", "product_url", "shop_id", "shop_name", "category_path", "category_id", "price_min", "price_max", "gmv_30d", "units_30d", "growth_30d_pct" (revenue growth vs. prior period, %), "creator_count", "selling_creator_count", "creator_growth_pct", "video_count", "video_growth_pct", "shop_count" (sellers offering this product, if available), "launch_date" (YYYY-MM-DD), "data_window_end" (YYYY-MM-DD).
Numbers must be plain numbers in {currency} with no symbols or abbreviations (write 4206171, not "$4.2M").
===
