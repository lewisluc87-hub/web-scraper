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

## Known limitations (v1)

- No login/authentication support
- No CAPTCHA handling or proxy rotation — will not work against sites with
  active anti-bot protection
- Rate limiting is a fixed delay between requests, not adaptive

## License

MIT
