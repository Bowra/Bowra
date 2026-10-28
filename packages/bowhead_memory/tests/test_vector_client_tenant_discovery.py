"""Resolve collection tenants from the authenticated connection.

A shared vector service can contain identical project IDs for several tenants.
Discovery must use workspaces visible to the current credentials and must fail
on ambiguity instead of reading another tenant's semantic dictionary.
"""

from __future__ import annotations

import httpx2
import pytest
from bowhead_memory.vector_client import (
    EmbedProxyClientFactory,
    VectorClient,
    collection_names,
    parse_schema_collection,
    resolve_tenant_from_collections,
)

_BASE = "http://dx.test"
_PROXY = "/openapi/v1/embed/"

# Minimal collection fixtures cover project ID collisions across two tenants.
_LIVE_COLLECTIONS = [
    {"name": "v1_dev_p1", "metadata": {"created_at": "2026-02-08 12:04:16"}},
    {"name": "v1_dev_p1_keywords", "metadata": {}},
    {"name": "v1_dev_p1_sop", "metadata": {}},
    {"name": "v1_dev_p5805", "metadata": {}},
    {"name": "v1_dev_p13796", "metadata": {}},
    {"name": "v1_rc_p1", "metadata": {}},
    {"name": "v1_rc_p933", "metadata": {}},
]


# ─── Pure decision criteria ───────────────────────────────────


def test_parse_only_matches_schema_collection_itself() -> None:
    assert parse_schema_collection("v1_dev_p1") == ("dev", "1")
    # Exclude subcollections: repeating the same tenant/project adds no evidence for the decision.
    assert parse_schema_collection("v1_dev_p1_keywords") is None
    assert parse_schema_collection("v1_dev_p1_sop") is None
    assert parse_schema_collection("") is None
    assert parse_schema_collection("chroma_internal") is None


def test_collection_names_accepts_both_shapes() -> None:
    """代理返 [{name, metadata}]；直连老接口历史上返过裸字符串数组。"""
    assert collection_names(_LIVE_COLLECTIONS)[:2] == ["v1_dev_p1", "v1_dev_p1_keywords"]
    assert collection_names(["v1_dev_p1", None, 7, ""]) == ["v1_dev_p1"]
    assert collection_names([]) == []


def test_resolves_tenant_by_visible_workspace_coverage() -> None:
    """实测场景：密钥可见 {1, 5805}；dev 两个都有，rc 只有 1 → dev。"""
    names = collection_names(_LIVE_COLLECTIONS)
    assert resolve_tenant_from_collections(names, project_ids=["1", "5805"]) == "dev"


def test_refuses_to_guess_when_project_id_collides() -> None:
    """★ 只看 project 1 时 dev/rc 并列——必须判不出来。

    这里返回 tenant 才是真 bug：猜中一半概率读到另一个客户的词典。
    """
    names = collection_names(_LIVE_COLLECTIONS)
    assert resolve_tenant_from_collections(names, project_ids=["1"]) is None


def test_unique_candidate_resolves() -> None:
    """绝大多数私有化实例只有一个 client——单候选直接判定。"""
    assert resolve_tenant_from_collections(["v1_acme_p7", "v1_acme_p7_keywords"], project_ids=["7"]) == "acme"


def test_no_coverage_or_no_input_returns_none() -> None:
    assert resolve_tenant_from_collections(["v1_dev_p1"], project_ids=["999"]) is None
    assert resolve_tenant_from_collections(["v1_dev_p1"], project_ids=[]) is None
    assert resolve_tenant_from_collections([], project_ids=["1"]) is None


def test_tenant_segment_may_contain_underscores() -> None:
    assert resolve_tenant_from_collections(["v1_acme_prod_p12"], project_ids=["12"]) == "acme_prod"


# ─── Factory wiring ───────────────────────────────────────────


def _swap_transport(vc: VectorClient, handler) -> None:  # type: ignore[no-untyped-def]
    vc._http = httpx2.AsyncClient(  # type: ignore[attr-defined]
        base_url=_BASE,
        headers={"Authorization": f"Bearer {vc.access_token}"},
        transport=httpx2.MockTransport(handler),
    )


def _factory_with(handler) -> EmbedProxyClientFactory:  # type: ignore[no-untyped-def]
    """真 factory，但把它建出来的 client transport 换成 mock。"""
    f = EmbedProxyClientFactory()
    real_get = f.get_client

    def _patched(base_url: str, auth_key: str) -> VectorClient:
        client = real_get(base_url, auth_key)
        _swap_transport(client, handler)
        return client

    f.get_client = _patched  # type: ignore[method-assign]
    return f


def _handler(calls: list[str], collections: list[dict]):  # type: ignore[no-untyped-def]
    def h(req: httpx2.Request) -> httpx2.Response:
        uri = req.url.params.get("uri") or ""
        calls.append(uri)
        if uri == "health":
            return httpx2.Response(200, json={"errcode": 200, "errmsg": "ok", "data": {"status": "healthy"}})
        if uri == "list-collections":
            return httpx2.Response(
                200,
                json={"errcode": 200, "errmsg": "ok", "data": {"collections": collections}},
            )
        return httpx2.Response(200, json={"errcode": 400, "errmsg": "unexpected"})

    return h


async def test_verified_client_carries_discovered_tenant() -> None:
    calls: list[str] = []
    f = _factory_with(_handler(calls, _LIVE_COLLECTIONS))
    try:
        client = await f.get_verified_client(_BASE, "k", project_ids=["1", "5805"])
        assert client is not None
        assert client.datarc_tenant == "dev"
        assert calls == ["health", "list-collections"]
        # Cache the detected tenant; do not fetch the full list again.
        again = await f.get_verified_client(_BASE, "k", project_ids=["1", "5805"])
        assert again is client
        assert calls == ["health", "list-collections"]
    finally:
        await f.aclose()


async def test_ambiguous_tenant_leaves_client_unpinned() -> None:
    """★ 判不准时 client 照常返回（BI 不受影响），但 tenant 留空 → 上层降级报不可用。"""
    calls: list[str] = []
    f = _factory_with(_handler(calls, _LIVE_COLLECTIONS))
    try:
        client = await f.get_verified_client(_BASE, "k", project_ids=["1"])
        assert client is not None
        assert client.datarc_tenant is None
    finally:
        await f.aclose()


async def test_tenant_probe_failure_is_cooled_down() -> None:
    """list-collections 挂了不该每个 turn 重试一次。"""
    calls: list[str] = []

    def h(req: httpx2.Request) -> httpx2.Response:
        uri = req.url.params.get("uri") or ""
        calls.append(uri)
        if uri == "health":
            return httpx2.Response(200, json={"errcode": 200, "errmsg": "ok", "data": {"status": "healthy"}})
        return httpx2.Response(500, text="boom")

    f = _factory_with(h)
    try:
        assert (await f.get_verified_client(_BASE, "k", project_ids=["1"])) is not None
        assert (await f.get_verified_client(_BASE, "k", project_ids=["1"])) is not None
        assert calls.count("list-collections") == 1
    finally:
        await f.aclose()


async def test_larger_project_set_may_retry_despite_cooldown() -> None:
    """★ pin 通道只带 1 个 project（撞号判不出）；随后 web turn 带全量可及集合过来，
    信息变多，本该能判出——不能被上一次的负缓存挡住。"""
    calls: list[str] = []
    f = _factory_with(_handler(calls, _LIVE_COLLECTIONS))
    try:
        c1 = await f.get_verified_client(_BASE, "k", project_ids=["1"])
        assert c1 is not None and c1.datarc_tenant is None
        c2 = await f.get_verified_client(_BASE, "k", project_ids=["1", "5805"])
        assert c2 is c1 and c2.datarc_tenant == "dev"
        assert calls.count("list-collections") == 2
        # Conversely, no new information (the same set or a subset) must not trigger another fetch.
        before = calls.count("list-collections")
        await f.get_verified_client(_BASE, "k", project_ids=["1"])
        assert calls.count("list-collections") == before
    finally:
        await f.aclose()


async def test_same_project_set_is_cooled_down() -> None:
    """负对照：同一批 project 判不出来后不该每个 turn 重拉全量列表。"""
    calls: list[str] = []
    f = _factory_with(_handler(calls, _LIVE_COLLECTIONS))
    try:
        await f.get_verified_client(_BASE, "k", project_ids=["1"])
        await f.get_verified_client(_BASE, "k", project_ids=["1"])
        assert calls.count("list-collections") == 1
    finally:
        await f.aclose()


async def test_no_project_ids_skips_the_probe() -> None:
    """没有可及集合的通道（IM/scheduled）不白拉一次全量列表。"""
    calls: list[str] = []
    f = _factory_with(_handler(calls, _LIVE_COLLECTIONS))
    try:
        client = await f.get_verified_client(_BASE, "k")
        assert client is not None and client.datarc_tenant is None
        assert calls == ["health"]
    finally:
        await f.aclose()


# ─── SSRF policy must match BI (on-prem intranet / HTTP) ────────


def test_embed_client_reaches_intranet_http_datarc_in_local_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Local deployments support administrator-configured intranet HTTP connections.

    BI and embedding clients must share the deployment-level outbound policy.
    The private address below is a synthetic fixture and makes no network call.
    """
    from bowhead_router.ssrf_guard import validate_url

    monkeypatch.setenv("BOWHEAD_DEPLOYMENT_MODE", "local")
    url = "http://10.20.30.40:8000"
    assert validate_url(url).ips == ("10.20.30.40",)


def test_factory_client_goes_through_the_ssrf_guard() -> None:
    """工厂建出来的 client 必须真的走 guard（不是只在注释里写）。

    原用例还断言 allow_http / allow_private_network 两个字段为 True。那两个字段自
    22014fa9e 起就被 guard 丢弃，断言它们等于什么都没量 —— 这条用例当时正好活成了它
    docstring 里嘲讽的那种「只在注释里写」。内网可达性现在是部署模式的性质，由上面
    test_embed_client_reaches_intranet_http_datarc_in_local_mode 真正把关。
    """
    f = EmbedProxyClientFactory()
    c = f.get_client("http://10.20.30.40:8000", "k")
    assert c.ssrf_guard is True


def test_metadata_and_blocked_ports_are_refused_in_saas(monkeypatch: pytest.MonkeyPatch) -> None:
    """SaaS rejects internal addresses and restricted ports without opt-ins."""
    from bowhead_router.ssrf_guard import SsrfBlocked, validate_url

    monkeypatch.setenv("BOWHEAD_DEPLOYMENT_MODE", "saas")
    for url in ("http://169.254.169.254/latest/meta-data/", "http://10.20.30.40:22/"):
        try:
            validate_url(url)
        except SsrfBlocked:
            continue
        raise AssertionError(f"expected SsrfBlocked for {url}")
