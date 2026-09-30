# Validation rules

Checks `scripts/validate_data.py` applies to KaloPilot data before scoring. Nothing is estimated: failed or missing values become `N/A`.

## Required fields per product
product_name, product_id, category, price, gmv, units_sold, growth, creators, videos

## Checks
- Missing or empty value → `N/A` (never filled in)
- Numbers parse correctly (strip `$`, `,`, `%`, `k`/`M` suffixes only when the source value is explicit)
- No negative GMV, units, creators or videos
- Price min ≤ price max
- Region and currency match `config/filters.yaml`
- Time window recorded with the data
- Duplicate product IDs flagged
- Source (`task_id`, `report_url`, fetch date) kept for traceability

## Output
Validated records → `data/processed/`, with a list of warnings per product.
