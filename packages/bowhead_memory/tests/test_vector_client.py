"""VectorClient — 小范围单测.

Use httpx2.MockTransport to simulate Datarc Embedding Service without a live vector database;
覆盖：health / collections / add/query/delete 的序列化与错误路径。
"""

from __future__ import annotations

import json

import httpx2
import pytest
from bowhead_memory.vector_client import VectorClient, VectorDBError


def _mock_transport(handler) -> httpx2.MockTransport:  # type: ignore[no-untyped-def]
    return httpx2.MockTransport(handler)


async def _make(handler) -> VectorClient:  # type: ignore[no-untyped-def]
    vc = VectorClient(base_url="http://vdb.test/", access_token="tok")
    await vc._http.aclose()
    vc._http = httpx2.AsyncClient(  # type: ignore[attr-defined]
        base_url="http://vdb.test/",
        headers={"Content-Type": "application/json", "Authorization": "Bearer tok"},
        transport=_mock_transport(handler),
    )
    return vc


async def test_health_ok() -> None:
    def h(req: httpx2.Request) -> httpx2.Response:
        assert req.url.path == "/health"
        assert req.headers.get("authorization") == "Bearer tok"
        return httpx2.Response(200, json={"status": "ok"})

    vc = await _make(h)
    try:
        out = await vc.health()
        assert out["status"] == "ok"
    finally:
        await vc.aclose()


async def test_query_serializes_body() -> None:
    captured: dict = {}

    def h(req: httpx2.Request) -> httpx2.Response:
        assert req.url.path == "/query-documents"
        captured["body"] = json.loads(req.content)
        return httpx2.Response(
            200,
            json={
                "results": {
                    "ids": [["a", "b"]],
                    "distances": [[0.1, 0.2]],
                    "documents": [["doc1", "doc2"]],
                    "metadatas": [[{}, {}]],
                }
            },
        )

    vc = await _make(h)
    try:
        res = await vc.query(
            collection="c1",
            query_texts=["北京"],
            n_results=5,
            where={"type": "dim"},
        )
        assert captured["body"]["collection"] == "c1"
        assert captured["body"]["query_texts"] == ["北京"]
        assert captured["body"]["n_results"] == 5
        assert captured["body"]["where"] == {"type": "dim"}
        assert res["ids"] == [["a", "b"]]
    finally:
        await vc.aclose()


async def test_http_error_raises_vectordb_error() -> None:
    def h(req: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(500, text="server exploded")

    vc = await _make(h)
    try:
        with pytest.raises(VectorDBError) as excinfo:
            await vc.health()
        assert excinfo.value.status == 500
        assert "server exploded" in (excinfo.value.body or "")
    finally:
        await vc.aclose()


async def test_delete_requires_selector() -> None:
    def h(req: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json={})

    vc = await _make(h)
    try:
        with pytest.raises(VectorDBError, match="requires at least one"):
            await vc.delete_documents(collection="c1")
    finally:
        await vc.aclose()


async def test_ensure_collection_is_idempotent() -> None:
    calls: list[str] = []

    def h(req: httpx2.Request) -> httpx2.Response:
        calls.append(req.url.path)
        if req.url.path == "/get-collection":
            # Absent on the first call; present on the second.
            if calls.count("/get-collection") == 1:
                return httpx2.Response(200, json={"collection": None})
            return httpx2.Response(200, json={"collection": {"name": "c1"}})
        if req.url.path == "/create-collection":
            return httpx2.Response(200, json={"collection": {"name": "c1"}})
        return httpx2.Response(404)

    vc = await _make(h)
    try:
        first = await vc.ensure_collection("c1")
        second = await vc.ensure_collection("c1")
        assert first["name"] == "c1"
        assert second["name"] == "c1"
        # First call: get + create; second call: get only.
        assert calls.count("/create-collection") == 1
        assert calls.count("/get-collection") == 2
    finally:
        await vc.aclose()
