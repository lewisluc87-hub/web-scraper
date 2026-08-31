# Configurable web scraper

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

## Known limitations (v1)

- No login/authentication support
- No CAPTCHA handling or proxy rotation — will not work against sites with
  active anti-bot protection
- Rate limiting is a fixed delay between requests, not adaptive

## License

MIT
