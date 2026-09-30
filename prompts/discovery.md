# Discovery prompt

Purpose: find candidate products. Fill `{placeholders}` from `config/filters.yaml` and `config/categories.yaml`.

---

Using real current TikTok Shop {region} data for the last {time_window_days} days, list the top {limit} products{category_clause}{price_clause}.

For each product return: product name, KaloData product ID, product URL, category path, price range, GMV, units sold, revenue growth vs. the prior period, number of creators, number of videos, commission rate, and launch date.

Do not estimate or invent missing values; mark unavailable fields as N/A.
Do not run video script extraction or review/comment analysis.
Return the product list as a markdown table.
