"""
Integration tests for mcp_server.py -- drive the ACTUAL server as a
subprocess over real stdio, using the official mcp SDK's client. Not a
mock of the protocol, and not a mock HTTP layer either: a real local
HTTP server serves the same saved fixture HTML already used by
test_parser.py, so this proves a real HTTP request -> real parse -> real
MCP tool response chain, without depending on outbound internet access
in CI (this repo's own tests already avoid that -- see test_parser.py).

No network access required beyond localhost. Matches this repo's
existing test style (plain assert functions + a manual runner at the
bottom) rather than introducing pytest, since the rest of this repo
doesn't use it.
"""

from __future__ import annotations

import asyncio
import functools
import http.server
import json
import socketserver
import sys
import tempfile
import threading
from contextlib import asynccontextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from mcp import ClientSession, StdioServerParameters, stdio_client

ROOT = Path(__file__).parent.parent
FIXTURES = ROOT / "fixtures"
SERVER_PATH = ROOT / "mcp_server.py"


class _LocalFixtureServer:
    """Serves fixtures/ on a random localhost port, so scrape() can be
    tested against a real HTTP request without touching the live
    internet or a mock HTTP layer."""

    def __enter__(self):
        handler = functools.partial(
            http.server.SimpleHTTPRequestHandler, directory=str(FIXTURES)
        )
        self.httpd = socketserver.TCPServer(("127.0.0.1", 0), handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}/{path}"


def _write_local_config(configs_dir: Path, base_url: str) -> None:
    config = {
        "base_url": base_url,
        "item_selector": "div.quote",
        "fields": [
            {"name": "text", "selector": "span.text"},
            {"name": "author", "selector": "small.author"},
            {"name": "tags", "selector": "div.tags a.tag", "multiple": True},
        ],
        "fetch_mode": "static",
        "max_pages": 1,
    }
    (configs_dir / "local_quotes.json").write_text(json.dumps(config))


@asynccontextmanager
async def open_session(env: dict[str, str]):
    """Start the real MCP server as a subprocess (env overrides
    CONFIGS_DIR/SNAPSHOT_DIR to an isolated temp dir where given) and
    open a real session against it over real stdio."""
    params = StdioServerParameters(command=sys.executable, args=[str(SERVER_PATH)], env=env)
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        yield session


def test_lists_all_three_tools():
    async def run():
        with tempfile.TemporaryDirectory() as configs_dir:
            async with open_session({"SCRAPER_MCP_CONFIGS_DIR": configs_dir}) as session:
                tools = await session.list_tools()
                names = {t.name for t in tools.tools}
                assert names == {"list_configs", "scrape", "check_for_changes"}

    asyncio.run(run())


def test_list_configs_reports_real_repo_configs():
    async def run():
        # No env override here -- confirms the tool sees this repo's
        # actual committed configs, not just an isolated test fixture.
        async with open_session({}) as session:
            res = await session.call_tool("list_configs", {})
            text = res.content[0].text
            assert "quotes_static" in text
            assert "quotes_rendered" in text
            assert "ecommerce_template" in text

    asyncio.run(run())


def test_scrape_against_real_local_http_server():
    async def run():
        with _LocalFixtureServer() as srv, tempfile.TemporaryDirectory() as configs_dir:
            _write_local_config(Path(configs_dir), srv.url("quotes_page1.html"))
            async with open_session({"SCRAPER_MCP_CONFIGS_DIR": configs_dir}) as session:
                res = await session.call_tool("scrape", {"config_name": "local_quotes"})
                data = json.loads(res.content[0].text)
                assert data["item_count"] == 3
                assert data["items"][0]["author"] == "Albert Einstein"
                assert data["items"][1]["tags"] == ["abilities", "choices"]

    asyncio.run(run())


def test_scrape_rejects_unknown_config():
    async def run():
        with tempfile.TemporaryDirectory() as configs_dir:
            async with open_session({"SCRAPER_MCP_CONFIGS_DIR": configs_dir}) as session:
                res = await session.call_tool("scrape", {"config_name": "does_not_exist"})
                # MCP tool errors surface as an error-flagged result to the
                # client, not a raised Python exception -- check that flag.
                assert res.is_error

    asyncio.run(run())


def test_scrape_rejects_path_traversal_config_name():
    async def run():
        with tempfile.TemporaryDirectory() as configs_dir:
            async with open_session({"SCRAPER_MCP_CONFIGS_DIR": configs_dir}) as session:
                res = await session.call_tool(
                    "scrape", {"config_name": "../../../etc/passwd"}
                )
                assert res.is_error

    asyncio.run(run())


def test_check_for_changes_with_no_prior_snapshot():
    async def run():
        with (
            _LocalFixtureServer() as srv,
            tempfile.TemporaryDirectory() as configs_dir,
            tempfile.TemporaryDirectory() as snap_dir,
        ):
            _write_local_config(Path(configs_dir), srv.url("quotes_page1.html"))
            env = {"SCRAPER_MCP_CONFIGS_DIR": configs_dir, "SCRAPER_MCP_SNAPSHOT_DIR": snap_dir}
            async with open_session(env) as session:
                res = await session.call_tool(
                    "check_for_changes", {"config_name": "local_quotes"}
                )
                assert "No previous snapshot" in res.content[0].text

    asyncio.run(run())


def test_check_for_changes_reports_real_diff_and_does_not_persist_a_snapshot():
    async def run():
        with (
            _LocalFixtureServer() as srv,
            tempfile.TemporaryDirectory() as configs_dir,
            tempfile.TemporaryDirectory() as snap_dir_str,
        ):
            snap_dir = Path(snap_dir_str)
            _write_local_config(Path(configs_dir), srv.url("quotes_page1.html"))

            # Pre-seed a "previous" snapshot missing one real quote, so a
            # genuine +1 added diff is expected against the live fixture.
            fake_previous = [
                {
                    "text": "\u201cThe world as we have created it is a process of our "
                    "thinking. It cannot be changed without changing our thinking.\u201d",
                    "author": "Albert Einstein",
                    "tags": ["change", "deep-thoughts", "thinking", "world"],
                },
                {
                    "text": "\u201cIt is our choices, Harry, that show what we truly are, "
                    "far more than our abilities.\u201d",
                    "author": "J.K. Rowling",
                    "tags": ["abilities", "choices"],
                },
            ]
            snap_folder = snap_dir / "local_quotes"
            snap_folder.mkdir(parents=True)
            (snap_folder / "2020-01-01T00-00-00Z.json").write_text(json.dumps(fake_previous))

            env = {
                "SCRAPER_MCP_CONFIGS_DIR": configs_dir,
                "SCRAPER_MCP_SNAPSHOT_DIR": str(snap_dir),
            }
            async with open_session(env) as session:
                res = await session.call_tool(
                    "check_for_changes", {"config_name": "local_quotes"}
                )
                text = res.content[0].text
                assert "+1 added" in text
                assert "Steve Martin" in text
                assert "2 unchanged" in text

            # The whole point of check_for_changes being read-only: confirm
            # no new snapshot file was written by the call above.
            remaining = list(snap_folder.glob("*.json"))
            assert len(remaining) == 1, (
                f"check_for_changes must not persist a new snapshot -- "
                f"found {len(remaining)} files"
            )

    asyncio.run(run())


if __name__ == "__main__":
    tests = [
        test_lists_all_three_tools,
        test_list_configs_reports_real_repo_configs,
        test_scrape_against_real_local_http_server,
        test_scrape_rejects_unknown_config,
        test_scrape_rejects_path_traversal_config_name,
        test_check_for_changes_with_no_prior_snapshot,
        test_check_for_changes_reports_real_diff_and_does_not_persist_a_snapshot,
    ]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL  {t.__name__}: {e}")
        except Exception as e:
            failed += 1
            print(f"ERROR {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
