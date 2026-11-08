"""Bounded representation reads share work without sharing caller authority."""

import asyncio
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import anyio
import httpx2
import pytest
from bowhead_core.artifact_store import ArtifactStore, LocalBackend
from bowhead_gateway.routes import files
from fastapi import FastAPI, HTTPException

from bowhead_gateway import artifact_representations as representations

PDF = b"%PDF-1.7\npreview"


@pytest.fixture
async def previews():
    coordinator = representations.PresentationPreviewCoordinator()
    try:
        yield coordinator
    finally:
        await coordinator.close()


async def test_overlapping_readers_share_conversion_after_the_first_reader_cancels(previews):
    started, release = anyio.Event(), anyio.Event()
    calls = []

    async def convert():
        calls.append("conversion")
        started.set()
        await release.wait()
        return PDF

    first = asyncio.create_task(previews.render("original", convert))
    await started.wait()
    second = asyncio.create_task(previews.render("original", convert))
    await anyio.lowlevel.checkpoint()
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert not second.done()
    release.set()
    assert await second == PDF
    assert calls == ["conversion"]
    assert await previews.render("original", convert) == PDF
    assert calls == ["conversion", "conversion"], "Completed results must not become a separate cache"


async def test_distinct_previews_wait_without_running_converters_in_parallel(previews):
    started, release = anyio.Event(), anyio.Event()
    order = []

    async def first_conversion():
        order.append("first started")
        started.set()
        await release.wait()
        order.append("first finished")
        return PDF

    async def next_conversion():
        order.append("next started")
        return PDF

    first = asyncio.create_task(previews.render("first", first_conversion))
    await started.wait()
    second = asyncio.create_task(previews.render("next", next_conversion))
    await anyio.lowlevel.checkpoint()
    assert order == ["first started"]
    release.set()
    assert await asyncio.gather(first, second) == [PDF, PDF]
    assert order == ["first started", "first finished", "next started"]


async def test_full_queue_still_admits_a_reader_of_existing_work(previews):
    started, release = anyio.Event(), anyio.Event()

    async def convert():
        started.set()
        await release.wait()
        return PDF

    first = asyncio.create_task(previews.render("active", convert))
    await started.wait()
    queued = [asyncio.create_task(previews.render(index, convert)) for index in range(3)]
    await anyio.lowlevel.checkpoint()
    with pytest.raises(HTTPException) as rejected:
        await previews.render("overflow", convert)
    assert rejected.value.status_code == 503
    assert rejected.value.detail == {"error": {"code": "artifact_representation_busy"}}
    assert rejected.value.headers == {"Cache-Control": "no-store", "Retry-After": "2"}
    joined = asyncio.create_task(previews.render("active", convert))
    release.set()
    assert await asyncio.gather(first, *queued, joined) == [PDF] * 5


async def test_queue_deadline_does_not_interrupt_the_active_conversion(previews, monkeypatch):
    started, release = anyio.Event(), anyio.Event()

    async def convert():
        started.set()
        await release.wait()
        return PDF

    first = asyncio.create_task(previews.render("active", convert))
    await started.wait()
    monkeypatch.setattr(representations, "_PRESENTATION_QUEUE_SECONDS", 0.05)
    with pytest.raises(HTTPException) as rejected:
        await previews.render("queued", convert)
    assert rejected.value.detail == {"error": {"code": "artifact_representation_busy"}}
    assert not first.done()
    release.set()
    assert await first == PDF
    assert await previews.render("queued", convert) == PDF


async def test_failed_shared_conversion_can_be_retried_without_retaining_the_failure(previews):
    started, release = anyio.Event(), anyio.Event()
    calls = []

    async def fail():
        calls.append("failed")
        started.set()
        await release.wait()
        raise ValueError("Synthetic conversion failure")

    first = asyncio.create_task(previews.render("same", fail))
    await started.wait()
    second = asyncio.create_task(previews.render("same", fail))
    await anyio.lowlevel.checkpoint()
    release.set()
    results = await asyncio.gather(first, second, return_exceptions=True)
    assert all(isinstance(result, ValueError) for result in results)
    assert calls == ["failed"]

    async def recovered():
        return PDF

    assert await previews.render("same", recovered) == PDF


async def test_shutdown_drains_active_and_queued_work_and_rejects_new_reads(previews):
    started = anyio.Event()
    queued_started = []

    async def active():
        started.set()
        await anyio.sleep_forever()

    async def queued():
        queued_started.append(True)
        return PDF

    first = asyncio.create_task(previews.render("active", active))
    await started.wait()
    second = asyncio.create_task(previews.render("queued", queued))
    await anyio.lowlevel.checkpoint()
    await previews.close()
    assert all(isinstance(result, asyncio.CancelledError) for result in await asyncio.gather(first, second, return_exceptions=True))
    assert queued_started == []
    with pytest.raises(HTTPException) as rejected:
        await previews.render("later", queued)
    assert rejected.value.detail == {"error": {"code": "artifact_representation_unavailable"}}


@pytest.fixture
async def presentation_store(tmp_path):
    store = ArtifactStore(db_path=tmp_path / "artifacts.db", backend=LocalBackend(root_dir=tmp_path / "blobs"))
    await store.initialize()
    art = await store.put_bytes(
        content=b"original bytes",
        filename="board.pptx",
        mime="application/vnd.openxmlformats-officedocument.presentationml.presentation",
        source="upload",
        created_by="owner",
    )
    return store, art


@pytest.mark.parametrize("ending", ["success", "revocation", "replacement", "unknown_identity", "runtime_change"])
async def test_original_route_shares_only_qualified_work_and_rechecks_each_reader(presentation_store, previews, monkeypatch, ending):
    store, art = presentation_store
    started, release, second_checked = anyio.Event(), anyio.Event(), anyio.Event()
    inputs = []
    renderer_urls = []

    async def convert(self, source):
        inputs.append(source)
        renderer_urls.append(self.url)
        started.set()
        await release.wait()
        return PDF

    original_head = store.backend.head

    async def head(path):
        result = await original_head(path)
        if ending == "unknown_identity" and result is not None:
            result = replace(result, identity=None, etag=None)
        if asyncio.current_task().get_name() == "second-preview":
            second_checked.set()
        return result

    monkeypatch.setattr(store.backend, "head", head)
    monkeypatch.setattr(representations.AuthoringExecutor, "view_presentation", convert)
    app = FastAPI()
    app.include_router(files.router, prefix="/api/v1/files")
    app.state.artifact_store = store
    app.state.settings = SimpleNamespace(dev_default_user="owner", auth_mode="dev")
    app.state.document_service = SimpleNamespace(executor=lambda: SimpleNamespace(root=store.backend.root_dir, url="http://executor", token="test"))
    app.state.presentation_previews = previews
    path = f"/api/v1/files/{art.id}/representations/pptx-pdf"
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://app") as client:
        first = asyncio.create_task(client.get(path))
        requests = [first]
        try:
            await started.wait()
            assert (await client.get(path, headers={"X-Bowhead-User-Id": "other"})).status_code == 404
            if ending == "runtime_change":
                app.state.document_service = SimpleNamespace(
                    executor=lambda: SimpleNamespace(root=store.backend.root_dir, url="http://other-executor", token="test")
                )
            second = asyncio.create_task(client.get(path), name="second-preview")
            requests.append(second)
            with anyio.fail_after(5):
                await second_checked.wait()
            if ending == "revocation":
                await store.soft_delete(art.id, user_id="owner")
            elif ending == "replacement":
                Path(art.path).write_bytes(b"replaced bytes")
            release.set()
            responses = await asyncio.gather(first, second)
            status = {"revocation": 404, "replacement": 503}.get(ending, 200)
            assert [response.status_code for response in responses] == [status, status]
            if status == 200:
                assert all(response.content == PDF for response in responses)
            else:
                assert all(response.content != PDF for response in responses)
            assert inputs == [b"original bytes"] * (2 if ending in {"unknown_identity", "runtime_change"} else 1)
            if ending == "runtime_change":
                assert renderer_urls == ["http://executor", "http://other-executor"]
            if ending == "replacement":
                assert (await client.get(path)).status_code == 200
                assert inputs == [b"original bytes", b"replaced bytes"]
        finally:
            release.set()
            await previews.close()
            await asyncio.gather(*requests, return_exceptions=True)


async def test_queued_source_replacement_is_refused_before_launching_its_converter(presentation_store, previews, monkeypatch):
    store, art = presentation_store
    other = await store.put_bytes(content=b"queued bytes", filename="other.pptx", mime=art.mime, source="upload", created_by="owner")
    started, release, queued_checked = anyio.Event(), anyio.Event(), anyio.Event()
    inputs = []
    original_head = store.backend.head

    async def head(path):
        result = await original_head(path)
        if asyncio.current_task().get_name() == "queued-preview":
            queued_checked.set()
        return result

    async def convert(self, source):
        inputs.append(source)
        started.set()
        await release.wait()
        return PDF

    monkeypatch.setattr(store.backend, "head", head)
    monkeypatch.setattr(representations.AuthoringExecutor, "view_presentation", convert)

    def executor():
        return SimpleNamespace(root=store.backend.root_dir, url="http://executor", token="test")

    first = asyncio.create_task(representations.build_presentation_view(store, art, executor, previews))
    await started.wait()
    second = asyncio.create_task(representations.build_presentation_view(store, other, executor, previews), name="queued-preview")
    try:
        with anyio.fail_after(5):
            await queued_checked.wait()
        Path(other.path).write_bytes(b"newest bytes")
        release.set()
        assert (await first).body == PDF
        with pytest.raises(HTTPException) as refused:
            await second
        assert refused.value.detail == {"error": {"code": "artifact_representation_unavailable"}}
        assert inputs == [b"original bytes"]
    finally:
        release.set()
        await previews.close()
        await asyncio.gather(first, second, return_exceptions=True)
