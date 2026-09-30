# Discovery follow-up prompt (Step U)

Purpose: sent with the SAME task_id as an unfinished Discovery task (KaloPilot ended it
as "completed" without the product JSON block). KaloPilot keeps the conversation context,
so it only has to output what it already fetched. Same keys as `prompts/discovery.md`.
Used only by: `python -m winning_product_agent run --live ... --continue-task <task_id>`.

Everything between the two `===` lines is sent to KaloPilot.

===
Your previous answer in this conversation ended before the final result was delivered. Please finish that same request now: use the products and data you already fetched above (fetch more only if strictly necessary) and return the final result.

Return up to {limit} products as ONE fenced JSON code block (```json ... ```), an array of objects with exactly these keys:
"product_id" (KaloData/TikTok product ID as a string), "product_name", "product_url", "shop_id", "shop_name", "category_path", "category_id", "price_min", "price_max", "gmv_30d", "units_30d", "growth_30d_pct" (revenue growth vs. prior period, %), "creator_count", "selling_creator_count", "creator_growth_pct", "video_count", "video_growth_pct", "shop_count" (sellers offering this product, if available), "launch_date" (YYYY-MM-DD), "data_window_end" (YYYY-MM-DD).
Do not use the Category Overview module (it is not included in this account's plan). If any value is only available from a module the plan does not include, do not pause or ask for confirmation: skip it and use null for that value.
Do not estimate or invent missing values. Use null for any value that is not available. A real zero must be returned as 0, not null.
Numbers must be plain numbers in {currency} with no symbols or abbreviations (write 4206171, not "$4.2M").
===
