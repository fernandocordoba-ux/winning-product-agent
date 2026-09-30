# Amazon validation prompt — BATCH (Step T, credit-saving mode)

Purpose: the same search as `prompts/amazon_validation.md` for SEVERAL eligible products in
ONE query (`query_plan.amazon_batch_size`). Split per product by product_id; the matching,
AVS and Amazon Confidence are still calculated by scripts/amazon_validation.py.

Everything between the two `===` lines is sent to KaloPilot.

===
Search Amazon US (amazon.com) for products that are the same as, or the closest equivalent to, each of these {n} TikTok Shop products:
{product_lines}

For EACH product return up to {max_candidates} Amazon US listings as candidates, most similar first. Do not choose or judge the best match; return the raw listing data only. If nothing similar exists on Amazon US, return an empty candidates array for that product.

Only report values you actually found on Amazon or in a data source you can cite. Do not estimate or invent any value. Use null for anything not available. A real zero must be 0, not null. Numbers must be plain numbers (no "$", no "k").

Return ONE fenced JSON code block (```json ... ```) with an ARRAY containing ONE object per product ("product_id" exactly as given):
[
  {{
    "product_id": string,
    "candidates": [
      {{"title": string, "url": string, "asin": string, "brand": string, "seller": string,
        "category_path": string, "price": number, "rating": number, "review_count": number,
        "bsr": number, "bsr_category": string,
        "monthly_sales_estimate": number, "monthly_sales_source": string,
        "listing_date": "YYYY-MM-DD", "attributes": [string]}}
    ],
    "search": {{
      "keyword": string,
      "comparable_listings_count": number,
      "comparable_price_min": number, "comparable_price_max": number,
      "top_brands": [{{"brand": string, "share_pct": number}}],
      "recent_trend": "growing" | "stable" | "declining" | null,
      "sources": [string]
    }}
  }}
]
===
