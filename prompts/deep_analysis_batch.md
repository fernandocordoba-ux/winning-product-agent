# Deep analysis prompt — BATCH (Step T, credit-saving mode)

Purpose: the same data as `prompts/deep_analysis.md`, for SEVERAL shortlisted products in
ONE query (`query_plan.deep_batch_size`, e.g. 5 products -> 1 query instead of 5).
The runner splits the answer per product (matched by product_id, never by guess),
keeps the whole answer as the raw batch file and saves one derived raw record per product.
The key list must stay identical to `prompts/deep_analysis.md` (a test enforces it).

Everything between the two `===` lines is sent to KaloPilot.

===
Using real current TikTok Shop {region} data (currency {currency}), analyze each of these {n} products in depth for the last {period_days} days:
{product_lines}

Do not run video script extraction or review/comment analysis.
Do not estimate or invent missing values. Use null for any value that is not available. A real zero must be returned as 0, not null.
Revenue values must be the revenue generated for EACH product in the period, in exact {currency} (not rounded, no "k"/"M").

Return the result as ONE fenced JSON code block (```json ... ```) containing an ARRAY with ONE object per product (same order as above, "product_id" exactly as given), each with exactly these keys:
"product_id", "product_name", "product_url", "category_path", "category_id", "shop_id", "shop_name",
"price_min", "price_max", "price_history" (array of {{"date": "YYYY-MM-DD", "price": number}} if available, else null),
"gmv_30d", "units_30d", "growth_30d_pct" (revenue growth vs. prior {period_days} days, %), "category_growth_pct",
"launch_date" (YYYY-MM-DD, first seen / listing date), "data_window_end" (YYYY-MM-DD), "commission_pct",
"daily_gmv" (array of {period_days} daily revenue values, oldest first; null for days without data),
"daily_units" (same, units),
"creator_count", "selling_creator_count", "creator_growth_pct", "daily_creator_count" (array oldest first, or null),
"top_creators" (up to {top_creators} objects, highest revenue first: {{"creator_id", "name", "revenue", "growth_pct"}}),
"video_count", "selling_video_count", "video_growth_pct", "video_sales_share_pct" (% of product revenue from videos), "daily_video_count" (array oldest first, or null),
"top_videos" (up to {top_videos} objects, highest revenue first: {{"video_id", "creator", "revenue", "views"}}),
"shop_count" (shops selling this product), "similar_listings_count", "category_product_count" (products selling in the same category).
If a product cannot be found, still return its object with its "product_id" and null values.
===
