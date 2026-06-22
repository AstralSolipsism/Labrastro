"""Tests for the fetch_capabilities builtin source reader."""

from __future__ import annotations

import io
import json
import socket
from http.client import RemoteDisconnected
from urllib.error import HTTPError, URLError

from reuleauxcoder.extensions.tools.builtin import fetch_capabilities as fetch_module
from reuleauxcoder.extensions.tools.builtin.fetch_capabilities import (
    FetchCapabilitiesTool,
)
from reuleauxcoder.extensions.tools.registry import build_tools


PUBLIC_ADDRINFO = [
    (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
]


class FakeResponse:
    def __init__(
        self,
        url: str,
        body: str | bytes,
        *,
        content_type: str = "text/html; charset=utf-8",
        status: int = 200,
    ) -> None:
        self.url = url
        self.status = status
        self.headers = {"Content-Type": content_type}
        self._body = body.encode("utf-8") if isinstance(body, str) else body

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_args: object) -> bool:
        return False

    def getcode(self) -> int:
        return self.status

    def geturl(self) -> str:
        return self.url

    def read(self, _limit: int = -1) -> bytes:
        return self._body


class FakeOpener:
    def __init__(
        self,
        routes: dict[
            str,
            FakeResponse
            | HTTPError
            | Exception
            | list[FakeResponse | HTTPError | Exception],
        ],
    ) -> None:
        self.routes = routes
        self.calls: list[str] = []
        self.timeouts: list[int | None] = []

    def open(self, req: object, timeout: int | None = None) -> FakeResponse:
        url = req.get_full_url()  # type: ignore[attr-defined]
        self.calls.append(url)
        self.timeouts.append(timeout)
        response = self.routes.get(url)
        if isinstance(response, list):
            if response:
                next_response = response.pop(0)
                if not response:
                    self.routes[url] = next_response
                response = next_response
            else:
                response = None
        if isinstance(response, Exception):
            raise response
        if isinstance(response, HTTPError):
            raise response
        if response is not None:
            return response
        raise HTTPError(
            url,
            404,
            "Not Found",
            {"Content-Type": "text/plain"},
            io.BytesIO(b"missing"),
        )


def _install_fake_network(
    monkeypatch,
    routes: dict[
        str,
        FakeResponse
        | HTTPError
        | Exception
        | list[FakeResponse | HTTPError | Exception],
    ],
) -> FakeOpener:
    opener = FakeOpener(routes)
    monkeypatch.setattr(fetch_module.socket, "getaddrinfo", lambda *_args: PUBLIC_ADDRINFO)
    monkeypatch.setattr(fetch_module.urllib_request, "build_opener", lambda *_args: opener)
    return opener


def test_registry_exposes_fetch_capabilities() -> None:
    assert "fetch_capabilities" in {tool.name for tool in build_tools()}


def test_fetch_html_sections_links_and_evidence(monkeypatch) -> None:
    url = "https://docs.example.com/tool"
    html = """
    <html>
      <head><title>Example Tool Docs</title></head>
      <body>
        <h1>Example Tool</h1>
        <p>Example Tool is a server-side CLI for automation.</p>
        <h2 id="install">Install</h2>
        <p>Install with npm before using it.</p>
        <pre><code>npm install -g example-tool</code></pre>
        <a href="https://github.com/acme/example-tool">Source</a>
      </body>
    </html>
    """
    _install_fake_network(monkeypatch, {url: FakeResponse(url, html)})

    payload = json.loads(FetchCapabilitiesTool().execute(url=url, focus="install"))

    assert payload["ok"] is True
    assert payload["source_kind"] == "docs_site"
    assert payload["title"] == "Example Tool Docs"
    assert payload["sections"][0]["heading"] == "Install"
    assert payload["sections"][0]["code_blocks"] == ["npm install -g example-tool"]
    assert payload["links"] == [
        {
            "title": "Source",
            "url": "https://github.com/acme/example-tool",
            "kind": "github_repo",
        }
    ]
    assert payload["evidence"][0]["source_url"].endswith("#install")
    assert payload["evidence"][0]["content_hash"]
    assert payload["evidence"][0]["fetched_at"]


def test_fetch_markdown_focuses_relevant_section(monkeypatch) -> None:
    url = "https://docs.example.com/guide.md"
    markdown = """
# Example Tool

## Overview
General notes.

## Windows install
Run this command.

```powershell
winget install Example.Tool
```
"""
    _install_fake_network(
        monkeypatch,
        {url: FakeResponse(url, markdown, content_type="text/markdown; charset=utf-8")},
    )

    payload = json.loads(FetchCapabilitiesTool().execute(url=url, focus="windows install"))

    assert payload["ok"] is True
    assert payload["source_kind"] == "markdown"
    assert payload["sections"][0]["heading"] == "Windows install"
    assert payload["sections"][0]["code_blocks"] == ["winget install Example.Tool"]


def test_fetch_github_repo_reads_readme_and_manifest(monkeypatch) -> None:
    repo_url = "https://github.com/acme/example-tool"
    readme_url = "https://raw.githubusercontent.com/acme/example-tool/HEAD/README.md"
    package_url = "https://raw.githubusercontent.com/acme/example-tool/HEAD/package.json"
    readme = """
# Example Tool

## Installation
Install globally.

```bash
npm install -g example-tool
```
"""
    package = '{"name":"example-tool","bin":{"example-tool":"bin/cli.js"}}'
    opener = _install_fake_network(
        monkeypatch,
        {
            readme_url: FakeResponse(
                readme_url,
                readme,
                content_type="text/markdown; charset=utf-8",
            ),
            package_url: FakeResponse(
                package_url,
                package,
                content_type="application/json",
            ),
        },
    )

    payload = json.loads(FetchCapabilitiesTool().execute(url=repo_url, focus="install"))

    assert payload["ok"] is True
    assert payload["source_kind"] == "github_repo"
    assert payload["title"] == "acme/example-tool"
    assert {"title": "README.md", "url": readme_url} in payload["docs"]
    assert {"title": "package.json", "url": package_url} in payload["docs"]
    assert any(section["heading"] == "Installation" for section in payload["sections"])
    assert readme_url in opener.calls
    assert package_url in opener.calls


def test_rejects_private_addresses() -> None:
    payload = json.loads(FetchCapabilitiesTool().execute(url="http://127.0.0.1/docs"))

    assert payload["ok"] is False
    assert payload["errors"][0]["code"] == "private_address"


def test_reports_unsupported_non_text_content(monkeypatch) -> None:
    url = "https://docs.example.com/logo.png"
    _install_fake_network(
        monkeypatch,
        {url: FakeResponse(url, b"\x89PNG\r\n", content_type="image/png")},
    )

    payload = json.loads(FetchCapabilitiesTool().execute(url=url))

    assert payload["ok"] is False
    assert payload["errors"][0]["code"] == "unsupported_content_type"


def test_reports_pdf_as_unsupported(monkeypatch) -> None:
    url = "https://docs.example.com/manual.pdf"
    _install_fake_network(
        monkeypatch,
        {url: FakeResponse(url, b"%PDF-1.7", content_type="application/pdf")},
    )

    payload = json.loads(FetchCapabilitiesTool().execute(url=url))

    assert payload["ok"] is False
    assert payload["errors"][0]["code"] == "unsupported_pdf"


def test_reports_browser_required_page(monkeypatch) -> None:
    url = "https://docs.example.com/app"
    html = "<html><body><script>window.__APP__ = true</script></body></html>"
    _install_fake_network(monkeypatch, {url: FakeResponse(url, html)})

    payload = json.loads(FetchCapabilitiesTool().execute(url=url))

    assert payload["ok"] is False
    assert payload["errors"][0]["code"] == "needs_browser"


def test_reports_http_error(monkeypatch) -> None:
    url = "https://docs.example.com/missing"
    error = HTTPError(
        url,
        404,
        "Not Found",
        {"Content-Type": "text/plain"},
        io.BytesIO(b"missing"),
    )
    _install_fake_network(monkeypatch, {url: error})

    payload = json.loads(FetchCapabilitiesTool().execute(url=url))

    assert payload["ok"] is False
    assert payload["errors"][0]["code"] == "http_error"


def test_reports_content_too_large(monkeypatch) -> None:
    url = "https://docs.example.com/huge.md"
    body = b"# Huge\n" + (b"x" * fetch_module.MAX_DOWNLOAD_BYTES)
    _install_fake_network(
        monkeypatch,
        {url: FakeResponse(url, body, content_type="text/markdown")},
    )

    payload = json.loads(FetchCapabilitiesTool().execute(url=url))

    assert payload["ok"] is False
    assert payload["errors"][0]["code"] == "content_too_large"


def test_reports_network_error(monkeypatch) -> None:
    url = "https://docs.example.com/timeout"
    monkeypatch.setattr(fetch_module, "REQUEST_RETRY_BACKOFF_SEC", 0)
    _install_fake_network(monkeypatch, {url: fetch_module.urllib_error.URLError("timed out")})

    payload = json.loads(FetchCapabilitiesTool().execute(url=url))

    assert payload["ok"] is False
    assert payload["errors"][0]["code"] == "network_error"


def test_retries_timeout_before_reporting_network_error(monkeypatch) -> None:
    url = "https://docs.example.com/retry-timeout"
    monkeypatch.setattr(fetch_module, "REQUEST_RETRY_BACKOFF_SEC", 0)
    opener = _install_fake_network(monkeypatch, {url: URLError("timed out")})

    payload = json.loads(FetchCapabilitiesTool().execute(url=url))

    assert payload["ok"] is False
    assert payload["errors"][0]["code"] == "network_error"
    assert opener.calls == [url, url, url]
    assert opener.timeouts == [fetch_module.REQUEST_TIMEOUT_SEC] * 3


def test_retries_timeout_and_returns_success(monkeypatch) -> None:
    url = "https://docs.example.com/retry-success.md"
    monkeypatch.setattr(fetch_module, "REQUEST_RETRY_BACKOFF_SEC", 0)
    opener = _install_fake_network(
        monkeypatch,
        {
            url: [
                URLError("timed out"),
                FakeResponse(
                    url,
                    "# Retry Success\n\n## Install\nRun it.",
                    content_type="text/markdown",
                ),
            ]
        },
    )

    payload = json.loads(FetchCapabilitiesTool().execute(url=url))

    assert payload["ok"] is True
    assert payload["title"] == "Install"
    assert opener.calls == [url, url]
    assert opener.timeouts == [fetch_module.REQUEST_TIMEOUT_SEC] * 2


def test_retries_remote_disconnected_and_returns_success(monkeypatch) -> None:
    url = "https://docs.example.com/retry-remote-disconnected.md"
    monkeypatch.setattr(fetch_module, "REQUEST_RETRY_BACKOFF_SEC", 0)
    opener = _install_fake_network(
        monkeypatch,
        {
            url: [
                RemoteDisconnected("Remote end closed connection without response"),
                RemoteDisconnected("Remote end closed connection without response"),
                FakeResponse(
                    url,
                    "# Retry Success\n\n## Install\nRun it.",
                    content_type="text/markdown",
                ),
            ]
        },
    )

    payload = json.loads(FetchCapabilitiesTool().execute(url=url))

    assert payload["ok"] is True
    assert payload["title"] == "Install"
    assert opener.calls == [url, url, url]
    assert opener.timeouts == [fetch_module.REQUEST_TIMEOUT_SEC] * 3


def test_retries_remote_disconnected_before_reporting_network_error(monkeypatch) -> None:
    url = "https://docs.example.com/retry-remote-disconnected-fail"
    monkeypatch.setattr(fetch_module, "REQUEST_RETRY_BACKOFF_SEC", 0)
    opener = _install_fake_network(
        monkeypatch,
        {url: RemoteDisconnected("Remote end closed connection without response")},
    )

    payload = json.loads(FetchCapabilitiesTool().execute(url=url))

    assert payload["ok"] is False
    assert payload["errors"][0]["code"] == "network_error"
    assert payload["errors"][0]["attempts"] == 3
    assert payload["errors"][0]["retryable"] is True
    assert payload["errors"][0]["url"] == url
    assert opener.calls == [url, url, url]
