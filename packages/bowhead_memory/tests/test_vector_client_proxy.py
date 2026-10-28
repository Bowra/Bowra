"""embed-via-openapi — VectorClient datarc-proxy 模式 + EmbedProxyClientFactory 单测.

Simulate the Datarc `/openapi/v1/embed/` proxy with httpx2.MockTransport, without live services:
- POST 信封 / GET 形式（health、list-collections 上游只收 GET）
- `{errcode, errmsg, data}` 解壳（与直连同 shape）
- 403（连接密钥未开 embed 权限，FastAPI 风格 `{"detail"}`）→ VectorDBError(status=403)
- errcode != 200（如上游 405 被包成 errcode=400）→ VectorDBError
- 只读 guard：写操作不发 HTTP 直接抛
- factory：per-connection 池化 + capability 探测（正缓存永久 / 负缓存 TTL）
"""

from __future__ import annotations

import json

import httpx2
import pytest
from bowhead_memory.vector_client import (
    EmbedProxyClientFactory,
    VectorClient,
    VectorDBError,
)

_BASE = "http://dx.test"
_PROXY = "/openapi/v1/embed/"


def _swap_transport(vc: VectorClient, handler) -> VectorClient:  # type: ignore[no-untyped-def]
    """Replace the internal HTTPX2 client with MockTransport, following test_vector_client.

    注意保留 base_url + Bearer header；ssrf_guard transport 也被一并替掉（测试不解析 DNS）。
    """
    vc._http = httpx2.AsyncClient(  # type: ignore[attr-defined]
        base_url=_BASE,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {vc.access_token}",
        },
        transport=httpx2.MockTransport(handler),
    )
    return vc


def _proxy_client(handler) -> VectorClient:  # type: ignore[no-untyped-def]
    vc = VectorClient(base_url=_BASE, access_token="conn-key", datarc_proxy=True)
    return _swap_transport(vc, handler)


def _shell(data: dict) -> dict:
    return {"errcode": 200, "errmsg": "success", "data": data}


# ─── Protocol shape ───────────────────────────────────────────


async def test_proxy_query_documents_post_envelope() -> None:
    captured: dict = {}

    def h(req: httpx2.Request) -> httpx2.Response:
        captured["method"] = req.method
        captured["path"] = req.url.path
        captured["body"] = json.loads(req.content)
        captured["auth"] = req.headers.get("authorization")
        return httpx2.Response(
            200,
            json=_shell(
                {
                    "status": "success",
                    "results": {
                        "ids": [["1"]],
                        "distances": [[0.12]],
                        "documents": [["门店"]],
                        "metadatas": [[{"prop": "d57"}]],
                    },
                }
            ),
        )

    vc = _proxy_client(h)
    try:
        res = await vc.query(collection="v1_demo_p1", query_texts=["店"], n_results=3)
    finally:
        await vc.aclose()
    assert captured["method"] == "POST"
    assert captured["path"] == _PROXY
    assert captured["auth"] == "Bearer conn-key"
    # Envelope: operation name in uri, original body in data.
    assert captured["body"]["uri"] == "query-documents"
    assert captured["body"]["data"]["collection"] == "v1_demo_p1"
    assert captured["body"]["data"]["query_texts"] == ["店"]
    # After unwrapping, the shape matches direct access: query() removes the outer "results" as usual.
    assert res["documents"] == [["门店"]]


async def test_proxy_health_and_list_collections_use_get() -> None:
    calls: list[tuple[str, str, dict]] = []

    def h(req: httpx2.Request) -> httpx2.Response:
        params = dict(req.url.params)
        calls.append((req.method, req.url.path, params))
        if params.get("uri") == "health":
            return httpx2.Response(200, json=_shell({"status": "healthy", "version": "26.5.2"}))
        return httpx2.Response(
            200,
            json=_shell(
                {
                    "status": "success",
                    "collections": [{"name": "v1_demo_p1", "metadata": {}}],
                }
            ),
        )

    vc = _proxy_client(h)
    try:
        health = await vc.health()
        colls = await vc.list_collections(q="v1_demo")
    finally:
        await vc.aclose()
    # Upstream health / list-collections accept only GET; the proxy forwards method (POST receives upstream 405).
    assert calls[0][0] == "GET" and calls[0][2] == {"uri": "health"}
    assert calls[1][0] == "GET" and calls[1][2] == {"uri": "list-collections", "q": "v1_demo"}
    assert health["status"] == "healthy"
    assert colls[0]["name"] == "v1_demo_p1"


# ─── Error mapping ────────────────────────────────────────────


async def test_proxy_403_maps_to_permission_error() -> None:
    def h(_req: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(403, json={"detail": "无当前接口权限"})

    vc = _proxy_client(h)
    try:
        with pytest.raises(VectorDBError) as ei:
            await vc.health()
    finally:
        await vc.aclose()
    assert ei.value.status == 403
    assert "embed" in str(ei.value) and "permission" in str(ei.value)


async def test_proxy_errcode_wrap_raises() -> None:
    # Observed: POST health → the proxy wraps upstream 405 as errcode=400 "Method Not Allowed".
    def h(_req: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200,
            json={"errcode": 400, "errmsg": '{"detail":"Method Not Allowed"}'},
        )

    vc = _proxy_client(h)
    try:
        with pytest.raises(VectorDBError) as ei:
            await vc.query(collection="c", query_texts=["x"])
    finally:
        await vc.aclose()
    assert "errcode=400" in str(ei.value)


async def test_proxy_errmsg_dict_coerced() -> None:
    # datarc occasionally returns a dict errmsg (the same issue as bi_bridge); do not raise AttributeError.
    def h(_req: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200,
            json={"errcode": 500, "errmsg": {"detail": "boom"}},
        )

    vc = _proxy_client(h)
    try:
        with pytest.raises(VectorDBError) as ei:
            await vc.health()
    finally:
        await vc.aclose()
    assert "errcode=500" in str(ei.value)


# ─── Read-only guard ──────────────────────────────────────────


async def test_proxy_blocks_write_ops_without_http() -> None:
    hits: list[str] = []

    def h(req: httpx2.Request) -> httpx2.Response:
        hits.append(req.url.path)
        return httpx2.Response(200, json=_shell({}))

    vc = _proxy_client(h)
    try:
        with pytest.raises(VectorDBError, match="read-only"):
            await vc.create_collection("c")
        with pytest.raises(VectorDBError, match="read-only"):
            await vc.delete_collection("c")
        with pytest.raises(VectorDBError, match="read-only"):
            await vc.rename_collection("a", "b")
        with pytest.raises(VectorDBError, match="read-only"):
            await vc.add_documents(collection="c", documents=["d"], metadatas=[{}], ids=["1"])
        with pytest.raises(VectorDBError, match="read-only"):
            await vc.delete_documents(collection="c", ids=["1"])
        with pytest.raises(VectorDBError, match="read-only"):
            await vc.upsert_document(collection="c", doc_id="1", document="d")
    finally:
        await vc.aclose()
    assert hits == []  # Block writes before sending HTTP requests.


async def test_proxy_requires_access_token() -> None:
    with pytest.raises(VectorDBError, match="access_token"):
        VectorClient(base_url=_BASE, datarc_proxy=True)


# ─── Factory: pooling + capability detection ──────────────────


def test_factory_pools_per_connection() -> None:
    f = EmbedProxyClientFactory()
    a1 = f.get_client("https://a.datarc.cn", "key-a")
    a2 = f.get_client("https://a.datarc.cn", "key-a")
    b = f.get_client("https://a.datarc.cn", "key-B")
    assert a1 is a2
    assert a1 is not b
    assert f.size() == 2
    assert a1.datarc_proxy is True and a1.ssrf_guard is True


async def test_factory_verified_client_probe_ok_cached() -> None:
    probe_count = 0

    def h(req: httpx2.Request) -> httpx2.Response:
        nonlocal probe_count
        if dict(req.url.params).get("uri") == "health":
            probe_count += 1
        return httpx2.Response(200, json=_shell({"status": "healthy"}))

    f = EmbedProxyClientFactory()
    _swap_transport(f.get_client(_BASE, "k"), h)  # Warm the pooled instance and replace its transport.
    try:
        c1 = await f.get_verified_client(_BASE, "k")
        c2 = await f.get_verified_client(_BASE, "k")
    finally:
        await f.aclose()
    assert c1 is not None and c1 is c2
    assert probe_count == 1  # Positive cache: do not probe again on the second call.


async def test_factory_verified_client_probe_403_negative_cached() -> None:
    probe_count = 0

    def h(_req: httpx2.Request) -> httpx2.Response:
        nonlocal probe_count
        probe_count += 1
        return httpx2.Response(403, json={"detail": "无当前接口权限"})

    f = EmbedProxyClientFactory()  # Default negative-cache TTL is 300s.
    _swap_transport(f.get_client(_BASE, "k"), h)
    try:
        assert await f.get_verified_client(_BASE, "k") is None
        assert await f.get_verified_client(_BASE, "k") is None
    finally:
        await f.aclose()
    assert probe_count == 1  # Do not retry within the negative-cache window.


async def test_factory_negative_ttl_zero_retries() -> None:
    calls = 0

    def h(_req: httpx2.Request) -> httpx2.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx2.Response(403, json={"detail": "无当前接口权限"})
        return httpx2.Response(200, json=_shell({"status": "healthy"}))

    f = EmbedProxyClientFactory(probe_neg_ttl_sec=0.0)  # TTL=0 → retry immediately (self-repair path).
    _swap_transport(f.get_client(_BASE, "k"), h)
    try:
        assert await f.get_verified_client(_BASE, "k") is None
        assert await f.get_verified_client(_BASE, "k") is not None  # Recover after permission is granted.
    finally:
        await f.aclose()
    assert calls == 2
