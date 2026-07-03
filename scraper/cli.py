"""
CLI entry point.

Usage:
  python -m scraper.cli --config configs/quotes_static.json --output out.csv
  python -m scraper.cli --config configs/quotes_static.json --output out.json --format json
  python -m scraper.cli --config configs/quotes_rendered.json --output out.csv --pages 3
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
from pathlib import Path

from scraper.core import ScrapeConfig, FieldSpec, scrape


def load_config(path: str) -> ScrapeConfig:
    with open(path) as f:
        raw = json.load(f)

    fields = [FieldSpec(**f) for f in raw["fields"]]
    raw = {**raw, "fields": fields}
    return ScrapeConfig(**raw)


def write_csv(rows: list[dict], path: str):
    if not rows:
        Path(path).write_text("")
        return
    # union of all keys, in first-seen order, so no field is silently dropped
    fieldnames = []
    for row in rows:
        for k in row.keys():
            if k not in fieldnames:
                fieldnames.append(k)

    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            # lists (multiple=True fields) get flattened to a pipe-joined string for CSV
            flat = {k: ("|".join(v) if isinstance(v, list) else v) for k, v in row.items()}
            writer.writerow(flat)


def write_json(rows: list[dict], path: str):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rows, f, indent=2, ensure_ascii=False)


def main():
    parser = argparse.ArgumentParser(description="Configurable web scraper — static or JS-rendered pages")
    parser.add_argument("--config", required=True, help="Path to a scrape config JSON file")
    parser.add_argument("--output", required=True, help="Output file path")
    parser.add_argument("--format", choices=["csv", "json"], default="csv")
    parser.add_argument("--pages", type=int, default=None, help="Override max_pages from config")
    parser.add_argument("--delay", type=float, default=None, help="Override delay_seconds from config")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt="%H:%M:%S",
    )

    config = load_config(args.config)
    if args.pages is not None:
        config.max_pages = args.pages
    if args.delay is not None:
        config.delay_seconds = args.delay

    results = scrape(config)

    if not results:
        logging.warning("No items extracted. Check your selectors / config.")
        sys.exit(1)

    if args.format == "csv":
        write_csv(results, args.output)
    else:
        write_json(results, args.output)

    logging.info(f"Wrote {len(results)} items to {args.output}")


if __name__ == "__main__":
    main()
