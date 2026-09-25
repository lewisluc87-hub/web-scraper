# Configurable web scraper

[![CI](https://github.com/lewisluc87-hub/web-scraper/actions/workflows/ci.yml/badge.svg)](https://github.com/lewisluc87-hub/web-scraper/actions/workflows/ci.yml)

A CLI tool that scrapes listing-style pages (product listings, quotes, job posts,
articles — anything repeated on a page) into CSV or JSON. Works on both static
HTML sites and JavaScript-rendered sites, and adapts to any target site by
editing a config file — no code changes needed per site.

## Why configurable, not hardcoded

Every scraping job targets a different site with different HTML structure.
Instead of writing one-off scripts per site, this tool takes a JSON config
(item selector, field selectors, pagination selector, fetch mode) so pointing
it at a new site is a config edit, not a rewrite.

## Install

```bash
pip install -r requirements.txt
playwright install chromium   # only needed for fetch_mode: "rendered"
```

## Usage

```bash
# Static HTML site
python -m scraper.cli --config configs/quotes_static.json --output quotes.csv

# JS-rendered site (uses headless Chromium via Playwright)
python -m scraper.cli --config configs/quotes_rendered.json --output quotes.json --format json

# Override page count / delay from the command line
python -m scraper.cli --config configs/quotes_static.json --output quotes.csv --pages 5 --delay 2.0
```

## Config format

```json
{
  "base_url": "https://example.com/listings/",
  "item_selector": "div.item",
  "fields": [
    {"name": "title", "selector": ".title"},
    {"name": "price", "selector": ".price"},
    {"name": "url", "selector": "a", "attr": "href"},
    {"name": "tags", "selector": ".tag", "multiple": true}
  ],
  "fetch_mode": "static",
  "next_page_selector": "a.next",
  "max_pages": 5,
  "delay_seconds": 1.0
}
```

- `fetch_mode`: `"static"` (fast, requests+BeautifulSoup) or `"rendered"` (Playwright, for JS-loaded content)
- `attr`: omit for text content, set to `"href"`/`"src"`/etc. to pull an attribute instead
- `multiple`: set `true` when a field can have several values per item (e.g. tags)
- `wait_selector` (rendered mode only): CSS selector to wait for before reading the page, so you don't scrape before JS has finished loading content

See `configs/ecommerce_template.json` for a starting point when adapting this to a client's product listing page.

## Demo target

Configs are set up against [quotes.toscrape.com](https://quotes.toscrape.com/),
a public sandbox built specifically for scraping practice (both a static and a
JS-rendered version exist at `/` and `/js/`), so the demo doesn't touch a real
business's site or raise any ToS concerns.

The same `item_selector` / `fields` pattern works identically for e-commerce
product listings, job boards, or article listings — only the selectors change.

## Verification

**Stated before building, as agreed:**
- Parsing logic verified against real HTML pulled from the live site (not
  synthetic), covering both a normal page and a page with missing/malformed
  fields (`fixtures/quotes_malformed.html`)
- Confirmed extracted item count matches the actual page content
- Confirmed pagination link is found and relative URLs resolve correctly
- Confirmed a missing field doesn't crash the run — it's logged and returns
  `None`/`[]` for that field, item still included
- Confirmed CSV and JSON output writers produce correct, properly escaped output

**Run the offline test suite (no network required):**
```bash
python tests/test_parser.py
```
Result: 6/6 passed.

**Honest limitation:** the fetch layer (actually requesting live pages,
following real pagination, and rendering JS) needs real network access to run
end-to-end — the offline tests above verify the parsing/extraction logic is
correct using a saved real HTML fixture, not that the network layer works
against the live internet right now. To confirm the full pipeline end-to-end
yourself:

```bash
python -m scraper.cli --config configs/quotes_static.json --output test.csv --pages 2
# then check test.csv — should have 20 rows (10 quotes x 2 pages)

python -m scraper.cli --config configs/quotes_rendered.json --output test_js.csv --pages 1
# then check test_js.csv — should have 10 rows, confirming JS-rendered content was captured
```

## Scheduled monitoring

`scraper.monitor` wraps the existing `scrape()` engine with three things
a one-off script doesn't have: history, diffing, and alerting.

```
python -m scraper.monitor --config configs/quotes_static.json --run-once
```

Each run:
1. Scrapes normally (same config format, same engine).
2. Loads the most recent previous snapshot (if any).
3. Diffs current vs. previous, classifying every item as **added**,
   **removed**, or **changed** (with the specific changed fields named).
4. Appends a human-readable summary to a log file.
5. Optionally POSTs a JSON payload to a webhook URL if anything changed.
6. Saves the current results as the new "most recent" snapshot.

### Identifying "the same item" across runs

Pass `--key-field url` (or whichever field is a stable, unique identifier
in your config, e.g. a product URL). Without a natural unique field, it
falls back to hashing each item's content — this still catches new/removed
items correctly, but a genuinely *changed* item will show up as one
removal + one addition instead of a "changed" entry, since there's no
stable anchor to match old and new against. Setting `--key-field` when a
suitable field exists is strongly recommended.

### Flags

| Flag | Effect |
|---|---|
| `--snapshot-dir DIR` | Where historical snapshots are stored (default: `snapshots/`) |
| `--log-file PATH` | Where diff reports are appended (default: `monitor.log`) |
| `--webhook-url URL` | Optional: POST a JSON alert here when something changes |
| `--key-field NAME` | Field used to match items across runs |
| `--interval SECONDS` | Loop in-process, sleeping between runs (demo/local use) |
| `--run-once` | Run one cycle and exit (default; this is what a cron job should call) |

### Deployment model

This does **not** implement its own production scheduler. `--run-once`
(the default) does one scrape-diff-alert cycle and exits — that's what a
real cron job, Windows Task Scheduler task, or systemd timer should
invoke on a schedule. `--interval` is a convenience loop for demos and
local testing, not the intended production deployment mechanism.

### Verified

- 16 offline unit tests (`python tests/test_monitor.py`) covering key
  matching (including the fallback-hash tradeoff, explicitly locked in by
  a test), diff classification (added/removed/changed/unchanged), snapshot
  save/load round-tripping, and picking the most recent of several
  snapshots.
- Webhook delivery is tested against a **real local HTTP server**, not a
  mock — confirms the actual JSON payload structure delivered over a real
  HTTP POST, and separately confirms a failed/unreachable webhook doesn't
  crash the monitor run.
- Full pipeline verified against two real HTML snapshots representing a
  genuine day-1-to-day-2 change (one quote removed, one edited in place,
  one added, one left untouched) run through the actual `parse_page()` →
  `diff_results()` → `format_diff_report()` pipeline — correctly produced
  `+1 added, -1 removed, ~1 changed (tags), 1 unchanged`, exactly matching
  the real edit made.

## MCP server

`mcp_server.py` exposes this scraper as an MCP server, so any MCP client
(Claude Desktop, etc.) can run scrape jobs directly. It wraps
`scraper.core`/`scraper.cli`/`scraper.monitor` rather than
reimplementing them — a fix to the parsing or diff logic applies here
automatically.

**Deliberately restricted, not a general-purpose web-fetch tool:** no
tool accepts an arbitrary URL or arbitrary CSS selectors from the
caller. Every tool that fetches anything takes a `config_name` resolved
only against configs already committed to `configs/` — this bounds what
a client can make the server do to "the sites this repo already decided
to scrape," not an open-ended fetcher a client (or anything able to
influence its tool-call arguments) could point at arbitrary sites.

**Tools exposed:**
- `list_configs()` — lists the configs in `configs/` (name, base URL,
  fetch mode).
- `scrape(config_name, max_pages=None)` — runs a named config and
  returns the extracted items as JSON. Rejects anything that isn't a
  real file already in `configs/`, including path-traversal attempts.
- `check_for_changes(config_name, key_field=None)` — scrapes now and
  diffs against the last snapshot saved by the CLI monitor, **without
  saving a new snapshot**. This is deliberately read-only: persisting
  here would mean an MCP call could silently advance the same baseline
  `scraper.monitor`'s own scheduled runs depend on, corrupting whichever
  one runs next. To actually advance the baseline, run the CLI monitor
  directly (`python -m scraper.monitor --config configs/<name>.json
  --run-once`).

**Run it** (stdio transport, what Claude Desktop expects):
```bash
python mcp_server.py
```

**Claude Desktop config** (`claude_desktop_config.json`):
```json
{
  "mcpServers": {
    "web-scraper": {
      "command": "python",
      "args": ["/absolute/path/to/web-scraper/mcp_server.py"]
    }
  }
}
```

### Verified

- A real client (the `mcp` SDK's own `ClientSession`) driving the actual
  server as a subprocess over real stdio confirmed: all three tools are
  listed correctly; `list_configs()` reports this repo's real configs;
  `scrape()` correctly rejects an unknown config name and a
  path-traversal attempt; `check_for_changes()` correctly reports "no
  snapshot yet" on a fresh config.
- `scrape()` tested against a **real local HTTP server** (not a mock)
  serving the same `fixtures/quotes_page1.html` `test_parser.py` already
  uses — a real HTTP GET, real HTML parse, real MCP tool response,
  matching this repo's own established "real local server, not mocks"
  testing philosophy (see the webhook test above) rather than
  introducing a different standard for this one file.
- `check_for_changes()`'s read-only property directly verified, not just
  assumed from reading the code: pre-seeded a fake previous snapshot,
  called the tool (got a correct `+1 added, 2 unchanged` diff against
  the real scrape), then confirmed the snapshot folder still contained
  exactly the one pre-seeded file — no new snapshot had been written.

## Known limitations (v1)

- No login/authentication support
- No CAPTCHA handling or proxy rotation — will not work against sites with
  active anti-bot protection
- Rate limiting is a fixed delay between requests, not adaptive

## License

MIT
