"""SSRF guard unit matrix — repo-wide single implementation (tech-design §11 / PRD §13).

No network: DNS injected. 2026-07 起 core/router 两份重复实现收敛到
`bowhead_router.ssrf_guard`，两边测试也合并到这一处；`bowhead_core.ssrf_guard`
只剩 re-export（兼容面由 core 侧 test_ssrf_guard_reexport.py 守）。
"""

from __future__ import annotations

import httpx2
import pytest
from bowhead_router.ssrf_guard import (
    SmtpPortBlocked,
    SsrfBlocked,
    SsrfGuardedAsyncTransport,
    validate_smtp_target,
    validate_url,
)


@pytest.fixture(autouse=True)
def _saas_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOWHEAD_DEPLOYMENT_MODE", "saas")


def _r(*ips):
    return lambda host: list(ips)


def test_public_https_ok():
    t = validate_url("https://api.datarc.cn/v1", resolver=_r("8.8.8.8"))
    assert t.host == "api.datarc.cn" and t.ip == "8.8.8.8" and t.scheme == "https"
    assert t.port == 443


@pytest.mark.parametrize(
    "ip",
    [
        "10.0.0.5",
        "172.16.0.1",
        "192.168.1.1",
        "127.0.0.1",
        "169.254.169.254",
        "0.0.0.0",
        "100.64.0.1",  # noqa: S104
        # multicast (224.0.0.0/4) — Python's is_global is True for these, so the
        # not-is_global base would let them through (re-review #multicast). 239.255.255.250
        # is the SSDP/UPnP group; 224.0.0.1 is all-hosts link-local.
        "224.0.0.1",
        "239.255.255.250",
    ],
)
def test_blocked_ipv4(ip):
    with pytest.raises(SsrfBlocked):
        validate_url("https://evil.example/x", resolver=_r(ip))


@pytest.mark.parametrize(
    "ip",
    [
        "::1",
        "fe80::1",
        "fc00::1",
        "::",
        "fd00::1234",
        "::ffff:169.254.169.254",
        "::ffff:10.0.0.1",  # IPv4-mapped must be unwrapped+blocked
        "ff02::1",
        "ff02::fb",  # IPv6 multicast (all-nodes / mDNS) — is_global True, must block
    ],
)
def test_blocked_ipv6_and_mapped(ip):
    with pytest.raises(SsrfBlocked):
        validate_url("https://evil.example/x", resolver=_r(ip))


def test_userinfo_blocked():
    with pytest.raises(SsrfBlocked):
        validate_url("https://user:pass@api.datarc.cn/", resolver=_r("203.0.113.10"))


@pytest.mark.parametrize(
    "url",
    [
        "http://api.datarc.cn/",  # http not allowed (no loopback)
        "file:///etc/passwd",
        "gopher://x/",
        "ftp://api.datarc.cn/",
    ],
)
def test_scheme_blocked(url):
    with pytest.raises(SsrfBlocked):
        validate_url(url, resolver=_r("203.0.113.10"))


@pytest.mark.parametrize("port", [22, 6379, 3306, 9200, 27017])
def test_dangerous_port_blocked(port):
    with pytest.raises(SsrfBlocked):
        validate_url(f"https://api.datarc.cn:{port}/", resolver=_r("203.0.113.10"))


def test_dns_rebind_multi_record_blocked():
    # public + internal in the same answer → any internal blocks the whole host.
    with pytest.raises(SsrfBlocked):
        validate_url("https://evil.example/", resolver=_r("8.8.8.8", "10.0.0.5"))


def test_ip_literal_internal_blocked_no_resolver():
    # literal metadata IP → fast path → blocked (no resolver needed)
    with pytest.raises(SsrfBlocked):
        validate_url("https://169.254.169.254/latest/meta-data/")


def test_ip_literal_public_ok_no_resolver():
    t = validate_url("https://8.8.8.8/v1")
    assert t.ip == "8.8.8.8"


def test_public_http_is_blocked_in_saas():
    with pytest.raises(SsrfBlocked):
        validate_url("http://api.datarc.cn/", resolver=_r("8.8.8.8"))


def test_private_network_is_blocked_in_saas():
    with pytest.raises(SsrfBlocked):
        validate_url("http://10.0.0.5/")


def test_local_mode_allows_private_http(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("BOWHEAD_DEPLOYMENT_MODE", "local")
    target = validate_url("http://10.0.0.5:8080/")
    assert target.scheme == "http" and target.ip == "10.0.0.5"


def test_loopback_denied_in_saas():
    # Formerly ..._without_optin: the opt-in (allow_loopback) was unused since 22014fa9e and
    # removed in 2026-09. saas always rejects loopback; no argument can enable it.
    with pytest.raises(SsrfBlocked):
        validate_url("https://localhost/", resolver=_r("127.0.0.1"))


def test_no_host():
    with pytest.raises(SsrfBlocked):
        validate_url("https:///nohost", resolver=_r("203.0.113.10"))


def test_malformed_port_error_does_not_echo_url_path_or_query():
    # re-review #url-echo: a malformed port raised SsrfBlocked(f"...{url!r}"), echoing the
    # whole URL incl. LLM-filled path/query — which may carry a (URL-encoded) secret that
    # _scrub can't catch. Error messages must echo only bounded fields (host), never the URL.
    with pytest.raises(SsrfBlocked) as ei:
        validate_url("https://api.datarc.cn:notaport/v1?api_key=topsecret123", resolver=_r("8.8.8.8"))
    msg = str(ei.value)
    assert "topsecret123" not in msg
    assert "api_key" not in msg


class _CaptureTransport(httpx2.AsyncBaseTransport):
    def __init__(self) -> None:
        self.request: httpx2.Request | None = None

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        self.request = request
        return httpx2.Response(200, json={"ok": True}, request=request)


@pytest.mark.asyncio
async def test_guarded_transport_pins_ip_and_restores_host_sni():
    capture = _CaptureTransport()
    transport = SsrfGuardedAsyncTransport(
        resolver=_r("8.8.8.8"),
        transport=capture,
    )
    async with httpx2.AsyncClient(transport=transport) as client:
        resp = await client.post("https://llm.example:8443/v1/chat?x=1", json={"hello": "world"})

    assert resp.status_code == 200
    assert capture.request is not None
    assert str(capture.request.url) == "https://8.8.8.8:8443/v1/chat?x=1"
    assert capture.request.headers["host"] == "llm.example:8443"
    assert capture.request.extensions["sni_hostname"] == "llm.example"


@pytest.mark.asyncio
async def test_guarded_transport_default_port_host_header():
    # default-port URL → Host header without port, URL rewritten to bare IP
    capture = _CaptureTransport()
    transport = SsrfGuardedAsyncTransport(resolver=_r("8.8.8.8"), transport=capture)
    async with httpx2.AsyncClient(transport=transport) as client:
        resp = await client.post("https://llm.example/v1/messages", json={"ok": True})
    assert resp.status_code == 200
    assert capture.request is not None
    assert str(capture.request.url) == "https://8.8.8.8/v1/messages"
    assert capture.request.headers["host"] == "llm.example"
    assert capture.request.extensions["sni_hostname"] == "llm.example"


def test_validate_url_keeps_all_validated_ips():
    # CDN host → several public IPs; keep all (dedup, resolution order). `.ip` = first.
    t = validate_url("https://api.datarc.cn/", resolver=_r("8.8.8.8", "1.1.1.1", "8.8.8.8"))
    assert t.ips == ("8.8.8.8", "1.1.1.1")
    assert t.ip == "8.8.8.8"


class _FailFirstTransport(httpx2.AsyncBaseTransport):
    """ConnectError on the first IP, 200 on any later — simulates a CDN whose first
    resolved IP refuses a direct-IP connect (the dashscope failure on 116)."""

    def __init__(self) -> None:
        self.attempts: list[str] = []

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        self.attempts.append(request.url.host)
        if len(self.attempts) == 1:
            raise httpx2.ConnectError("All connection attempts failed", request=request)
        return httpx2.Response(200, json={"ok": True}, request=request)


@pytest.mark.asyncio
async def test_guarded_transport_falls_back_across_validated_ips():
    inner = _FailFirstTransport()
    transport = SsrfGuardedAsyncTransport(resolver=_r("8.8.8.8", "1.1.1.1"), transport=inner)
    async with httpx2.AsyncClient(transport=transport) as client:
        resp = await client.post("https://llm.example/v1", json={"x": 1})
    assert resp.status_code == 200
    assert inner.attempts == ["8.8.8.8", "1.1.1.1"]  # tried first (failed), fell back to second


@pytest.mark.asyncio
async def test_guarded_transport_raises_when_all_ips_fail():
    class _AllFail(httpx2.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
            raise httpx2.ConnectError("nope", request=request)

    transport = SsrfGuardedAsyncTransport(resolver=_r("8.8.8.8", "1.1.1.1"), transport=_AllFail())
    async with httpx2.AsyncClient(transport=transport) as client:
        with pytest.raises(httpx2.ConnectError):
            await client.post("https://llm.example/v1", json={"x": 1})


@pytest.mark.asyncio
async def test_guarded_transport_does_not_retry_post_connect_errors():
    # Double-send guard: a POST-connect failure (body may already be on the wire) must
    # NOT fall back to the next IP — only connect-phase errors do. Inner transport must
    # be called exactly once despite two validated IPs.
    class _ReadErrorAfterConnect(httpx2.AsyncBaseTransport):
        def __init__(self) -> None:
            self.calls = 0

        async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
            self.calls += 1
            raise httpx2.ReadError("reset after send", request=request)

    inner = _ReadErrorAfterConnect()
    transport = SsrfGuardedAsyncTransport(resolver=_r("8.8.8.8", "1.1.1.1"), transport=inner)
    async with httpx2.AsyncClient(transport=transport) as client:
        with pytest.raises(httpx2.ReadError):
            await client.post("https://llm.example/v1", json={"x": 1})
    assert inner.calls == 1  # not retried


# ── SMTP targets (by deployment mode) ───────────────────────────────────


@pytest.mark.parametrize("port", [25, 465, 587, 2525])
def test_saas_smtp_public_host_on_standard_port_allowed(port):
    target = validate_smtp_target("smtp.example.com", port, resolver=_r("8.8.8.8", "1.1.1.1"))
    assert (target.host, target.port, target.ips) == ("smtp.example.com", port, ("8.8.8.8", "1.1.1.1"))


@pytest.mark.parametrize("ip", ["10.0.0.5", "127.0.0.1", "169.254.169.254", "::1", "100.64.0.1"])
def test_saas_smtp_non_public_host_blocked(ip):
    with pytest.raises(SsrfBlocked):
        validate_smtp_target("smtp.example.com", 587, resolver=_r(ip))
    with pytest.raises(SsrfBlocked):
        validate_smtp_target(ip, 587)


def test_saas_smtp_mixed_public_and_private_answers_blocked():
    with pytest.raises(SsrfBlocked):
        validate_smtp_target("smtp.example.com", 587, resolver=_r("8.8.8.8", "10.0.0.5"))


@pytest.mark.parametrize("port", [22, 80, 443, 2526, 8025])
def test_saas_smtp_nonstandard_port_blocked(port):
    with pytest.raises(SmtpPortBlocked):
        validate_smtp_target("smtp.example.com", port, resolver=_r("8.8.8.8"))


def test_saas_generic_url_policy_still_blocks_smtp_ports():
    with pytest.raises(SsrfBlocked):
        validate_url("https://smtp.example.com:587/", resolver=_r("8.8.8.8"))


def test_local_smtp_target_is_unrestricted(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("BOWHEAD_DEPLOYMENT_MODE", "local")

    def _never(host):
        raise AssertionError("local mode must not resolve")

    target = validate_smtp_target("mail.intranet", 2500, resolver=_never)
    assert (target.host, target.port, target.ips) == ("mail.intranet", 2500, ("mail.intranet",))
    assert validate_smtp_target("127.0.0.1", 25).ips == ("127.0.0.1",)
