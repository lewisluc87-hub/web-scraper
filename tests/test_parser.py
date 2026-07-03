"""
Offline unit tests — verify parsing logic against saved HTML fixtures.
No network access required. This is what proves the extraction logic is
correct, independent of whether the live site is reachable at test time.
"""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent.parent))

from scraper.core import ScrapeConfig, FieldSpec, parse_page

FIXTURES = Path(__file__).parent.parent / "fixtures"

QUOTES_CONFIG = ScrapeConfig(
    base_url="https://quotes.toscrape.com/",
    item_selector="div.quote",
    fields=[
        FieldSpec(name="text", selector="span.text"),
        FieldSpec(name="author", selector="small.author"),
        FieldSpec(name="author_link", selector="span a", attr="href"),
        FieldSpec(name="tags", selector="div.tags a.tag", multiple=True),
    ],
    next_page_selector="li.next a",
)


def test_extracts_correct_item_count():
    html = (FIXTURES / "quotes_page1.html").read_text()
    results, _ = parse_page(html, QUOTES_CONFIG, QUOTES_CONFIG.base_url)
    assert len(results) == 3, f"expected 3 quotes, got {len(results)}"


def test_extracts_correct_text_content():
    html = (FIXTURES / "quotes_page1.html").read_text()
    results, _ = parse_page(html, QUOTES_CONFIG, QUOTES_CONFIG.base_url)
    assert results[1]["author"] == "J.K. Rowling"
    assert "choices" in results[1]["tags"]
    assert results[0]["author_link"] == "/author/Albert-Einstein"


def test_extracts_multiple_tags_as_list():
    html = (FIXTURES / "quotes_page1.html").read_text()
    results, _ = parse_page(html, QUOTES_CONFIG, QUOTES_CONFIG.base_url)
    assert results[0]["tags"] == ["change", "deep-thoughts", "thinking", "world"]


def test_finds_next_page_url_and_resolves_relative_link():
    html = (FIXTURES / "quotes_page1.html").read_text()
    _, next_url = parse_page(html, QUOTES_CONFIG, QUOTES_CONFIG.base_url)
    assert next_url == "https://quotes.toscrape.com/page/2/"


def test_missing_fields_do_not_crash_and_return_none_or_empty():
    html = (FIXTURES / "quotes_malformed.html").read_text()
    results, _ = parse_page(html, QUOTES_CONFIG, QUOTES_CONFIG.base_url)

    # all 3 items should still be returned — missing fields become None / [] , not a crash
    assert len(results) == 3

    normal, no_author, orphan = results
    assert normal["author"] == "Normal Author"

    assert no_author["author"] is None
    assert no_author["tags"] == []

    assert orphan["text"] is None
    assert orphan["tags"] == ["orphan"]


def test_empty_page_returns_empty_list_not_error():
    html = "<html><body><p>no items here</p></body></html>"
    results, next_url = parse_page(html, QUOTES_CONFIG, QUOTES_CONFIG.base_url)
    assert results == []
    assert next_url is None


if __name__ == "__main__":
    tests = [
        test_extracts_correct_item_count,
        test_extracts_correct_text_content,
        test_extracts_multiple_tags_as_list,
        test_finds_next_page_url_and_resolves_relative_link,
        test_missing_fields_do_not_crash_and_return_none_or_empty,
        test_empty_page_returns_empty_list_not_error,
    ]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL  {t.__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
