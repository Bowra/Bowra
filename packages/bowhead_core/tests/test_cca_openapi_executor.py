"""cca block ④c — SSRF-guarded httpx2 executor (tech-design §5/§6.2/§8/§9).

Uses httpx2.MockTransport (no real network) + an injected DNS resolver to assert:
the connect-by-IP request shape (URL host = validated IP, Host header + TLS SNI =
original host — DNS-rebind safe), arg→wire mapping (path/query/header/body), auth
injection, SSRF block, upstream-status classification, redirect/timeout/connection
handling, response trim, and that NO credential ever leaks into the returned error.
"""

from __future__ import annotations

import json

import httpx2
import pytest
from bowhead_core.openapi_tool_source import (
    _read_capped,
    _trim,
    http_call,
    make_http_executor,
    parse_openapi_ops,
    ssrf_get_text,
)
from bowhead_core.scrub import scrub as _scrub
from bowhead_core.ssrf_guard import SsrfBlocked


def _spec() -> dict:
    return {
        "openapi": "3.0.0",
        "info": {"title": "datarc", "version": "1"},
        "paths": {
            "/metrics/{mid}": {
                "get": {
                    "operationId": "getMetric",
                    "parameters": [
                        {"name": "mid", "in": "path", "required": True, "schema": {"type": "string"}},
                        {"name": "tenant", "in": "header", "required": True, "schema": {"type": "string"}},
                        {"name": "window", "in": "query", "required": False, "schema": {"type": "string"}},
                    ],
                    "responses": {"200": {"description": "ok"}},
                }
            },
            "/query": {
                "post": {
                    "operationId": "runQuery",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": ["metric"],
                                    "properties": {"metric": {"type": "string"}, "dims": {"type": "array", "items": {"type": "string"}}},
                                }
                            }
                        },
                    },
                    "responses": {"200": {"description": "ok"}},
                }
            },
        },
    }


def _op(op_id: str):
    return next(o for o in parse_openapi_ops(_spec()) if o.operation_id == op_id)


_PUBLIC = ["93.184.216.34"]  # validates (global)
_INTERNAL = ["10.0.0.5"]  # blocked (not is_global)


@pytest.fixture
def saas_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """IP 锁定与内网拦截只在 SaaS 模式生效（22014fa9e 起出站策略只由
    BOWHEAD_DEPLOYMENT_MODE 决定，默认 local 放行内网且不锁 IP）。显式声明，
    不依赖本机环境变量。"""
    monkeypatch.setenv("BOWHEAD_DEPLOYMENT_MODE", "saas")


def _resolver(ips):
    return lambda host: ips


@pytest.mark.asyncio
async def test_get_connects_by_ip_with_host_and_sni_preserved(saas_mode: None) -> None:
    seen: list[httpx2.Request] = []

    def handler(req: httpx2.Request) -> httpx2.Response:
        seen.append(req)
        return httpx2.Response(200, json={"value": 42})

    res = await http_call(
        "https://api.datarc.cn",
        _op("getMetric"),
        {"mid": "rev/2024", "tenant": "t1", "window": "30d"},
        {"Authorization": "Bearer secret-abc"},
        dns_resolver=_resolver(_PUBLIC),
        transport=httpx2.MockTransport(handler),
    )
    assert res["ok"] is True
    assert json.loads(res["data"]) == {"value": 42}

    req = seen[0]
    assert req.url.host == "93.184.216.34"  # connected to the validated IP
    assert req.headers["host"] == "api.datarc.cn"  # Host restored to original
    assert req.extensions.get("sni_hostname") == "api.datarc.cn"  # TLS pinned to host
    assert "rev%2F2024" in str(req.url)  # path param URL-encoded on the wire (raw URL)
    assert req.url.params.get("window") == "30d"  # query param
    assert req.headers["tenant"] == "t1"  # header param
    assert req.headers["authorization"] == "Bearer secret-abc"  # auth injected


@pytest.mark.asyncio
async def test_post_body_props_collected_into_json() -> None:
    seen: list[httpx2.Request] = []

    def handler(req: httpx2.Request) -> httpx2.Response:
        seen.append(req)
        return httpx2.Response(200, json={"rows": []})

    res = await http_call(
        "https://api.datarc.cn",
        _op("runQuery"),
        {"metric": "revenue", "dims": ["region", "month"]},
        {},
        dns_resolver=_resolver(_PUBLIC),
        transport=httpx2.MockTransport(handler),
    )
    assert res["ok"] is True
    body = json.loads(seen[0].content)
    assert body == {"metric": "revenue", "dims": ["region", "month"]}
    assert seen[0].method == "POST"


@pytest.mark.asyncio
async def test_ssrf_block_when_resolves_internal(saas_mode: None) -> None:
    def handler(req: httpx2.Request) -> httpx2.Response:  # pragma: no cover — must not run
        raise AssertionError("request must not be sent when SSRF-blocked")

    res = await http_call(
        "https://evil.example.com",
        _op("getMetric"),
        {"mid": "x", "tenant": "t"},
        {},
        dns_resolver=_resolver(_INTERNAL),
        transport=httpx2.MockTransport(handler),
    )
    assert res["ok"] is False
    assert res["error_kind"] == "blocked"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,kind",
    [
        (404, "upstream_4xx"),
        (503, "upstream_5xx"),
        (302, "redirect"),
        # credential-readiness R7: credential 4xx split out so ④e can attribute it.
        (401, "upstream_auth"),
        (403, "upstream_auth"),
    ],
)
async def test_status_classification(status: int, kind: str) -> None:
    def handler(req: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(status, json={"detail": "boom"}, headers={"location": "https://x"})

    res = await http_call(
        "https://api.datarc.cn",
        _op("getMetric"),
        {"mid": "x", "tenant": "t"},
        {},
        dns_resolver=_resolver(_PUBLIC),
        transport=httpx2.MockTransport(handler),
    )
    assert res["ok"] is False
    assert res["error_kind"] == kind
    assert str(status) in res["error"]  # status surfaced; body NOT echoed
    assert "boom" not in json.dumps(res)  # upstream body not leaked on error


@pytest.mark.asyncio
async def test_timeout_and_connection_errors_classified() -> None:
    def timeout_handler(req: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadTimeout("slow", request=req)

    def conn_handler(req: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("refused", request=req)

    r1 = await http_call(
        "https://api.datarc.cn",
        _op("getMetric"),
        {"mid": "x", "tenant": "t"},
        {},
        dns_resolver=_resolver(_PUBLIC),
        transport=httpx2.MockTransport(timeout_handler),
    )
    r2 = await http_call(
        "https://api.datarc.cn",
        _op("getMetric"),
        {"mid": "x", "tenant": "t"},
        {},
        dns_resolver=_resolver(_PUBLIC),
        transport=httpx2.MockTransport(conn_handler),
    )
    assert r1["error_kind"] == "timeout"
    assert r2["error_kind"] == "connection"


@pytest.mark.asyncio
async def test_no_credentials_leak_into_error_output() -> None:
    def handler(req: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(500, text="upstream error with token sk-deadbeef12345678")

    res = await http_call(
        "https://api.datarc.cn",
        _op("getMetric"),
        {"mid": "x", "tenant": "t"},
        {"Authorization": "Bearer super-secret-token-xyz"},
        dns_resolver=_resolver(_PUBLIC),
        transport=httpx2.MockTransport(handler),
    )
    blob = json.dumps(res)
    assert "super-secret-token-xyz" not in blob  # injected cred never echoed
    assert "sk-deadbeef12345678" not in blob  # upstream body not echoed at all
    assert res["error_kind"] == "upstream_5xx"


@pytest.mark.asyncio
async def test_make_http_executor_wires_resolver() -> None:
    async def src_resolver(team_id: str, source_id: str) -> tuple[str, dict[str, str]]:
        assert (team_id, source_id) == ("team_a", "ots_1")  # team_id bound at build time
        return "https://api.datarc.cn", {"X-Key": "k"}

    def handler(req: httpx2.Request) -> httpx2.Response:
        assert req.headers["x-key"] == "k"
        return httpx2.Response(200, json={"v": 1})

    execu = make_http_executor("team_a", src_resolver, dns_resolver=_resolver(_PUBLIC), transport=httpx2.MockTransport(handler))
    res = await execu("ots_1", _op("getMetric"), {"mid": "x", "tenant": "t"})
    assert res["ok"] is True


@pytest.mark.asyncio
async def test_success_body_is_scrubbed_of_reflected_creds() -> None:
    """cca review #2: an upstream that REFLECTS our injected auth header (or returns
    creds in its body) must not leak them to the LLM via the success `data`."""
    jwt = "eyJhbGciOiJIUzI1NiIs.payload+slash/eq="  # base64url JWT shape (+ / =)

    def handler(req: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json={"echoed": f"Authorization: Bearer {jwt}"})

    res = await http_call(
        "https://api.datarc.cn",
        _op("getMetric"),
        {"mid": "x", "tenant": "t"},
        {"Authorization": f"Bearer {jwt}"},
        dns_resolver=_resolver(_PUBLIC),
        transport=httpx2.MockTransport(handler),
    )
    assert res["ok"] is True
    assert jwt not in res["data"]  # reflected bearer token scrubbed (base64url)
    assert "[redacted]" in res["data"]


@pytest.mark.asyncio
async def test_read_capped_streams_and_truncates_without_full_buffer() -> None:
    """cca review #6: a chunked body with NO content-length is streamed + capped at
    max_bytes (early-abort) — not fully buffered. Drives _read_capped directly with a
    fake streaming response (httpx2 MockTransport always sets content-length, which the
    separate too_large guard handles)."""

    class _FakeResp:
        def __init__(self, chunks: list[bytes]) -> None:
            self._chunks = chunks
            self.yielded = 0

        async def aiter_bytes(self):
            for c in self._chunks:
                self.yielded += 1
                yield c

    resp = _FakeResp([b"x" * 600, b"y" * 600, b"z" * 600])  # 1800 bytes in 3 chunks
    out = await _read_capped(resp, max_bytes=1024)  # type: ignore[arg-type]
    assert len(out.encode()) < 1800  # capped
    assert "truncated" in out
    assert resp.yielded == 2  # stopped early after exceeding the cap (not all 3)


@pytest.mark.asyncio
async def test_read_capped_does_not_materialize_a_large_boundary_chunk() -> None:
    """cca re-review #9: a body that stays under the cap for small chunks then sends one
    large chunk (chunked / no content-length, so the too_large precheck can't catch it)
    must NOT be fully buffered. The earlier guard appended the whole boundary chunk then
    did ``b"".join(chunks)[:max_bytes]`` — materialising the large chunk in the list AND
    again in the join before slicing (~2 live copies). A correct cap retains ≤ max_bytes,
    so the only large allocation is the unavoidable single transient yielded chunk."""
    import tracemalloc

    big = 8 * 1024 * 1024  # 8 MiB single boundary chunk crossing a 1 KiB cap

    class _FakeResp:
        async def aiter_bytes(self):
            yield b"a" * 512
            yield b"b" * big

    resp = _FakeResp()
    tracemalloc.start()
    try:
        out = await _read_capped(resp, max_bytes=1024)  # type: ignore[arg-type]
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert len(out.encode()) <= 1024 + 64  # retained output stays at the cap (+marker)
    assert "truncated" in out
    # one transient ~8 MiB yielded chunk is unavoidable; the bug RETAINED it in the list
    # and re-joined (~2 copies ≈ 16 MiB). The fix keeps peak near a single copy.
    assert peak < big * 3 // 2, f"peak {peak} implies the boundary chunk was buffered + re-joined"


@pytest.mark.asyncio
async def test_known_oversized_content_length_rejected_before_read() -> None:
    """cca review #8: a declared Content-Length over the budget is rejected without
    reading the body."""

    def handler(req: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, content=b"x" * 10, headers={"content-length": "999999999"})

    res = await http_call(
        "https://api.datarc.cn",
        _op("getMetric"),
        {"mid": "x", "tenant": "t"},
        {},
        dns_resolver=_resolver(_PUBLIC),
        transport=httpx2.MockTransport(handler),
        max_bytes=1024,
    )
    assert res["ok"] is False
    assert res["error_kind"] == "too_large"


@pytest.mark.unit
def test_scrub_redacts_credentials() -> None:
    assert "secret123" not in _scrub("Authorization: Bearer secret123")
    assert "sk-abcdefghij" not in _scrub("key is sk-abcdefghij")
    assert "[redacted]" in _scrub("api_key=topsecret&x=1")
    assert _scrub("https://u:p@host/x").startswith("https://[redacted]@")
    # base64url JWT (cca review #1: \S+ catches + / =)
    assert "eyJ.aaa+bbb/ccc=" not in _scrub("Bearer eyJ.aaa+bbb/ccc=")
    # Basic auth b64 token (re-review #1): the keyword-pattern stopped at the space
    # before the b64, leaking "dXNlcjpwYXNz" (=user:pass). A reflected Authorization
    # header in an upstream body must not hand the b64 cred to the LLM.
    assert "dXNlcjpwYXNz" not in _scrub("Authorization: Basic dXNlcjpwYXNz")
    assert "dXNlcjpwYXNz" not in _scrub('{"echoed": "Authorization: Basic dXNlcjpwYXNz"}')
    # dashed key formats (re-review #sk-dashed): sk-proj-… / sk-ant-… — the {8,} alnum
    # run stopped at the first dash, leaving the real (modern OpenAI/Anthropic) key visible.
    # Neutral wrappers (no keyword nearby) so only the sk- pattern can redact these.
    assert "proj-abc123def456ghi" not in _scrub("leaked sk-proj-abc123def456ghi here")
    assert "ant-api03-XYZ12345abcd" not in _scrub("(sk-ant-api03-XYZ12345abcd)")


@pytest.mark.unit
def test_trim_truncates_oversized_body() -> None:
    big = "a" * 5000
    out = _trim(big, max_bytes=100)
    assert len(out.encode()) < 5000
    assert "truncated" in out
    assert _trim("short", max_bytes=100) == "short"


@pytest.mark.asyncio
async def test_ssrf_get_text_fetches_public_capped() -> None:
    # create-time spec fetch: SSRF-guarded GET of a user-supplied URL, connect-by-IP.
    def handler(req: httpx2.Request) -> httpx2.Response:
        assert req.headers.get("host") == "api.datarc.cn"  # Host restored to original
        return httpx2.Response(200, text='{"openapi": "3.0.0"}')

    out = await ssrf_get_text("https://api.datarc.cn/openapi.json", dns_resolver=_resolver(_PUBLIC), transport=httpx2.MockTransport(handler))
    assert out == '{"openapi": "3.0.0"}'


@pytest.mark.asyncio
async def test_ssrf_get_text_blocks_internal_url(saas_mode: None) -> None:
    def handler(req: httpx2.Request) -> httpx2.Response:  # pragma: no cover — must not run
        raise AssertionError("SSRF must block before any request is sent")

    with pytest.raises(SsrfBlocked):
        await ssrf_get_text("https://evil.example/openapi.json", dns_resolver=_resolver(_INTERNAL), transport=httpx2.MockTransport(handler))
