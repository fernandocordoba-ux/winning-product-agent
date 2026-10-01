# Discovery lenses (Step AE experiment) — three ways to ask KaloPilot for candidates

Each lens is ONE query for ONE category. Everything between `=== <lens>` and the next `===` line is sent.
Placeholders come from config (market, period, thresholds). The JSON keys are identical for all lenses so
the same parser and preliminary score apply. 90 days are requested as three 30-day blocks.

=== proven
Using real current TikTok Shop {region} data (currency {currency}), find up to {limit} PROVEN best-selling products in the category "{category_name}" (KaloData category: {kalodata_match}).

Selection:
- Rank by units sold in the last 30 days (highest first), among products whose revenue in the last 30 days is at least as high as in the previous 30 days (steady or rising, not falling).
- Only products with at least {units_min} units and {gmv_min} {currency} revenue in the last 30 days, average price between {price_min} and {price_max} {currency}.
{common_rules}
=== rising
Using real current TikTok Shop {region} data (currency {currency}), find up to {limit} products in the category "{category_name}" (KaloData category: {kalodata_match}) with SUSTAINED growth over the last 90 days.

Selection:
- Revenue must have grown in BOTH steps: days 31-60 vs days 61-90, AND last 30 days vs days 31-60. Exclude one-month spikes.
- Revenue in days 31-60 must be at least {base_min} {currency} (growth from a tiny base does not count).
- Rank by revenue growth of the last 30 days vs days 31-60 (highest first).
- Average price between {price_min} and {price_max} {currency}.
{common_rules}
=== creators
Using real current TikTok Shop {region} data (currency {currency}), find up to {limit} products in the category "{category_name}" (KaloData category: {kalodata_match}) where creator activity is accelerating.

Selection:
- Rank by growth in the number of creators selling the product in the last 30 days vs the previous 30 days (highest first); use new videos as tie-break.
- Only products with at least {units_min} units in the last 30 days and average price between {price_min} and {price_max} {currency}.
{common_rules}
=== common
- Exclude products sold by well-known established brands or their official brand stores; prefer generic / unbranded products that independent sellers can source from dropshipping suppliers.
- Exclude seasonal holiday items (Halloween, Christmas, Thanksgiving, Easter, Valentine's).
Do not run deep analysis, video script extraction, review/comment analysis, or per-creator/per-video breakdowns.
Do not use the Category Overview module (it is not included in this account's plan). If any value is only available from a module the plan does not include, do not pause or ask for confirmation: skip it and use null for that value.
Do not estimate or invent missing values. Use null for any value that is not available. A real zero must be returned as 0, not null.

Return the result as ONE fenced JSON code block (```json ... ```), an array of objects with exactly these keys:
"product_id" (KaloData/TikTok product ID as a string), "product_name" (max 80 characters), "shop_name", "price_min", "price_max", "gmv_30d" (last 30 days), "gmv_prev_30d" (days 31-60), "gmv_prev2_30d" (days 61-90), "units_30d", "units_prev_30d", "creator_count", "creator_growth_pct", "video_count", "shop_count", "launch_date" (YYYY-MM-DD), "data_window_end" (YYYY-MM-DD).
Keep the answer short: no commentary before or after the JSON block (long answers are cut off and the data is lost).
Numbers must be plain numbers in {currency} with no symbols or abbreviations (write 4206171, not "$4.2M").
===
