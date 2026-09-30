"""Score validated products using config/scoring.yaml.

NOT IMPLEMENTED YET — the scoring system will be defined later.
"""
import sys


def score_products(products, config):
    raise NotImplementedError("Scoring system not implemented yet.")


def main():
    print("score_products.py: scoring not implemented yet.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
