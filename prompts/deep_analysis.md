# Deep analysis prompt

Purpose: analyze one shortlisted product in depth. Fill `{placeholders}`.

---

Analyze this TikTok Shop {region} product in depth: {product_url_or_id}

Return:
- Daily sales trend for the last {time_window_days} days
- Revenue growth vs. the prior period and category growth
- Top {creator_limit} creators (at least 3): followers, revenue generated for THIS product in the period (exact USD), growth, units, views
- Top {video_limit} videos (at least 3): video ID, title, creator, revenue generated for THIS product in the period (exact USD, not rounded), views
- Total number of creators and total number of videos promoting the product in the period
- Share of sales from videos vs. livestreams vs. shop page
- Main competing products in the same category

Do not estimate or invent missing values; mark unavailable fields as N/A.
Do not run video script extraction or review/comment analysis unless explicitly requested.
