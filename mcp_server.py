"""MCP server exposing the configurable scraper as tools.

Wraps the existing scraper.core / scraper.cli / scraper.monitor code
rather than reimplementing it -- a fix to the parsing or diff logic
applies here automatically.

Deliberately restricted: no tool accepts an arbitrary URL or arbitrary
CSS selectors from the caller. Every tool that fetches anything takes a
config_name resolved only against configs/ already committed to this
repo. This bounds what a client can make this server do to "the sites
this repo already decided to scrape" -- not an open-ended web-fetching
tool that a client (or anything able to influence its tool-call
arguments) could point at arbitrary internal or third-party sites.

Usage (stdio transport, what Claude Desktop expects):
    python mcp_server.py
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from mcp.server.mcpserver import MCPServer

from scraper.cli import load_config
from scraper.core import scrape as run_scrape
from scraper.monitor import diff_results, format_diff_report, load_latest_snapshot

# Overridable via env var so tests can point these at an isolated temp
# directory instead of this repo's real configs/ and snapshots/ --
# without needing to touch (or pollute) real repo state to test the tools.
CONFIGS_DIR = Path(os.environ.get("SCRAPER_MCP_CONFIGS_DIR", Path(__file__).parent / "configs"))
SNAPSHOT_DIR = Path(os.environ.get("SCRAPER_MCP_SNAPSHOT_DIR", Path(__file__).parent / "snapshots"))

server = MCPServer(
    "web-scraper",
    description=(
        "Runs pre-configured scrape jobs from this repo's configs/ directory. "
        "Does not accept arbitrary URLs or CSS selectors -- only named configs "
        "already committed to the repo."
    ),
)


def _resolve_config_path(config_name: str) -> Path:
    """Resolve config_name to a real file inside CONFIGS_DIR, rejecting
    anything else. This is the actual safety boundary for the scrape
    tool: Path(...).name strips any directory components (so
    "../../etc/passwd" resolves to a "passwd.json" lookup inside
    CONFIGS_DIR, not an actual traversal), and the explicit parent-dir
    check below is defense-in-depth on top of that, not the only guard.
    """
    name = Path(config_name).name
    if name.endswith(".json"):
        name = name[:-5]
    candidate = CONFIGS_DIR / f"{name}.json"
    if not candidate.is_file() or candidate.resolve().parent != CONFIGS_DIR.resolve():
        raise ValueError(
            f"Unknown config '{config_name}'. Use list_configs() to see available configs."
        )
    return candidate


@server.tool()
def list_configs() -> str:
    """List the scrape configs available in this repo's configs/ directory."""
    configs = sorted(CONFIGS_DIR.glob("*.json"))
    if not configs:
        return "No configs found."
    lines = []
    for path in configs:
        try:
            data = json.loads(path.read_text())
            lines.append(
                f"- {path.stem}: {data.get('base_url', '?')} "
                f"(fetch_mode: {data.get('fetch_mode', 'static')})"
            )
        except (json.JSONDecodeError, OSError) as e:
            lines.append(f"- {path.stem}: [could not read config: {e}]")
    return "\n".join(lines)


@server.tool()
def scrape(config_name: str, max_pages: int | None = None) -> str:
    """Run a pre-configured scrape job and return the extracted items as JSON.

    config_name must be one of the configs listed by list_configs() --
    this tool does not accept arbitrary URLs or selectors. max_pages
    optionally overrides the config's own max_pages for this call.
    """
    path = _resolve_config_path(config_name)
    config = load_config(str(path))
    if max_pages is not None:
        config.max_pages = max_pages

    results = run_scrape(config)
    return json.dumps(
        {"config": config_name, "item_count": len(results), "items": results},
        indent=2,
        ensure_ascii=False,
    )


@server.tool()
def check_for_changes(config_name: str, key_field: str | None = None) -> str:
    """Scrape a config now and diff against its last saved snapshot -- read-only.

    Never saves a new snapshot, so repeated calls always compare against
    the same baseline (whatever `scraper.monitor` last saved) and this
    never interferes with the CLI monitor's own scheduled run history.
    To actually advance the baseline, run the CLI monitor directly:
    `python -m scraper.monitor --config configs/<name>.json --run-once`.
    """
    path = _resolve_config_path(config_name)
    config = load_config(str(path))
    current = run_scrape(config)

    previous = load_latest_snapshot(SNAPSHOT_DIR, config_name)
    if previous is None:
        return (
            f"No previous snapshot for '{config_name}' -- nothing to diff against yet "
            f"(scraped {len(current)} items just now). Run the CLI monitor at least "
            f"once to create a baseline: python -m scraper.monitor --config "
            f"{path} --run-once"
        )

    diff = diff_results(previous, current, key_field)
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    return format_diff_report(diff, config_name, timestamp)


def main():
    server.run()


if __name__ == "__main__":
    sys.exit(main())
