"""
monitor.py -- turns a one-off scrape into a monitored, recurring job.

Adds three things on top of the existing scraper.core.scrape():

1. Snapshots: every run's results are saved to disk with a timestamp, so
   the next run has something to compare against.
2. Diffing: compares the current scrape to the most recent previous
   snapshot and classifies each item as added, removed, or changed.
3. Alerting: writes a human-readable summary to a log file, and
   optionally POSTs a JSON payload to a webhook URL when something changed.

Deployment model: this does NOT run its own scheduler in production --
that's what cron / Windows Task Scheduler / a systemd timer are for.
`--run-once` (the default) does exactly one scrape-diff-alert cycle and
exits, which is what a cron job should call. `--interval SECONDS` is
provided as a convenience for demos/local testing (loops in-process,
sleeping between runs) -- not intended as the real deployment mechanism.

Usage:
    python -m scraper.monitor --config configs/quotes_static.json --run-once
    python -m scraper.monitor --config configs/quotes_static.json --interval 3600
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import requests

from scraper.cli import load_config
from scraper.core import scrape

logger = logging.getLogger("scraper.monitor")


# ---------------------------------------------------------------------------
# Keying: identify "the same item" across two runs
# ---------------------------------------------------------------------------
def compute_key(item: dict, key_field: str | None) -> str:
    """
    Return a stable identifier for an item, for matching across runs.

    If `key_field` is given and present on the item, use its value directly
    (e.g. a product URL -- the natural, stable identifier for a listing).
    Otherwise, fall back to a hash of the item's sorted field values, so
    items can still be matched even without a dedicated unique field --
    though in that fallback case, any field changing makes it look like a
    different item entirely (removed + added) rather than "changed", since
    there's no stable anchor. Prefer setting key_field when one exists.
    """
    if key_field and item.get(key_field) not in (None, ""):
        return str(item[key_field])
    stable_repr = json.dumps(item, sort_keys=True, default=str)
    return hashlib.sha256(stable_repr.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Diffing
# ---------------------------------------------------------------------------
@dataclass
class DiffResult:
    added: list[dict] = field(default_factory=list)
    removed: list[dict] = field(default_factory=list)
    changed: list[tuple[dict, dict]] = field(default_factory=list)  # (old, new)
    unchanged_count: int = 0

    @property
    def has_changes(self) -> bool:
        return bool(self.added or self.removed or self.changed)


def diff_results(previous: list[dict], current: list[dict], key_field: str | None) -> DiffResult:
    prev_by_key = {compute_key(item, key_field): item for item in previous}
    cur_by_key = {compute_key(item, key_field): item for item in current}

    _warn_if_key_collides(current, cur_by_key, key_field)

    diff = DiffResult()
    for key, cur_item in cur_by_key.items():
        if key not in prev_by_key:
            diff.added.append(cur_item)
        elif prev_by_key[key] != cur_item:
            diff.changed.append((prev_by_key[key], cur_item))
        else:
            diff.unchanged_count += 1

    for key, prev_item in prev_by_key.items():
        if key not in cur_by_key:
            diff.removed.append(prev_item)

    return diff


def _warn_if_key_collides(items: list[dict], keyed: dict, key_field: str | None) -> None:
    """If multiple items collapsed onto the same key, items silently
    disappeared from the diff -- this bit us for real during testing
    (quotes_static.json's 'author_link' isn't unique: one author can have
    several quotes, so 6 Einstein quotes collapsed into 1 key and 5 were
    silently dropped from the comparison). Surface this loudly instead of
    letting it fail silently."""
    if len(keyed) < len(items):
        dropped = len(items) - len(keyed)
        logger.warning(
            f"--key-field '{key_field}' is not unique: {len(items)} items collapsed to "
            f"{len(keyed)} distinct keys ({dropped} item(s) silently excluded from this diff). "
            f"Choose a field that's unique per item (e.g. a per-item URL, not a shared author/category link)."
        )


def format_diff_report(diff: DiffResult, config_name: str, timestamp: str) -> str:
    lines = [
        f"[{timestamp}] {config_name}: "
        f"+{len(diff.added)} added, -{len(diff.removed)} removed, "
        f"~{len(diff.changed)} changed, {diff.unchanged_count} unchanged"
    ]
    for item in diff.added:
        lines.append(f"  + {_summarize_item(item)}")
    for item in diff.removed:
        lines.append(f"  - {_summarize_item(item)}")
    for old, new in diff.changed:
        changed_fields = [k for k in new if old.get(k) != new.get(k)]
        lines.append(f"  ~ {_summarize_item(new)} (changed: {', '.join(changed_fields)})")
    return "\n".join(lines)


def _summarize_item(item: dict) -> str:
    """First couple of fields, for a compact one-line summary."""
    parts = [f"{k}={v!r}" for k, v in list(item.items())[:2]]
    return ", ".join(parts)


# ---------------------------------------------------------------------------
# Snapshot storage
# ---------------------------------------------------------------------------
def snapshot_path(snapshot_dir: Path, config_name: str, timestamp: str) -> Path:
    safe_name = config_name.replace("/", "_").replace("\\", "_")
    return snapshot_dir / safe_name / f"{timestamp}.json"


def save_snapshot(snapshot_dir: Path, config_name: str, results: list[dict], timestamp: str) -> Path:
    path = snapshot_path(snapshot_dir, config_name, timestamp)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def load_latest_snapshot(snapshot_dir: Path, config_name: str) -> list[dict] | None:
    """Return the most recent previous snapshot's results, or None if this
    is the first run (nothing to diff against yet)."""
    safe_name = config_name.replace("/", "_").replace("\\", "_")
    folder = snapshot_dir / safe_name
    if not folder.exists():
        return None
    snapshots = sorted(folder.glob("*.json"))
    if not snapshots:
        return None
    return json.loads(snapshots[-1].read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Alerting
# ---------------------------------------------------------------------------
def append_log(log_path: Path, report: str) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(report + "\n")


def send_webhook_alert(webhook_url: str, diff: DiffResult, config_name: str, timestamp: str) -> None:
    """POST a JSON summary to a webhook. Failure to deliver the alert is
    logged, not fatal -- a broken webhook shouldn't crash the monitor run
    or prevent the snapshot from being saved."""
    payload = {
        "config": config_name,
        "timestamp": timestamp,
        "added_count": len(diff.added),
        "removed_count": len(diff.removed),
        "changed_count": len(diff.changed),
        "added": diff.added,
        "removed": diff.removed,
        "changed": [{"old": old, "new": new} for old, new in diff.changed],
    }
    try:
        resp = requests.post(webhook_url, json=payload, timeout=10)
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.error(f"Webhook alert failed to deliver: {e}")


# ---------------------------------------------------------------------------
# One full monitor cycle
# ---------------------------------------------------------------------------
def run_once(
    config_path: str,
    snapshot_dir: Path,
    log_path: Path,
    webhook_url: str | None = None,
    key_field: str | None = None,
) -> DiffResult:
    config_name = Path(config_path).stem
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")

    config = load_config(config_path)
    current_results = scrape(config)

    previous_results = load_latest_snapshot(snapshot_dir, config_name)

    if previous_results is None:
        logger.info(f"No previous snapshot for '{config_name}' -- this is the first run, nothing to diff.")
        diff = DiffResult(added=current_results)
    else:
        diff = diff_results(previous_results, current_results, key_field)

    report = format_diff_report(diff, config_name, timestamp)
    logger.info(report)
    append_log(log_path, report)

    if diff.has_changes and webhook_url:
        send_webhook_alert(webhook_url, diff, config_name, timestamp)

    save_snapshot(snapshot_dir, config_name, current_results, timestamp)

    return diff


def main():
    parser = argparse.ArgumentParser(description="Scheduled monitoring wrapper around scraper.core.scrape()")
    parser.add_argument("--config", required=True, help="Path to a scrape config JSON file")
    parser.add_argument("--snapshot-dir", default="snapshots", help="Where to store historical snapshots")
    parser.add_argument("--log-file", default="monitor.log", help="Where to append diff reports")
    parser.add_argument("--webhook-url", default=None, help="Optional URL to POST a JSON alert to when something changes")
    parser.add_argument("--key-field", default=None, help="Field to use as each item's stable identity across runs (e.g. 'url'). Falls back to a content hash if omitted.")
    parser.add_argument("--interval", type=float, default=None, help="If set, loop forever, sleeping this many seconds between runs (for demos -- use cron/Task Scheduler for real deployment)")
    parser.add_argument("--run-once", action="store_true", help="Run exactly one cycle and exit (default behavior if --interval is not set)")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    snapshot_dir = Path(args.snapshot_dir)
    log_path = Path(args.log_file)

    if args.interval:
        logger.info(f"Starting monitor loop, checking every {args.interval}s (Ctrl+C to stop)")
        while True:
            run_once(args.config, snapshot_dir, log_path, args.webhook_url, args.key_field)
            time.sleep(args.interval)
    else:
        run_once(args.config, snapshot_dir, log_path, args.webhook_url, args.key_field)


if __name__ == "__main__":
    main()