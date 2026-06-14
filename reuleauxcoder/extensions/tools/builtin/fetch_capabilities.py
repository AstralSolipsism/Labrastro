"""Read-only capability source fetching for documentation-backed manifests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from http import client as http_client
import hashlib
import ipaddress
import json
import re
import socket
import time
from typing import Any
from urllib import error as urllib_error
from urllib import request as urllib_request
from urllib.parse import urljoin, urlparse, urlunparse

from reuleauxcoder.extensions.tools.backend import LocalToolBackend, ToolBackend
from reuleauxcoder.extensions.tools.base import Tool, backend_handler
from reuleauxcoder.extensions.tools.registry import register_tool
from reuleauxcoder.extensions.tools.spec import ToolOutputStrategy, ToolRisk


DEFAULT_MAX_CHARS = 50_000
MAX_OUTPUT_CHARS = 100_000
MIN_OUTPUT_CHARS = 4_000
MAX_DOWNLOAD_BYTES = 2_000_000
REQUEST_TIMEOUT_SEC = 30
REQUEST_MAX_ATTEMPTS = 3
REQUEST_RETRY_BACKOFF_SEC = 0.5
MAX_REDIRECTS = 5
REDIRECT_CODES = {301, 302, 303, 307, 308}
TEXT_EXTENSIONS = {
    ".md",
    ".mdx",
    ".txt",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".ini",
    ".cfg",
    ".conf",
    ".rst",
}
GITHUB_REPO_FILES = [
    "README.md",
    "readme.md",
    "docs/README.md",
    "package.json",
    "pyproject.toml",
    "Cargo.toml",
    "deno.json",
    "mcp.json",
]
GITHUB_REFS = ["HEAD", "main", "master"]


@register_tool
class FetchCapabilitiesTool(Tool):
    name = "fetch_capabilities"
    risk = ToolRisk.CAPABILITY
    output_strategy = ToolOutputStrategy.JSON
    permission_policy = "capability"
    description = (
        "Fetch read-only capability documentation from a URL and return structured "
        "sections, links, code blocks, and source evidence. Use this before creating "
        "or updating capability manifest candidates. This tool does not infer install "
        "steps; it only reads and cites source material."
    )
    parameters = {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "HTTP(S) documentation, raw file, or repository URL to read.",
            },
            "focus": {
                "type": "string",
                "description": "Optional topic to prioritize, such as install, setup, MCP config, or Windows.",
            },
            "source_hint": {
                "type": "string",
                "description": "Optional source hint: auto, docs_site, github_repo, github_file, markdown, raw_file.",
            },
            "max_chars": {
                "type": "integer",
                "description": "Approximate max characters of source text to return. Default 50000.",
            },
        },
        "required": ["url"],
    }

    def __init__(self, backend: ToolBackend | None = None):
        super().__init__(backend or LocalToolBackend())

    def execute(
        self,
        url: str,
        focus: str = "",
        source_hint: str = "auto",
        max_chars: int = DEFAULT_MAX_CHARS,
    ) -> str:
        return self.run_backend(
            url=url,
            focus=focus,
            source_hint=source_hint,
            max_chars=max_chars,
        )

    @backend_handler("local")
    def _execute_local(
        self,
        url: str,
        focus: str = "",
        source_hint: str = "auto",
        max_chars: int = DEFAULT_MAX_CHARS,
    ) -> str:
        fetched_at = _utc_now()
        if not isinstance(url, str) or not url.strip():
            return _json_result(
                ok=False,
                url=str(url or ""),
                fetched_at=fetched_at,
                errors=[_error("invalid_url", "url must be a non-empty string")],
            )

        requested_url = url.strip()
        effective_max_chars = _normalize_max_chars(max_chars)
        if effective_max_chars is None:
            return _json_result(
                ok=False,
                url=requested_url,
                fetched_at=fetched_at,
                errors=[_error("invalid_max_chars", "max_chars must be a positive integer")],
            )

        validation_error = _validate_public_http_url(requested_url)
        if validation_error:
            return _json_result(
                ok=False,
                url=requested_url,
                fetched_at=fetched_at,
                errors=[validation_error],
            )

        try:
            github_target = _github_target(requested_url)
            if github_target and github_target["kind"] == "repo":
                result = _fetch_github_repo(
                    requested_url,
                    github_target["owner"],
                    github_target["repo"],
                    focus=str(focus or ""),
                    max_chars=effective_max_chars,
                    fetched_at=fetched_at,
                )
            elif github_target and github_target["kind"] == "blob":
                raw_url = github_target["raw_url"]
                response = _fetch_http(raw_url)
                result = _document_payload(
                    requested_url=requested_url,
                    final_url=response.final_url or raw_url,
                    source_url=response.final_url or raw_url,
                    body=response.body,
                    content_type=response.content_type,
                    status=response.status,
                    source_kind="github_file",
                    focus=str(focus or ""),
                    max_chars=effective_max_chars,
                    fetched_at=fetched_at,
                    title_override=github_target.get("path") or "",
                    fetch_error=response.error,
                )
            else:
                response = _fetch_http(requested_url)
                source_kind = _source_kind(
                    response.final_url or requested_url,
                    response.content_type,
                    str(source_hint or ""),
                )
                result = _document_payload(
                    requested_url=requested_url,
                    final_url=response.final_url or requested_url,
                    source_url=response.final_url or requested_url,
                    body=response.body,
                    content_type=response.content_type,
                    status=response.status,
                    source_kind=source_kind,
                    focus=str(focus or ""),
                    max_chars=effective_max_chars,
                    fetched_at=fetched_at,
                    fetch_error=response.error,
                )
        except Exception as exc:
            result = {
                "ok": False,
                "url": requested_url,
                "final_url": "",
                "source_kind": "unknown",
                "title": "",
                "sections": [],
                "links": [],
                "evidence": [],
                "content_hash": "",
                "fetched_at": fetched_at,
                "errors": [_error("fetch_failed", str(exc))],
            }

        return json.dumps(result, ensure_ascii=False, sort_keys=True)


@dataclass(slots=True)
class _FetchResponse:
    final_url: str
    status: int
    content_type: str
    body: bytes
    error: dict[str, Any] | None = None


class _NoRedirectHandler(urllib_request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


class _HTMLTextExtractor(HTMLParser):
    def __init__(self, base_url: str):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.title = ""
        self.links: list[dict[str, str]] = []
        self.parts: list[str] = []
        self._skip_depth = 0
        self._title_depth = 0
        self._heading_tag: str | None = None
        self._heading_parts: list[str] = []
        self._pre_depth = 0
        self._pre_parts: list[str] = []
        self._link_stack: list[dict[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = {name.lower(): value or "" for name, value in attrs}
        lower = tag.lower()
        if lower in {"script", "style", "noscript", "svg"}:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if lower == "title":
            self._title_depth += 1
            return
        if lower in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self._heading_tag = lower
            self._heading_parts = []
            return
        if lower == "a":
            href = attrs_dict.get("href", "").strip()
            if href:
                self._link_stack.append(
                    {
                        "url": urljoin(self.base_url, href),
                        "title": attrs_dict.get("title") or attrs_dict.get("aria-label") or "",
                    }
                )
            return
        if lower == "pre":
            self._pre_depth += 1
            self._pre_parts = []
            return
        if lower in {"p", "div", "section", "article", "li", "tr", "br"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        lower = tag.lower()
        if lower in {"script", "style", "noscript", "svg"} and self._skip_depth:
            self._skip_depth -= 1
            return
        if self._skip_depth:
            return
        if lower == "title" and self._title_depth:
            self._title_depth -= 1
            return
        if lower == self._heading_tag:
            level = int(lower[1])
            heading = _compact_text(" ".join(self._heading_parts))
            if heading:
                self.parts.append(f"\n\n{'#' * level} {heading}\n\n")
            self._heading_tag = None
            self._heading_parts = []
            return
        if lower == "a" and self._link_stack:
            link = self._link_stack.pop()
            if _is_http_url(link["url"]):
                self.links.append(link)
            return
        if lower == "pre" and self._pre_depth:
            self._pre_depth -= 1
            code = "".join(self._pre_parts).strip("\n")
            if code:
                self.parts.append(f"\n```text\n{code}\n```\n")
            self._pre_parts = []
            return
        if lower in {"p", "div", "section", "article", "li", "tr"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._title_depth:
            self.title += data
            return
        if self._heading_tag:
            self._heading_parts.append(data)
            return
        if self._pre_depth:
            self._pre_parts.append(data)
            return
        text = _compact_text(data)
        if text:
            self.parts.append(text + " ")
            if self._link_stack:
                current = self._link_stack[-1]
                if not current.get("title"):
                    current["title"] = text

    def markdown_text(self) -> str:
        text = "".join(self.parts)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()


def _fetch_http(url: str) -> _FetchResponse:
    opener = urllib_request.build_opener(_NoRedirectHandler)
    current_url = url
    for redirect_count in range(MAX_REDIRECTS + 1):
        validation_error = _validate_public_http_url(current_url)
        if validation_error:
            return _FetchResponse(
                final_url=current_url,
                status=0,
                content_type="",
                body=b"",
                error=validation_error,
            )
        req = urllib_request.Request(
            current_url,
            headers={
                "User-Agent": "Labrastro fetch_capabilities/1.0",
                "Accept": "text/html,text/markdown,text/plain,application/json,application/yaml,*/*;q=0.1",
            },
        )
        redirect_to = ""
        last_network_error: _FetchResponse | None = None
        for attempt in range(1, REQUEST_MAX_ATTEMPTS + 1):
            try:
                with opener.open(req, timeout=REQUEST_TIMEOUT_SEC) as response:
                    status = int(getattr(response, "status", response.getcode() or 0))
                    final_url = str(response.geturl() or current_url)
                    content_type = str(response.headers.get("Content-Type", ""))
                    body = response.read(MAX_DOWNLOAD_BYTES + 1)
                    if len(body) > MAX_DOWNLOAD_BYTES:
                        return _FetchResponse(
                            final_url=final_url,
                            status=status,
                            content_type=content_type,
                            body=body[:MAX_DOWNLOAD_BYTES],
                            error=_error(
                                "content_too_large",
                                f"response exceeded {MAX_DOWNLOAD_BYTES} bytes",
                            ),
                        )
                    return _FetchResponse(
                        final_url=final_url,
                        status=status,
                        content_type=content_type,
                        body=body,
                    )
            except urllib_error.HTTPError as exc:
                location = exc.headers.get("Location", "")
                if exc.code in REDIRECT_CODES and location:
                    if redirect_count >= MAX_REDIRECTS:
                        return _FetchResponse(
                            final_url=current_url,
                            status=exc.code,
                            content_type=str(exc.headers.get("Content-Type", "")),
                            body=b"",
                            error=_error(
                                "too_many_redirects",
                                "maximum redirect count exceeded",
                            ),
                        )
                    redirect_to = urljoin(current_url, location)
                    break
                body = exc.read(MAX_DOWNLOAD_BYTES + 1)
                return _FetchResponse(
                    final_url=current_url,
                    status=exc.code,
                    content_type=str(exc.headers.get("Content-Type", "")),
                    body=body[:MAX_DOWNLOAD_BYTES],
                    error=_error("http_error", f"HTTP {exc.code}"),
                )
            except (
                urllib_error.URLError,
                TimeoutError,
                socket.timeout,
                http_client.RemoteDisconnected,
                ConnectionResetError,
                ConnectionAbortedError,
            ) as exc:
                retryable = _is_retryable_network_error(exc)
                last_network_error = _FetchResponse(
                    final_url=current_url,
                    status=0,
                    content_type="",
                    body=b"",
                    error=_error(
                        "network_error",
                        _network_error_message(exc),
                        url=current_url,
                        attempts=attempt,
                        retryable=retryable,
                    ),
                )
                if attempt < REQUEST_MAX_ATTEMPTS and retryable:
                    time.sleep(_retry_backoff_delay(attempt))
                    continue
                return last_network_error
        if redirect_to:
            current_url = redirect_to
            continue
        if last_network_error is not None:
            return last_network_error
    return _FetchResponse(
        final_url=current_url,
        status=0,
        content_type="",
        body=b"",
        error=_error("too_many_redirects", "maximum redirect count exceeded"),
    )


def _network_error_message(exc: BaseException) -> str:
    if isinstance(exc, urllib_error.URLError):
        return str(exc.reason)
    return str(exc)


def _is_retryable_network_error(exc: BaseException) -> bool:
    if isinstance(
        exc,
        (
            TimeoutError,
            socket.timeout,
            http_client.RemoteDisconnected,
            ConnectionResetError,
            ConnectionAbortedError,
        ),
    ):
        return True
    if isinstance(exc, urllib_error.URLError):
        reason = exc.reason
        if isinstance(
            reason,
            (
                TimeoutError,
                socket.timeout,
                http_client.RemoteDisconnected,
                OSError,
            ),
        ):
            return True
        message = str(reason).lower()
        return any(
            marker in message
            for marker in (
                "remote end closed",
                "closed connection",
                "timed out",
                "timeout",
                "temporarily",
                "temporary",
                "connection reset",
                "connection aborted",
                "network is unreachable",
            )
        )
    return False


def _retry_backoff_delay(attempt: int) -> float:
    return min(3.0, REQUEST_RETRY_BACKOFF_SEC * (3 ** max(0, attempt - 1)))


def _fetch_github_repo(
    requested_url: str,
    owner: str,
    repo: str,
    *,
    focus: str,
    max_chars: int,
    fetched_at: str,
) -> dict[str, Any]:
    all_sections: list[dict[str, Any]] = []
    all_links: list[dict[str, str]] = []
    docs: list[dict[str, str]] = []
    errors: list[dict[str, Any]] = []
    content_parts: list[str] = []

    for path in GITHUB_REPO_FILES:
        response: _FetchResponse | None = None
        raw_url = ""
        for ref in GITHUB_REFS:
            raw_url = f"https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{path}"
            candidate = _fetch_http(raw_url)
            if candidate.status == 200 and not candidate.error:
                response = candidate
                break
            if candidate.error and candidate.status != 404:
                error = dict(candidate.error)
                error["message"] = f"{path}: {error.get('message', '')}"
                error.setdefault("url", raw_url)
                errors.append(error)
                break
        if response is None:
            continue
        payload = _document_payload(
            requested_url=raw_url,
            final_url=response.final_url or raw_url,
            source_url=response.final_url or raw_url,
            body=response.body,
            content_type=response.content_type,
            status=response.status,
            source_kind="github_file",
            focus=focus,
            max_chars=max_chars,
            fetched_at=fetched_at,
            title_override=path,
        )
        if payload.get("ok"):
            docs.append({"title": path, "url": response.final_url or raw_url})
            all_sections.extend(payload.get("sections") or [])
            all_links.extend(payload.get("links") or [])
            content_parts.append(_decode_body(response.body, response.content_type))

    content_hash = _hash_text("\n\n".join(content_parts))
    selected = _select_sections(
        all_sections,
        focus=focus,
        fragment=urlparse(requested_url).fragment,
        max_chars=max_chars,
    )
    evidence = _evidence_from_sections(selected, content_hash, fetched_at)
    if not selected:
        return {
            "ok": False,
            "url": requested_url,
            "final_url": requested_url,
            "source_kind": "github_repo",
            "title": f"{owner}/{repo}",
            "sections": [],
            "links": _dedupe_links(all_links),
            "evidence": [],
            "content_hash": content_hash,
            "fetched_at": fetched_at,
            "errors": errors
            or [
                _error(
                    "github_repo_unreadable",
                    "could not read README or known manifest files from the repository",
                )
            ],
        }
    return {
        "ok": True,
        "url": requested_url,
        "final_url": requested_url,
        "source_kind": "github_repo",
        "title": f"{owner}/{repo}",
        "sections": selected,
        "links": _dedupe_links(all_links),
        "docs": docs,
        "evidence": evidence,
        "content_hash": content_hash,
        "fetched_at": fetched_at,
        "errors": errors,
    }


def _document_payload(
    *,
    requested_url: str,
    final_url: str,
    source_url: str,
    body: bytes,
    content_type: str,
    status: int,
    source_kind: str,
    focus: str,
    max_chars: int,
    fetched_at: str,
    title_override: str = "",
    fetch_error: dict[str, str] | None = None,
) -> dict[str, Any]:
    if fetch_error:
        return {
            "ok": False,
            "url": requested_url,
            "final_url": final_url,
            "source_kind": source_kind,
            "title": title_override,
            "sections": [],
            "links": [],
            "evidence": [],
            "content_hash": "",
            "fetched_at": fetched_at,
            "errors": [fetch_error],
        }
    if _is_pdf(final_url, content_type):
        return _unsupported_payload(
            requested_url,
            final_url,
            source_kind,
            fetched_at,
            "unsupported_pdf",
            "PDF extraction is not supported by fetch_capabilities v1",
        )
    if not _is_textual(final_url, content_type):
        return _unsupported_payload(
            requested_url,
            final_url,
            source_kind,
            fetched_at,
            "unsupported_content_type",
            f"unsupported content type: {content_type or 'unknown'}",
        )

    text = _decode_body(body, content_type)
    content_hash = _hash_text(text)
    html_links: list[dict[str, str]] = []
    title = title_override
    if _looks_like_html(final_url, content_type, text):
        parser = _HTMLTextExtractor(final_url)
        parser.feed(text)
        text = parser.markdown_text()
        title = title or _compact_text(parser.title)
        html_links = parser.links
        source_kind = source_kind if source_kind != "unknown" else "docs_site"
        if len(_compact_text(text)) < 100 and "<script" in body[:50_000].lower().decode(
            "utf-8", errors="ignore"
        ):
            return _unsupported_payload(
                requested_url,
                final_url,
                source_kind,
                fetched_at,
                "needs_browser",
                "page appears to require browser rendering",
            )

    sections = _markdown_sections(text, source_url, title_override=title)
    if not title:
        title = _document_title(sections, final_url)
    links = _dedupe_links(html_links + _links_from_text(text, final_url))
    selected = _select_sections(
        sections,
        focus=focus,
        fragment=urlparse(requested_url).fragment or urlparse(final_url).fragment,
        max_chars=max_chars,
    )
    evidence = _evidence_from_sections(selected, content_hash, fetched_at)
    ok = status < 400 and bool(selected)
    return {
        "ok": ok,
        "url": requested_url,
        "final_url": final_url,
        "source_kind": source_kind,
        "title": title,
        "sections": selected,
        "links": links,
        "evidence": evidence,
        "content_hash": content_hash,
        "fetched_at": fetched_at,
        "errors": [] if ok else [_error("no_readable_sections", "no readable text sections found")],
    }


def _markdown_sections(
    text: str, source_url: str, *, title_override: str = ""
) -> list[dict[str, Any]]:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    sections: list[dict[str, Any]] = []
    current = _new_section(title_override or "Document", source_url)
    in_code = False
    code_lines: list[str] = []

    for line in lines:
        heading = None if in_code else _markdown_heading(line)
        if heading:
            _finish_code_block(current, code_lines)
            code_lines = []
            _append_section(sections, current)
            current = _new_section(heading, source_url)
            continue
        if line.strip().startswith("```") or line.strip().startswith("~~~"):
            if in_code:
                _finish_code_block(current, code_lines)
                code_lines = []
                in_code = False
            else:
                in_code = True
            current["text"] += line.rstrip() + "\n"
            continue
        if in_code:
            code_lines.append(line)
            current["text"] += line.rstrip() + "\n"
        else:
            current["text"] += line.rstrip() + "\n"
    _finish_code_block(current, code_lines)
    _append_section(sections, current)
    return sections


def _new_section(heading: str, source_url: str) -> dict[str, Any]:
    anchor = "#" + _slugify(heading) if heading and heading != "Document" else ""
    return {
        "heading": _compact_text(heading) or "Document",
        "anchor": anchor,
        "text": "",
        "code_blocks": [],
        "source_url": _with_fragment(source_url, anchor),
    }


def _append_section(sections: list[dict[str, Any]], section: dict[str, Any]) -> None:
    text = section.get("text", "")
    section["text"] = _clean_section_text(text)
    section["code_blocks"] = [
        _truncate(block.strip(), 4_000)
        for block in section.get("code_blocks", [])
        if block.strip()
    ]
    if section["text"] or section["code_blocks"]:
        sections.append(section)


def _finish_code_block(section: dict[str, Any], code_lines: list[str]) -> None:
    code = "\n".join(code_lines).strip("\n")
    if code:
        section.setdefault("code_blocks", []).append(code)


def _select_sections(
    sections: list[dict[str, Any]], *, focus: str, fragment: str, max_chars: int
) -> list[dict[str, Any]]:
    if not sections:
        return []
    candidates = list(sections)
    normalized_fragment = _normalize_anchor(fragment)
    if normalized_fragment:
        matches = [
            section
            for section in candidates
            if _normalize_anchor(str(section.get("anchor", ""))) == normalized_fragment
            or _normalize_anchor(str(section.get("heading", ""))) == normalized_fragment
        ]
        if matches:
            candidates = matches
    elif focus.strip():
        candidates.sort(key=lambda section: _focus_score(section, focus), reverse=True)
        positive = [section for section in candidates if _focus_score(section, focus) > 0]
        if positive:
            candidates = positive + [
                section for section in candidates if _focus_score(section, focus) <= 0
            ]

    selected: list[dict[str, Any]] = []
    used = 0
    for section in candidates:
        text = str(section.get("text", ""))
        code_blocks = list(section.get("code_blocks") or [])
        section_size = len(text) + sum(len(block) for block in code_blocks)
        remaining = max_chars - used
        if remaining <= 0:
            break
        copied = {
            "heading": str(section.get("heading", "")),
            "anchor": str(section.get("anchor", "")),
            "text": _truncate(text, max(500, remaining)),
            "code_blocks": [
                _truncate(str(block), 4_000)
                for block in code_blocks[:6]
                if remaining > 0
            ],
            "source_url": str(section.get("source_url", "")),
        }
        selected.append(copied)
        used += min(section_size, remaining)
        if used >= max_chars:
            break
        if len(selected) >= 12:
            break
    return selected


def _evidence_from_sections(
    sections: list[dict[str, Any]], content_hash: str, fetched_at: str
) -> list[dict[str, str]]:
    evidence: list[dict[str, str]] = []
    for section in sections[:8]:
        excerpt = _excerpt(str(section.get("text", "")))
        if not excerpt and section.get("code_blocks"):
            excerpt = _excerpt(str(section["code_blocks"][0]))
        if not excerpt:
            continue
        evidence.append(
            {
                "field": "source",
                "title": str(section.get("heading", "")),
                "url": str(section.get("source_url", "")),
                "excerpt": excerpt,
                "heading": str(section.get("heading", "")),
                "anchor": str(section.get("anchor", "")),
                "source_url": str(section.get("source_url", "")),
                "content_hash": content_hash,
                "fetched_at": fetched_at,
            }
        )
    return evidence


def _github_target(url: str) -> dict[str, str] | None:
    parsed = urlparse(url)
    if parsed.netloc.lower() != "github.com":
        return None
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 2:
        return None
    owner, repo = parts[0], parts[1]
    if len(parts) >= 5 and parts[2] == "blob":
        ref = parts[3]
        path = "/".join(parts[4:])
        return {
            "kind": "blob",
            "owner": owner,
            "repo": repo,
            "path": path,
            "raw_url": f"https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{path}",
        }
    if len(parts) == 2:
        return {"kind": "repo", "owner": owner, "repo": repo}
    return None


def _source_kind(url: str, content_type: str, source_hint: str) -> str:
    hint = source_hint.strip().lower()
    if hint in {"docs_site", "github_repo", "github_file", "markdown", "raw_file"}:
        return hint
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    path = parsed.path.lower()
    if host == "raw.githubusercontent.com":
        return "github_file"
    if path.endswith((".md", ".mdx")):
        return "markdown"
    if any(path.endswith(ext) for ext in TEXT_EXTENSIONS):
        return "raw_file"
    if "html" in content_type.lower():
        return "docs_site"
    if _is_textual(url, content_type):
        return "raw_file"
    return "unknown"


def _validate_public_http_url(url: str) -> dict[str, str] | None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return _error("invalid_scheme", "only http and https URLs are supported")
    if not parsed.hostname:
        return _error("invalid_url", "URL host is required")
    host = parsed.hostname.strip().lower()
    if host == "localhost" or host.endswith(".localhost"):
        return _error("private_address", "localhost URLs are not allowed")
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        return _error("dns_error", str(exc))
    for info in infos:
        address = info[4][0]
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            return _error("dns_error", f"invalid resolved address: {address}")
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            return _error("private_address", f"resolved address is not public: {address}")
    return None


def _is_textual(url: str, content_type: str) -> bool:
    lower_type = content_type.lower().split(";")[0].strip()
    path = urlparse(url).path.lower()
    structured_types = {
        "application/json",
        "application/ld+json",
        "application/x-yaml",
        "application/yaml",
        "application/toml",
        "application/xml",
        "application/xhtml+xml",
    }
    if lower_type.startswith("text/") or lower_type in structured_types:
        return True
    if any(path.endswith(ext) for ext in TEXT_EXTENSIONS):
        return True
    return lower_type == "application/octet-stream" and any(
        path.endswith(ext) for ext in TEXT_EXTENSIONS
    )


def _looks_like_html(url: str, content_type: str, text: str) -> bool:
    lower = content_type.lower()
    path = urlparse(url).path.lower()
    return "html" in lower or path.endswith((".html", ".htm")) or "<html" in text[:500].lower()


def _is_pdf(url: str, content_type: str) -> bool:
    return "application/pdf" in content_type.lower() or urlparse(url).path.lower().endswith(
        ".pdf"
    )


def _decode_body(body: bytes, content_type: str) -> str:
    match = re.search(r"charset=([\w.-]+)", content_type, re.IGNORECASE)
    encodings = [match.group(1)] if match else []
    encodings.extend(["utf-8", "utf-16", "latin-1"])
    for encoding in encodings:
        try:
            return body.decode(encoding, errors="replace")
        except LookupError:
            continue
    return body.decode("utf-8", errors="replace")


def _markdown_heading(line: str) -> str | None:
    match = re.match(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$", line)
    if not match:
        return None
    return _compact_text(match.group(2))


def _links_from_text(text: str, base_url: str) -> list[dict[str, str]]:
    links: list[dict[str, str]] = []
    for match in re.finditer(r"\[([^\]]{1,200})\]\((https?://[^)\s]+)\)", text):
        links.append(
            {
                "title": _compact_text(match.group(1)),
                "url": urljoin(base_url, match.group(2)),
            }
        )
    for match in re.finditer(r"https?://[^\s<>)\"']+", text):
        links.append({"title": "", "url": match.group(0).rstrip(".,;:")})
    return _dedupe_links(links)


def _dedupe_links(links: list[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[str] = set()
    result: list[dict[str, str]] = []
    for link in links:
        raw_url = str(link.get("url") or "").strip()
        if not _is_http_url(raw_url):
            continue
        normalized = raw_url.rstrip("/")
        if normalized in seen:
            continue
        seen.add(normalized)
        result.append(
            {
                "title": _compact_text(str(link.get("title") or "")),
                "url": raw_url,
                "kind": _link_kind(raw_url),
            }
        )
        if len(result) >= 80:
            break
    return result


def _link_kind(url: str) -> str:
    parsed = urlparse(url)
    if parsed.netloc.lower() == "github.com":
        target = _github_target(url)
        if target:
            return "github_" + target["kind"]
    if parsed.netloc.lower() == "raw.githubusercontent.com":
        return "github_file"
    return "docs_site"


def _focus_score(section: dict[str, Any], focus: str) -> int:
    focus_tokens = _tokens(focus)
    if not focus_tokens:
        return 0
    heading_tokens = _tokens(str(section.get("heading", "")))
    text_tokens = _tokens(str(section.get("text", ""))[:2_000])
    return 3 * len(focus_tokens & heading_tokens) + len(focus_tokens & text_tokens)


def _tokens(value: str) -> set[str]:
    return {token for token in re.findall(r"[\w.-]+", value.lower()) if len(token) > 1}


def _document_title(sections: list[dict[str, Any]], final_url: str) -> str:
    for section in sections:
        heading = str(section.get("heading", "")).strip()
        if heading and heading != "Document":
            return heading
    path = urlparse(final_url).path.rstrip("/").split("/")[-1]
    return path or final_url


def _clean_section_text(text: str) -> str:
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _compact_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _excerpt(text: str, limit: int = 360) -> str:
    return _truncate(_compact_text(text), limit)


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 24)].rstrip() + "... (truncated)"


def _slugify(value: str) -> str:
    slug = re.sub(r"[^\w\s-]", "", value.lower(), flags=re.UNICODE)
    slug = re.sub(r"[\s_]+", "-", slug).strip("-")
    return slug or "section"


def _normalize_anchor(value: str) -> str:
    value = value.strip().lstrip("#")
    if not value:
        return ""
    return _slugify(value)


def _with_fragment(url: str, anchor: str) -> str:
    if not anchor:
        return url
    parsed = urlparse(url)
    return urlunparse(parsed._replace(fragment=anchor.lstrip("#")))


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def _normalize_max_chars(value: int) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    if parsed <= 0:
        return None
    return min(MAX_OUTPUT_CHARS, max(MIN_OUTPUT_CHARS, parsed))


def _is_http_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _error(code: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"code": code, "message": message, **extra}


def _json_result(
    *,
    ok: bool,
    url: str,
    fetched_at: str,
    final_url: str = "",
    source_kind: str = "unknown",
    title: str = "",
    sections: list[dict[str, Any]] | None = None,
    links: list[dict[str, str]] | None = None,
    evidence: list[dict[str, str]] | None = None,
    content_hash: str = "",
    errors: list[dict[str, str]] | None = None,
) -> str:
    return json.dumps(
        {
            "ok": ok,
            "url": url,
            "final_url": final_url,
            "source_kind": source_kind,
            "title": title,
            "sections": sections or [],
            "links": links or [],
            "evidence": evidence or [],
            "content_hash": content_hash,
            "fetched_at": fetched_at,
            "errors": errors or [],
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def _unsupported_payload(
    requested_url: str,
    final_url: str,
    source_kind: str,
    fetched_at: str,
    code: str,
    message: str,
) -> dict[str, Any]:
    return {
        "ok": False,
        "url": requested_url,
        "final_url": final_url,
        "source_kind": source_kind,
        "title": "",
        "sections": [],
        "links": [],
        "evidence": [],
        "content_hash": "",
        "fetched_at": fetched_at,
        "errors": [_error(code, message)],
    }
