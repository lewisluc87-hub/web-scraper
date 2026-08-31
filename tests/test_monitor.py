"""
Offline unit tests for scraper.monitor -- diffing, snapshot storage, and
alerting logic. No network access required; webhook delivery is tested
against a real local HTTP server (not mocked), same "verify against the
real thing, not synthetic assumptions" standard as test_parser.py.
"""

import http.server
import json
import shutil
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from scraper.monitor import (
    DiffResult,
    compute_key,
    diff_results,
    format_diff_report,
    load_latest_snapshot,
    save_snapshot,
    send_webhook_alert,
)


# ---------- compute_key ----------

def test_compute_key_uses_key_field_when_present():
    item = {"url": "https://example.com/a", "title": "Item A"}
    assert compute_key(item, key_field="url") == "https://example.com/a"


def test_compute_key_falls_back_to_hash_when_key_field_missing():
    item = {"title": "Item A", "price": "10"}
    key = compute_key(item, key_field="url")  # 'url' not present
    assert key != "url"
    assert len(key) == 16  # truncated sha256


def test_compute_key_hash_is_stable_regardless_of_field_order():
    item_a = {"title": "X", "price": "1"}
    item_b = {"price": "1", "title": "X"}
    assert compute_key(item_a, None) == compute_key(item_b, None)


def test_compute_key_hash_differs_for_different_content():
    item_a = {"title": "X", "price": "1"}
    item_b = {"title": "X", "price": "2"}
    assert compute_key(item_a, None) != compute_key(item_b, None)


# ---------- diff_results ----------

def test_diff_detects_added_items():
    previous = [{"url": "a", "title": "A"}]
    current = [{"url": "a", "title": "A"}, {"url": "b", "title": "B"}]
    diff = diff_results(previous, current, key_field="url")
    assert len(diff.added) == 1
    assert diff.added[0]["url"] == "b"
    assert len(diff.removed) == 0
    assert len(diff.changed) == 0
    assert diff.unchanged_count == 1


def test_diff_detects_removed_items():
    previous = [{"url": "a", "title": "A"}, {"url": "b", "title": "B"}]
    current = [{"url": "a", "title": "A"}]
    diff = diff_results(previous, current, key_field="url")
    assert len(diff.removed) == 1
    assert diff.removed[0]["url"] == "b"


def test_diff_detects_changed_items():
    previous = [{"url": "a", "title": "A", "price": "10"}]
    current = [{"url": "a", "title": "A", "price": "12"}]
    diff = diff_results(previous, current, key_field="url")
    assert len(diff.changed) == 1
    old, new = diff.changed[0]
    assert old["price"] == "10"
    assert new["price"] == "12"


def test_diff_reports_no_changes_for_identical_data():
    items = [{"url": "a", "title": "A"}, {"url": "b", "title": "B"}]
    diff = diff_results(items, items, key_field="url")
    assert not diff.has_changes
    assert diff.unchanged_count == 2


def test_diff_handles_empty_previous_as_all_added():
    current = [{"url": "a"}, {"url": "b"}]
    diff = diff_results([], current, key_field="url")
    assert len(diff.added) == 2
    assert len(diff.removed) == 0


def test_diff_without_key_field_treats_any_change_as_remove_plus_add():
    """Without a stable key field, a changed item has no anchor -- it
    should show up as one removed + one added, not 'changed'. This is a
    documented tradeoff, not a bug: the test locks in that expected
    behavior so it can't silently change later."""
    previous = [{"title": "A", "price": "10"}]
    current = [{"title": "A", "price": "12"}]
    diff = diff_results(previous, current, key_field=None)
    assert len(diff.added) == 1
    assert len(diff.removed) == 1
    assert len(diff.changed) == 0


def test_diff_warns_when_key_field_is_not_unique(caplog_workaround=None):
    """Regression test for a real bug found during manual testing: using
    'author_link' as the key field for the quotes site silently collapsed
    6 Einstein quotes (all sharing one author_link) into a single key,
    dropping 5 of them from the diff without any indication. The fix logs
    a warning whenever items collapse onto fewer keys than there are
    items -- this test simulates that exact shape of collision."""
    import logging

    current = [
        {"author_link": "/author/Einstein", "text": "quote 1"},
        {"author_link": "/author/Einstein", "text": "quote 2"},
        {"author_link": "/author/Einstein", "text": "quote 3"},
        {"author_link": "/author/Rowling", "text": "quote 4"},
    ]
    logger = logging.getLogger("scraper.monitor")
    records = []
    handler = logging.Handler()
    handler.emit = lambda record: records.append(record)
    logger.addHandler(handler)
    try:
        diff_results([], current, key_field="author_link")
    finally:
        logger.removeHandler(handler)

    warnings = [r for r in records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "not unique" in warnings[0].getMessage()
    assert "4 items collapsed to 2 distinct keys" in warnings[0].getMessage()


def test_diff_does_not_warn_when_key_field_is_actually_unique():
    import logging

    current = [{"url": "a"}, {"url": "b"}, {"url": "c"}]
    logger = logging.getLogger("scraper.monitor")
    records = []
    handler = logging.Handler()
    handler.emit = lambda record: records.append(record)
    logger.addHandler(handler)
    try:
        diff_results([], current, key_field="url")
    finally:
        logger.removeHandler(handler)

    warnings = [r for r in records if r.levelno == logging.WARNING]
    assert len(warnings) == 0


# ---------- format_diff_report ----------

def test_format_diff_report_includes_counts():
    diff = DiffResult(added=[{"url": "a"}], removed=[{"url": "b"}], changed=[], unchanged_count=5)
    report = format_diff_report(diff, "my_config", "2026-01-01T00-00-00Z")
    assert "+1 added" in report
    assert "-1 removed" in report
    assert "~0 changed" in report
    assert "5 unchanged" in report
    assert "my_config" in report


# ---------- snapshot storage ----------

def test_snapshot_round_trip():
    tmpdir = Path(tempfile.mkdtemp())
    try:
        results = [{"url": "a", "title": "A"}]
        save_snapshot(tmpdir, "my_config", results, "2026-01-01T00-00-00Z")
        loaded = load_latest_snapshot(tmpdir, "my_config")
        assert loaded == results
    finally:
        shutil.rmtree(tmpdir)


def test_load_latest_snapshot_returns_none_when_no_history():
    tmpdir = Path(tempfile.mkdtemp())
    try:
        result = load_latest_snapshot(tmpdir, "never_seen_config")
        assert result is None
    finally:
        shutil.rmtree(tmpdir)


def test_load_latest_snapshot_picks_the_most_recent_one():
    tmpdir = Path(tempfile.mkdtemp())
    try:
        save_snapshot(tmpdir, "cfg", [{"v": 1}], "2026-01-01T00-00-00Z")
        save_snapshot(tmpdir, "cfg", [{"v": 2}], "2026-01-02T00-00-00Z")
        save_snapshot(tmpdir, "cfg", [{"v": 3}], "2026-01-03T00-00-00Z")
        loaded = load_latest_snapshot(tmpdir, "cfg")
        assert loaded == [{"v": 3}]
    finally:
        shutil.rmtree(tmpdir)


# ---------- webhook alert (real local HTTP server, not mocked) ----------

class _CapturingHandler(http.server.BaseHTTPRequestHandler):
    received_payloads = []

    def do_POST(self):
        length = int(self.headers["Content-Length"])
        body = self.rfile.read(length)
        _CapturingHandler.received_payloads.append(json.loads(body))
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass  # silence default request logging


def test_webhook_alert_delivers_real_http_post():
    _CapturingHandler.received_payloads.clear()
    server = http.server.HTTPServer(("127.0.0.1", 0), _CapturingHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()

    diff = DiffResult(added=[{"url": "a"}], removed=[], changed=[], unchanged_count=0)
    send_webhook_alert(f"http://127.0.0.1:{port}/", diff, "my_config", "2026-01-01T00-00-00Z")
    thread.join(timeout=5)

    assert len(_CapturingHandler.received_payloads) == 1
    payload = _CapturingHandler.received_payloads[0]
    assert payload["config"] == "my_config"
    assert payload["added_count"] == 1


def test_webhook_alert_failure_does_not_raise():
    """A broken/unreachable webhook must not crash the monitor run."""
    diff = DiffResult(added=[{"url": "a"}], removed=[], changed=[], unchanged_count=0)
    # Port 1 is reserved/unlikely to be listening -- should fail fast, and
    # send_webhook_alert should swallow the error rather than raising.
    send_webhook_alert("http://127.0.0.1:1/", diff, "my_config", "2026-01-01T00-00-00Z")


if __name__ == "__main__":
    tests = [
        test_compute_key_uses_key_field_when_present,
        test_compute_key_falls_back_to_hash_when_key_field_missing,
        test_compute_key_hash_is_stable_regardless_of_field_order,
        test_compute_key_hash_differs_for_different_content,
        test_diff_detects_added_items,
        test_diff_detects_removed_items,
        test_diff_detects_changed_items,
        test_diff_reports_no_changes_for_identical_data,
        test_diff_handles_empty_previous_as_all_added,
        test_diff_without_key_field_treats_any_change_as_remove_plus_add,
        test_diff_warns_when_key_field_is_not_unique,
        test_diff_does_not_warn_when_key_field_is_actually_unique,
        test_format_diff_report_includes_counts,
        test_snapshot_round_trip,
        test_load_latest_snapshot_returns_none_when_no_history,
        test_load_latest_snapshot_picks_the_most_recent_one,
        test_webhook_alert_delivers_real_http_post,
        test_webhook_alert_failure_does_not_raise,
    ]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {t.__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)