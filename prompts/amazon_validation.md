# Amazon validation prompt (Step N)

Purpose: collect RAW Amazon US evidence for one TikTok Shop product.
The provider returns candidate listings and search-level data only. It does NOT
decide the match: `scripts/amazon_validation.py` computes match confidence,
signals, AVS and Amazon Confidence deterministically from these values.

Note: KaloPilot has no dedicated Amazon dataset; it can only use web search.
Fields it cannot verify must come back as null (never estimated).

Everything between the two `===` lines is sent to the provider.

===
Search Amazon US (amazon.com) for products that are the same as, or the closest equivalent to, this TikTok Shop product:

Name: {tiktok_name}
Category: {tiktok_category}
Brand/shop: {tiktok_brand}
Price on TikTok Shop: {tiktok_price} USD

Return up to {max_candidates} Amazon US listings as candidates, most similar first. Do not choose or judge the best match; return the raw listing data only. If nothing similar exists on Amazon US, return an empty candidates array.

Only report values you actually found on Amazon or in a data source you can cite. Do not estimate or invent any value. Use null for anything not available. A real zero must be 0, not null. Numbers must be plain numbers (no "$", no "k").

Return ONE fenced JSON code block (```json ... ```) with ONE object:
{{
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
===
