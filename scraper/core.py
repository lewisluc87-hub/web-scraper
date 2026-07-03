"""
Core scraping engine.

Two fetch strategies, chosen via config:
  - "static": requests + BeautifulSoup. Fast, low-resource. Works for
    server-rendered HTML.
  - "rendered": Playwright (headless Chromium). Slower, heavier, but
    works for sites that load content via JavaScript after page load.

Both strategies feed the same parsing logic, so a config only needs to
change `fetch_mode` to switch — item/field selectors don't change.
"""

from __future__ import annotations

import time
import logging
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger("scraper")


@dataclass
class FieldSpec:
    name: str
    selector: str
    attr: str | None = None          # e.g. "href", "src" — None means text content
    multiple: bool = False           # collect a list of values (e.g. tags) instead of one


@dataclass
class ScrapeConfig:
    base_url: str
    item_selector: str
    fields: list[FieldSpec]
    fetch_mode: str = "static"        # "static" or "rendered"
    next_page_selector: str | None = None   # CSS selector for a "next page" link (attr href)
    max_pages: int = 1
    delay_seconds: float = 1.0
    user_agent: str = "Mozilla/5.0 (compatible; PortfolioScraper/1.0)"
    wait_selector: str | None = None  # for rendered mode: CSS selector to wait for before reading DOM


class FetchError(Exception):
    pass


def fetch_static(url: str, user_agent: str, timeout: int = 15) -> str:
    """Fetch a page with a plain HTTP GET. Raises FetchError on failure."""
    try:
        resp = requests.get(url, headers={"User-Agent": user_agent}, timeout=timeout)
        resp.raise_for_status()
        return resp.text
    except requests.RequestException as e:
        raise FetchError(f"Failed to fetch {url}: {e}") from e


def fetch_rendered(url: str, user_agent: str, wait_selector: str | None, timeout_ms: int = 15000) -> str:
    """
    Fetch a JS-rendered page using Playwright (headless Chromium).
    Imported lazily so `static` mode doesn't require Playwright installed.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        raise FetchError(
            "Playwright is required for fetch_mode='rendered'. "
            "Install with: pip install playwright && playwright install chromium"
        ) from e

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(user_agent=user_agent)
            page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
            if wait_selector:
                page.wait_for_selector(wait_selector, timeout=timeout_ms)
            html = page.content()
            browser.close()
            return html
    except Exception as e:
        raise FetchError(f"Failed to render {url}: {e}") from e


def extract_field(item_soup: BeautifulSoup, spec: FieldSpec) -> Any:
    """Extract one field from a single item's soup according to its FieldSpec."""
    elements = item_soup.select(spec.selector)
    if not elements:
        return [] if spec.multiple else None

    def value_of(el):
        if spec.attr:
            return el.get(spec.attr)
        return el.get_text(strip=True)

    if spec.multiple:
        return [value_of(el) for el in elements]
    return value_of(elements[0])


def parse_page(html: str, config: ScrapeConfig, page_url: str) -> tuple[list[dict], str | None]:
    """
    Parse one page's HTML into a list of item dicts, plus the next-page URL (or None).
    Individual items with extraction errors are logged and skipped, not fatal.
    """
    soup = BeautifulSoup(html, "html.parser")
    items = soup.select(config.item_selector)

    results = []
    for idx, item in enumerate(items):
        try:
            row = {}
            for spec in config.fields:
                row[spec.name] = extract_field(item, spec)
            results.append(row)
        except Exception as e:
            logger.warning(f"Skipping item {idx} on {page_url}: {e}")

    next_url = None
    if config.next_page_selector:
        next_el = soup.select_one(config.next_page_selector)
        if next_el and next_el.get("href"):
            next_url = urljoin(page_url, next_el["href"])

    return results, next_url


def scrape(config: ScrapeConfig) -> list[dict]:
    """
    Run the full scrape: fetch → parse → follow pagination, up to max_pages.
    Respects delay_seconds between requests.
    """
    all_results: list[dict] = []
    url = config.base_url
    pages_done = 0

    while url and pages_done < config.max_pages:
        logger.info(f"Fetching page {pages_done + 1}: {url}")

        try:
            if config.fetch_mode == "rendered":
                html = fetch_rendered(url, config.user_agent, config.wait_selector)
            else:
                html = fetch_static(url, config.user_agent)
        except FetchError as e:
            logger.error(str(e))
            break

        page_results, next_url = parse_page(html, config, url)
        logger.info(f"  extracted {len(page_results)} items")
        all_results.extend(page_results)

        pages_done += 1
        url = next_url

        if url and pages_done < config.max_pages:
            time.sleep(config.delay_seconds)

    return all_results
