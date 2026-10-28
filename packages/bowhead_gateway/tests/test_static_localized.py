"""Unit tests for LocalizedStaticFiles · 内容协商决定 zh / en static dir.

不启 FastAPI app, 只测 _detect_locale 解析逻辑 + ASGI dispatch 走对的 StaticFiles 实例.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import httpx2
import pytest
from bowhead_core.theme_surface_registry_generated import (
    CANONICAL_THEME_TOKEN_CSS,
)
from bowhead_gateway.public_entry_context import (
    PublicAccessContext,
    reset_public_access_context,
    set_public_access_context,
)
from bowhead_gateway.static_localized import (
    LocalizedStaticFiles,
    RuntimeBasePathStaticFiles,
    _detect_locale,
    _rewrite_css_urls_for_base_path,
    _rewrite_html_for_base_path,
    _rewrite_webpack_public_path,
    frontend_static_app,
    public_document_title,
)

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(autouse=True)
def business_copy_identity(monkeypatch):
    """Pin historical display copy without changing the installed adapters."""
    from bowhead_gateway import product_identity

    monkeypatch.setattr(product_identity, "read_build_edition", lambda: "business")


def _scope(headers: list[tuple[bytes, bytes]]) -> dict:
    return {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": headers,
    }


def _public_context(base_url: str, base_path: str) -> PublicAccessContext:
    return PublicAccessContext(
        base_url=base_url,
        origin="https://dev.example",
        base_path=base_path,
        app_root=f"{base_url}/",
        match_source="forwarded_url",
    )


@pytest.fixture(autouse=True)
def resolved_public_context() -> object:
    token = set_public_access_context(
        _public_context("https://dev.example/bowhead", "/bowhead"),
    )
    try:
        yield
    finally:
        reset_public_access_context(token)


# ─── _detect_locale: cookie precedence ──────────────────────


def test_detect_locale_cookie_zh():
    assert _detect_locale(_scope([(b"cookie", b"NEXT_LOCALE=zh-CN")])) == "zh-CN"


def test_detect_locale_cookie_en():
    assert _detect_locale(_scope([(b"cookie", b"NEXT_LOCALE=en-US")])) == "en-US"


def test_detect_locale_cookie_with_others():
    assert _detect_locale(_scope([(b"cookie", b"foo=bar; NEXT_LOCALE=en-US; sid=abc")])) == "en-US"


def test_detect_locale_invalid_cookie_follows_browser():
    assert _detect_locale(_scope([(b"cookie", b"NEXT_LOCALE=ja-JP"), (b"accept-language", b"zh-CN")])) == "zh-CN"


# ─── _detect_locale: Accept-Language fallback ────────────────


def test_detect_locale_accept_en():
    assert _detect_locale(_scope([(b"accept-language", b"en-US,en;q=0.9")])) == "en-US"


def test_detect_locale_accept_zh():
    assert _detect_locale(_scope([(b"accept-language", b"zh-CN,zh;q=0.9")])) == "zh-CN"


def test_detect_locale_accept_zh_with_en_fallback():
    assert _detect_locale(_scope([(b"accept-language", b"zh,en;q=0.5")])) == "zh-CN"


def test_detect_locale_accept_de():
    assert _detect_locale(_scope([(b"accept-language", b"de-DE")])) == "de-DE"


def test_detect_locale_accept_fr():
    assert _detect_locale(_scope([(b"accept-language", b"fr-FR,de;q=0.5")])) == "fr-FR"
    assert _detect_locale(_scope([(b"accept-language", b"fr-CA")])) == "fr-FR"


def test_detect_locale_cookie_fr():
    assert _detect_locale(_scope([(b"cookie", b"NEXT_LOCALE=fr-FR"), (b"accept-language", b"zh-CN")])) == "fr-FR"


def test_detect_locale_unknown_browser_language_defaults_to_english():
    assert _detect_locale(_scope([(b"accept-language", b"ja-JP")])) == "en-US"


def test_detect_locale_no_headers_defaults_to_english():
    assert _detect_locale(_scope([])) == "en-US"


# ─── Cookie precedes Accept-Language ────────────────────────


def test_detect_locale_cookie_overrides_accept():
    assert (
        _detect_locale(
            _scope(
                [
                    (b"cookie", b"NEXT_LOCALE=en-US"),
                    (b"accept-language", b"zh-CN,zh;q=0.9"),
                ]
            )
        )
        == "en-US"
    )


# ─── ASGI dispatch: real StaticFiles, correct directory ──────


@pytest.fixture
def localized_app(tmp_path: Path) -> LocalizedStaticFiles:
    zh = tmp_path / "zh-CN"
    en = tmp_path / "en-US"
    zh.mkdir()
    en.mkdir()
    de = tmp_path / "de-DE"
    de.mkdir()
    (zh / "index.html").write_text("<html>ZH</html>")
    (en / "index.html").write_text("<html>EN</html>")
    (de / "index.html").write_text("<html>DE</html>")
    fr = tmp_path / "fr-FR"
    fr.mkdir()
    (fr / "index.html").write_text("<html>FR</html>")
    return LocalizedStaticFiles(zh_dir=str(zh), en_dir=str(en), de_dir=str(de), fr_dir=str(fr))


async def _served_body(app: LocalizedStaticFiles, headers: list[tuple[bytes, bytes]]) -> bytes:
    received_body: list[bytes] = []

    async def receive():
        return {"type": "http.disconnect"}

    async def send(msg):
        if msg["type"] == "http.response.body":
            received_body.append(msg.get("body", b""))

    scope = {"type": "http", "method": "GET", "path": "/index.html", "headers": headers}
    await app(scope, receive, send)
    return b"".join(received_body)


@pytest.mark.asyncio
async def test_dispatch_serves_fr_when_explicitly_selected(localized_app: LocalizedStaticFiles):
    for headers in ([(b"cookie", b"NEXT_LOCALE=fr-FR")], [(b"cookie", b"NEXT_LOCALE=fr-FR"), (b"accept-language", b"zh-CN")]):
        body = await _served_body(localized_app, headers)
        assert b"FR" in body and b"ZH" not in body and b"EN" not in body


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("browser_language", "expected_body"),
    [(b"zh-TW", b"ZH"), (b"en-GB", b"EN"), (b"de-AT", b"DE"), (b"fr-CA", b"FR"), (b"ja-JP", b"EN")],
)
async def test_dispatch_selects_browser_language_before_hydration(localized_app: LocalizedStaticFiles, browser_language: bytes, expected_body: bytes):
    body = await _served_body(localized_app, [(b"accept-language", browser_language)])
    assert body == b"<html>" + expected_body + b"</html>"


@pytest.mark.asyncio
async def test_dispatch_serves_en_for_fr_when_fr_build_missing(tmp_path: Path):
    """旧镜像没有法语目录时，法语请求拿英文那份，绝不落回中文。"""
    zh = tmp_path / "zh-CN"
    en = tmp_path / "en-US"
    zh.mkdir()
    en.mkdir()
    (zh / "index.html").write_text("<html>ZH</html>")
    (en / "index.html").write_text("<html>EN</html>")
    app = LocalizedStaticFiles(zh_dir=str(zh), en_dir=str(en))

    body = await _served_body(app, [(b"cookie", b"NEXT_LOCALE=fr-FR")])

    assert b"EN" in body and b"ZH" not in body


@pytest.mark.asyncio
async def test_dispatch_serves_en_when_cookie(localized_app: LocalizedStaticFiles):
    received_body: list[bytes] = []

    async def receive():
        return {"type": "http.disconnect"}

    async def send(msg):
        if msg["type"] == "http.response.body":
            received_body.append(msg.get("body", b""))

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/index.html",
        "headers": [(b"cookie", b"NEXT_LOCALE=en-US")],
    }
    await localized_app(scope, receive, send)
    body = b"".join(received_body)
    assert b"EN" in body and b"ZH" not in body


@pytest.mark.asyncio
async def test_dispatch_serves_english_by_default(localized_app: LocalizedStaticFiles):
    received_body: list[bytes] = []

    async def receive():
        return {"type": "http.disconnect"}

    async def send(msg):
        if msg["type"] == "http.response.body":
            received_body.append(msg.get("body", b""))

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/index.html",
        "headers": [],
    }
    await localized_app(scope, receive, send)
    body = b"".join(received_body)
    assert b"EN" in body and b"ZH" not in body


@pytest.mark.asyncio
async def test_dispatch_serves_de_when_explicitly_selected(localized_app: LocalizedStaticFiles):
    received_body: list[bytes] = []

    async def receive():
        return {"type": "http.disconnect"}

    async def send(msg):
        if msg["type"] == "http.response.body":
            received_body.append(msg.get("body", b""))

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/index.html",
        "headers": [(b"cookie", b"NEXT_LOCALE=de-DE")],
    }
    await localized_app(scope, receive, send)
    body = b"".join(received_body)
    assert b"DE" in body and b"ZH" not in body and b"EN" not in body


def test_rewrite_html_for_runtime_base_path() -> None:
    html = '<html><head></head><body><script src="/_next/app.js"></script><a href="/login">Log in</a><script>self.__next_f.push([1,\'0:{"p":""}\'])</script></body></html>'

    result = _rewrite_html_for_base_path(html, "/bowhead")

    assert 'window.__BOWHEAD_BASE_PATH__="/bowhead"' in result
    assert 'window.__BOWHEAD_BASE_URL__="https://dev.example/bowhead"' in result
    assert '"baseUrl":"https://dev.example/bowhead"' in result
    assert '<meta name="bowhead-base-path" content="/bowhead">' in result
    assert "history[m]=function(state,title,url)" in result
    assert "toPublicHistoryUrl(url)" in result
    assert 'u==="/"?p+"/":p+u' in result
    assert "window.fetch=function(input,init)" in result
    assert "toPublicRequest" in result
    assert "init&&init.headers?init.headers:request instanceof Request?request.headers:undefined" in result
    assert "document.createElement=function" in result
    assert 'document.addEventListener("click"' in result
    assert 'ensureMetadataNode("bowhead-document-title","title")' in result
    assert 'ensureMetadataNode("bowhead-runtime-favicon","link")' in result
    assert "title.textContent=documentTitle;" in result
    assert 'applyMetadata("Bowhead \\u2014 Verified Intelligence",toPublic("/bowhead-icon.svg")' in result
    assert "finalizePublicBrand" not in result
    assert 'document.addEventListener("DOMContentLoaded"' not in result
    assert "document.title" not in result
    assert "data-bowhead-public-favicon" not in result
    assert 'toPublic("/bowhead-icon.svg")' in result
    assert 'src="_next/app.js"' in result
    assert 'href="login"' in result
    assert '<script>self.__next_f.push([1,\'0:{"p":""}\'])</script>' in result


def test_rewrite_html_preserves_length_prefixed_flight_payload() -> None:
    token = set_public_access_context(
        _public_context(
            "https://dev.example/ai/apps/bowhead",
            "/ai/apps/bowhead",
        ),
    )
    flight_script = '<script>self.__next_f.push([1,"4:T16,{\\"href\\":\\"/_next/x.js\\"}"])</script>'
    html = f'<html><head><link href="/_next/app.css"></head><body>{flight_script}</body></html>'

    try:
        result = _rewrite_html_for_base_path(html, "/ai/apps/bowhead")
    finally:
        reset_public_access_context(token)

    assert 'href="_next/app.css"' in result
    assert flight_script in result


def test_rewrite_html_injects_public_context_for_root_deployment() -> None:
    token = set_public_access_context(_public_context("https://dev.example", ""))
    html = '<html><head></head><body><script src="/_next/app.js"></script></body></html>'

    try:
        result = _rewrite_html_for_base_path(html, "")
    finally:
        reset_public_access_context(token)

    assert 'window.__BOWHEAD_BASE_PATH__=""' in result
    assert 'window.__BOWHEAD_BASE_URL__="https://dev.example"' in result
    assert '"matchSource":"forwarded_url"' in result
    assert 'src="_next/app.js"' in result


def test_rewrite_html_uses_browser_relative_paths_for_direct_root_context() -> None:
    context = PublicAccessContext(
        base_url="https://dev.example",
        origin="https://dev.example",
        base_path="",
        app_root="https://dev.example/",
        match_source="direct",
    )
    token = set_public_access_context(context)
    html = '<html><head><link href="/_next/app.css"></head><body><script src="/_next/app.js"></script><a href="/login/">Log in</a></body></html>'

    try:
        result = _rewrite_html_for_base_path(html, "", internal_path="/")
    finally:
        reset_public_access_context(token)

    assert 'href="_next/app.css"' in result
    assert 'src="_next/app.js"' in result
    assert 'href="login/"' in result


def test_rewrite_html_uses_browser_relative_paths_without_server_context() -> None:
    token = set_public_access_context(None)
    html = '<html><head><link href="/_next/app.css"></head><body><script src="/_next/app.js"></script><a href="/login/">Log in</a></body></html>'

    try:
        result = _rewrite_html_for_base_path(
            html,
            "",
            internal_path="/settings/",
        )
    finally:
        reset_public_access_context(token)

    assert 'href="../_next/app.css"' in result
    assert 'src="../_next/app.js"' in result
    assert 'href="../login/"' in result
    assert '<meta name="bowhead-internal-path" content="/settings/">' in result
    assert "location.pathname" in result
    assert "X-Bowhead-Public-Base" in result
    assert "internal.example:8000" not in result


def test_rewrite_html_uses_browser_relative_paths_for_cookie_context() -> None:
    context = PublicAccessContext(
        base_url="https://dev.example",
        origin="https://dev.example",
        base_path="",
        app_root="https://dev.example/",
        match_source="cookie",
        entry_id="pe_root",
        team_id="team_a",
    )
    token = set_public_access_context(context)
    html = '<html><head><link href="/_next/app.css"></head><body><a href="/login/">Log in</a></body></html>'

    try:
        result = _rewrite_html_for_base_path(
            html,
            "",
            internal_path="/settings/",
        )
    finally:
        reset_public_access_context(token)

    assert 'href="../_next/app.css"' in result
    assert 'href="../login/"' in result
    assert '"matchSource":"cookie"' in result


@pytest.mark.asyncio
async def test_runtime_static_injects_public_context_for_root_deployment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    token = set_public_access_context(_public_context("https://dev.example", ""))
    (tmp_path / "index.html").write_text("<html><head></head><body>index</body></html>")
    static_files = RuntimeBasePathStaticFiles(directory=tmp_path, html=True)

    try:
        response = await static_files.get_response(
            "index.html",
            {
                "type": "http",
                "method": "GET",
                "path": "/index.html",
                "root_path": "",
                "scheme": "http",
                "server": ("internal.example", 8000),
                "headers": [],
            },
        )
    finally:
        reset_public_access_context(token)

    assert response.status_code == 200
    assert 'window.__BOWHEAD_BASE_URL__="https://dev.example"' in response.body.decode()
    assert response.headers["cache-control"] == "no-cache"


@pytest.mark.asyncio
async def test_runtime_static_serves_html_without_server_context(
    tmp_path: Path,
) -> None:
    (tmp_path / "login").mkdir()
    (tmp_path / "login" / "index.html").write_text(
        '<html><head><script src="/_next/app.js"></script></head><body><a href="/">Home</a></body></html>',
    )
    static_files = RuntimeBasePathStaticFiles(directory=tmp_path, html=True)
    token = set_public_access_context(None)

    try:
        response = await static_files.get_response(
            "login/index.html",
            {
                "type": "http",
                "method": "GET",
                "path": "/login/",
                "root_path": "",
                "scheme": "http",
                "server": ("internal.example", 8000),
                "headers": [],
            },
        )
    finally:
        reset_public_access_context(token)

    assert response.status_code == 200
    html = response.body.decode()
    assert 'src="../_next/app.js"' in html
    assert 'href="../"' in html
    assert "internal.example:8000" not in html


@pytest.mark.asyncio
async def test_runtime_static_preserves_encoded_internal_route_anchor(
    tmp_path: Path,
) -> None:
    (tmp_path / "café").mkdir()
    (tmp_path / "café" / "index.html").write_text(
        "<html><head></head><body>café</body></html>",
    )
    static_files = RuntimeBasePathStaticFiles(directory=tmp_path, html=True)
    token = set_public_access_context(None)

    try:
        response = await static_files.get_response(
            "café/index.html",
            {
                "type": "http",
                "method": "GET",
                "path": "/café/",
                "raw_path": b"/caf%C3%A9/",
                "root_path": "",
                "scheme": "http",
                "server": ("internal.example", 8000),
                "headers": [],
            },
        )
    finally:
        reset_public_access_context(token)

    assert response.status_code == 200
    assert '<meta name="bowhead-internal-path" content="/caf%C3%A9/">' in response.body.decode()


@pytest.mark.asyncio
async def test_runtime_static_injects_resolved_public_brand(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    token = set_public_access_context(_public_context("https://dev.example", ""))
    (tmp_path / "login").mkdir()
    (tmp_path / "login" / "index.html").write_text(
        "<html><head><title>Bowhead</title></head><body>login</body></html>",
    )
    seen = []

    async def provider(context):  # type: ignore[no-untyped-def]
        seen.append(context)
        return {"schemaVersion": 2, "brand": {"applicationName": "Acme"}}

    static_files = RuntimeBasePathStaticFiles(
        directory=tmp_path,
        html=True,
        public_brand_provider=provider,
    )
    try:
        response = await static_files.get_response(
            "login/index.html",
            {
                "type": "http",
                "method": "GET",
                "path": "/login/index.html",
                "root_path": "",
                "scheme": "http",
                "server": ("internal.example", 8000),
                "headers": [],
            },
        )
    finally:
        reset_public_access_context(token)

    html = response.body.decode()
    assert seen and seen[0] is not None
    assert 'window.__BOWHEAD_PUBLIC_BRAND__={"schemaVersion":2,"brand":{"applicationName":"Acme"}}' in html
    assert 'style.id="bowhead-team-brand-tokens"' in html
    assert "Object.keys(tokens).length!==keys.length" in html
    token_names = json.dumps(
        CANONICAL_THEME_TOKEN_CSS,
        ensure_ascii=True,
        separators=(",", ":"),
    )
    assert f"const names={token_names}" in html
    assert "!/^#[0-9A-F]{6}$/.test(color)" in html
    assert "r.style.setProperty" not in html
    assert "internal.example:8000" not in html


def test_rewrite_webpack_public_path_for_runtime_base_path() -> None:
    script = '(()=>{var r={};r.p="/_next/",r.u=e=>"static/chunks/"+e})()'

    assert _rewrite_webpack_public_path(script) == ('(()=>{var r={};r.p=(window.__BOWHEAD_BASE_PATH__||"")+"/_next/",r.u=e=>"static/chunks/"+e})()')


@pytest.mark.asyncio
async def test_runtime_static_webpack_uses_browser_base_not_internal_asset_path(
    tmp_path: Path,
) -> None:
    chunks = tmp_path / "_next" / "static" / "chunks"
    chunks.mkdir(parents=True)
    (chunks / "webpack-test.js").write_text('var r={};r.p="/_next/";')
    static_files = RuntimeBasePathStaticFiles(directory=tmp_path, html=True)

    response = await static_files.get_response(
        "_next/static/chunks/webpack-test.js",
        {
            "type": "http",
            "method": "GET",
            "path": "/_next/static/chunks/webpack-test.js",
            "root_path": "",
            "scheme": "http",
            "headers": [],
        },
    )

    assert response.status_code == 200
    assert response.body.decode() == ('var r={};r.p=(window.__BOWHEAD_BASE_PATH__||"")+"/_next/";')
    assert "../" not in response.body.decode()


def test_rewrite_css_urls_for_runtime_base_path() -> None:
    css = (
        '@font-face{src:url(/_next/static/media/font.woff2) format("woff2")} '
        '.icon{background:url("/_next/static/media/icon.svg")} '
        ".remote{background:url(https://cdn.example/icon.svg)}"
    )

    assert _rewrite_css_urls_for_base_path(css, "/bowhead") == (
        '@font-face{src:url(/bowhead/_next/static/media/font.woff2) format("woff2")} '
        '.icon{background:url("/bowhead/_next/static/media/icon.svg")} '
        ".remote{background:url(https://cdn.example/icon.svg)}"
    )


@pytest.mark.asyncio
async def test_runtime_static_rewrites_css_font_urls_for_base_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    (tmp_path / "app.css").write_text(
        "@font-face{src:url(/_next/static/media/6c596dfcddeca1e9-s.p.woff2)}",
    )
    static_files = RuntimeBasePathStaticFiles(directory=tmp_path, html=True)

    response = await static_files.get_response(
        "app.css",
        {
            "type": "http",
            "method": "GET",
            "path": "/app.css",
            "root_path": "",
            "scheme": "http",
            "server": ("internal.example", 8000),
            "headers": [],
        },
    )

    assert response.body == b"@font-face{src:url(_next/static/media/6c596dfcddeca1e9-s.p.woff2)}"


@pytest.mark.asyncio
async def test_runtime_static_uses_request_scoped_base_path_for_css(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    css_dir = tmp_path / "_next" / "static" / "css"
    css_dir.mkdir(parents=True)
    (css_dir / "app.css").write_text(
        "@font-face{src:url(/_next/static/media/font.woff2)}",
    )
    static_files = RuntimeBasePathStaticFiles(directory=tmp_path, html=True)
    context = PublicAccessContext(
        base_url="https://dev.example/ai/apps/bowhead",
        origin="https://dev.example",
        base_path="/ai/apps/bowhead",
        app_root="https://dev.example/ai/apps/bowhead/",
        match_source="forwarded_url",
    )
    token = set_public_access_context(context)

    try:
        response = await static_files.get_response(
            "_next/static/css/app.css",
            {
                "type": "http",
                "method": "GET",
                "path": "/_next/static/css/app.css",
                "root_path": "",
                "scheme": "http",
                "server": ("internal.example", 8000),
                "headers": [],
            },
        )
    finally:
        reset_public_access_context(token)

    assert response.status_code == 200
    assert response.body.decode() == "@font-face{src:url(../media/font.woff2)}"
    assert response.headers["cache-control"] == "private, max-age=31536000, immutable"


@pytest.mark.asyncio
async def test_frontend_static_app_compresses_hashed_assets(
    tmp_path: Path,
) -> None:
    css_dir = tmp_path / "_next" / "static" / "css"
    css_dir.mkdir(parents=True)
    css = ".large{color:#123456}" * 4_000
    (css_dir / "app.abc123.css").write_text(css)
    app = frontend_static_app(
        RuntimeBasePathStaticFiles(directory=tmp_path, html=True),
    )

    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app),
        base_url="https://dev.example",
    ) as client:
        response = await client.get(
            "/_next/static/css/app.abc123.css",
            headers={"accept-encoding": "gzip"},
        )

    assert response.status_code == 200
    assert response.text == css.replace("/_next/", "../")
    assert response.headers["content-encoding"] == "gzip"
    assert response.headers["vary"] == "Accept-Encoding"
    assert int(response.headers["content-length"]) < len(css.encode())
    assert response.headers["cache-control"] == "private, max-age=31536000, immutable"


@pytest.mark.asyncio
async def test_runtime_static_uses_relative_css_urls_without_server_context(
    tmp_path: Path,
) -> None:
    css_dir = tmp_path / "_next" / "static" / "css"
    css_dir.mkdir(parents=True)
    (css_dir / "app.css").write_text(
        "@font-face{src:url(/_next/static/media/font.woff2)}",
    )
    static_files = RuntimeBasePathStaticFiles(directory=tmp_path, html=True)
    token = set_public_access_context(None)

    try:
        response = await static_files.get_response(
            "_next/static/css/app.css",
            {
                "type": "http",
                "method": "GET",
                "path": "/_next/static/css/app.css",
                "root_path": "",
                "scheme": "http",
                "server": ("internal.example", 8000),
                "headers": [],
            },
        )
    finally:
        reset_public_access_context(token)

    assert response.status_code == 200
    assert response.body.decode() == "@font-face{src:url(../media/font.woff2)}"


@pytest.mark.asyncio
async def test_runtime_static_uses_relative_css_urls_for_cookie_context(
    tmp_path: Path,
) -> None:
    css_dir = tmp_path / "_next" / "static" / "css"
    css_dir.mkdir(parents=True)
    (css_dir / "app.css").write_text(
        "@font-face{src:url(/_next/static/media/font.woff2)}",
    )
    static_files = RuntimeBasePathStaticFiles(directory=tmp_path, html=True)
    context = PublicAccessContext(
        base_url="https://dev.example",
        origin="https://dev.example",
        base_path="",
        app_root="https://dev.example/",
        match_source="cookie",
        entry_id="pe_root",
        team_id="team_a",
    )
    token = set_public_access_context(context)

    try:
        response = await static_files.get_response(
            "_next/static/css/app.css",
            {
                "type": "http",
                "method": "GET",
                "path": "/_next/static/css/app.css",
                "root_path": "",
                "scheme": "http",
                "server": ("internal.example", 8000),
                "headers": [],
            },
        )
    finally:
        reset_public_access_context(token)

    assert response.status_code == 200
    assert response.body.decode() == "@font-face{src:url(../media/font.woff2)}"


@pytest.mark.asyncio
async def test_runtime_static_root_redirect_uses_configured_public_url(tmp_path: Path) -> None:
    """Never build an external Location from the proxy's internal Host header."""
    (tmp_path / "index.html").write_text("<html>index</html>")
    static_files = RuntimeBasePathStaticFiles(directory=tmp_path, html=True)

    response = await static_files.get_response(
        "",
        {
            "type": "http",
            "method": "GET",
            "path": "/bowhead",
            "root_path": "",
            "scheme": "http",
            "server": ("dev.example", 8000),
            "headers": [],
        },
    )

    assert 300 <= response.status_code < 400
    assert response.headers["location"] == "./"


@pytest.mark.asyncio
async def test_runtime_static_directory_redirect_preserves_query_string(tmp_path: Path) -> None:
    """Directory-slash redirects must keep invite/reset tokens on the query."""
    (tmp_path / "invite").mkdir()
    (tmp_path / "invite" / "index.html").write_text("<html>invite</html>")
    static_files = RuntimeBasePathStaticFiles(directory=tmp_path, html=True)

    response = await static_files.get_response(
        "invite",
        {
            "type": "http",
            "method": "GET",
            "path": "/bowhead/invite",
            "root_path": "",
            "scheme": "http",
            "server": ("dev.example", 8000),
            "query_string": b"token=abc.def.ghi",
            "headers": [],
        },
    )

    assert 300 <= response.status_code < 400
    assert response.headers["location"] == "invite/?token=abc.def.ghi"


@pytest.mark.asyncio
async def test_dispatch_serves_en_for_de_when_de_build_missing(tmp_path: Path):
    """旧镜像没有德语目录时，德语请求拿英文那份，绝不落回中文。"""
    zh = tmp_path / "zh-CN"
    en = tmp_path / "en-US"
    zh.mkdir()
    en.mkdir()
    (zh / "index.html").write_text("<html>ZH</html>")
    (en / "index.html").write_text("<html>EN</html>")
    app = LocalizedStaticFiles(zh_dir=str(zh), en_dir=str(en))

    body = await _served_body(app, [(b"cookie", b"NEXT_LOCALE=de-DE")])

    assert b"EN" in body and b"ZH" not in body


@pytest.mark.parametrize(
    ("public_brand", "expected"),
    [
        # Product default brand (including built-in themes whose application name is still Bowhead) keeps the current title
        ({"schemaVersion": 2, "brand": {"applicationName": "Bowhead"}}, "Bowhead — Verified Intelligence"),
        # Custom brand: application name only, without the tagline
        ({"schemaVersion": 2, "brand": {"applicationName": "北辰数据中心"}}, "北辰数据中心"),
        # Missing or incomplete injection: fall back to the product title
        (None, "Bowhead — Verified Intelligence"),
        ({"schemaVersion": 2}, "Bowhead — Verified Intelligence"),
        ({"schemaVersion": 2, "brand": {"applicationName": "  "}}, "Bowhead — Verified Intelligence"),
    ],
)
def test_public_document_title_follows_injected_brand(public_brand, expected) -> None:
    assert public_document_title(public_brand) == expected

    html = _rewrite_html_for_base_path(
        "<html><head></head><body></body></html>",
        "",
        public_brand=public_brand,
    )
    encoded = json.dumps(expected, ensure_ascii=True)
    assert f'applyMetadata({encoded},href,"public-bootstrap",meta)' in html
