"""Validate raw KaloPilot data (data/raw/) and write clean records to data/processed/.

Rules: prompts/validation.md. Missing values become "N/A" — never estimated.
Skeleton only; implementation pending.
"""
import sys

REQUIRED_FIELDS = [
    "product_name", "product_id", "category", "price",
    "gmv", "units_sold", "growth", "creators", "videos",
]


def validate_product(record):
    """Return (clean_record, warnings). TODO: implement checks from prompts/validation.md."""
    raise NotImplementedError


def main():
    print("validate_data.py: not implemented yet.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
