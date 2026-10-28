"""Pending import must remain a live gate even after a successful cached read."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import anyio
import pytest
from bowhead_core.skill_bridge import _wrap_skill_execution_gate, _wrap_skill_read_with_cache, register_skill_tools
from bowhead_core.skill_instance_store import SkillInstanceStore
from bowhead_core.tool_registry import ToolRegistry

from bowhead_skill import SkillCatalog, SkillRuntime

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def _case(tmp_path: Path, source_type: str = "user_upload"):
    root = tmp_path / "private-method"
    root.mkdir()
    manifest = root / "SKILL.md"
    manifest.write_text("---\nname: private-method\ndescription: Financial method\n---\nORIGINAL_PRIVATE_BODY", encoding="utf-8")
    store = SkillInstanceStore(tmp_path / "skills.db")
    await store.initialize()
    await store.upsert(user_id="u_owner", name="private-method", source_type=source_type, source_path=str(root))
    ctx = SimpleNamespace(user_id="u_owner", team_id="t_one", skill_instance_store=store)
    return manifest, store, ctx


async def _status(store: SkillInstanceStore, status: str):
    assert await store.set_import_status(user_id="u_owner", name="private-method", import_status=status, import_report_json=None)


@pytest.mark.parametrize("cache_flag", ["0", "1"])
@pytest.mark.parametrize("runtime_flag", ["0", "1"])
@pytest.mark.parametrize("source_type", ["user_upload", "user_generated"])
async def test_registered_read_rechecks_pending_then_reloads_corrected_content(tmp_path, monkeypatch, cache_flag, runtime_flag, source_type):
    monkeypatch.setenv("BOWHEAD_SKILL_READ_CACHE", cache_flag)
    monkeypatch.setenv("BOWHEAD_SKILL_MULTIRUNTIME", runtime_flag)
    manifest, store, ctx = await _case(tmp_path, source_type)
    registry = ToolRegistry()
    register_skill_tools(registry, SkillCatalog(), SkillRuntime())
    read = registry.get("skill_read").handler
    args = {"name": "private-method"}
    first = await read(args, ctx)
    assert "ORIGINAL_PRIVATE_BODY" in str(first)
    second = await read(args, ctx)
    assert bool(second.get("_cached")) == (cache_flag == "1")
    await _status(store, "pending")
    denied = await read(args, ctx)
    assert denied["error_kind"] == "skill_import_pending"
    assert "ORIGINAL_PRIVATE_BODY" not in str(denied)
    assert (await store.get(user_id="u_owner", name="private-method")).enabled is True
    manifest.write_text(manifest.read_text().replace("ORIGINAL_PRIVATE_BODY", "CORRECTED_PRIVATE_BODY"))
    await _status(store, "cleared")
    fresh = await read(args, ctx)
    assert "CORRECTED_PRIVATE_BODY" in str(fresh)
    assert "ORIGINAL_PRIVATE_BODY" not in str(fresh)
    assert not fresh.get("_cached")


async def test_pending_denial_fences_inflight_cache_fill_across_shared_peer_contexts(tmp_path, monkeypatch):
    monkeypatch.setenv("BOWHEAD_SKILL_READ_CACHE", "1")
    _, store, ctx = await _case(tmp_path)
    ctx._ξ_skill_cache = {}
    peer = SimpleNamespace(**vars(ctx))
    began, finish = anyio.Event(), anyio.Event()
    reads = 0

    async def delayed_reader(args, owner):
        nonlocal reads
        reads += 1
        if reads == 1:
            began.set()
            await finish.wait()
            return {"ok": True, "content": "OLD_INFLIGHT_BODY"}
        return {"ok": True, "content": "CURRENT_BODY"}

    read = _wrap_skill_execution_gate("skill_read", _wrap_skill_read_with_cache(delayed_reader), SkillCatalog())
    async with anyio.create_task_group() as group:
        group.start_soon(read, {"name": "private-method"}, ctx)
        await began.wait()
        await _status(store, "pending")
        denial = await read({"name": "private-method"}, peer)
        await _status(store, "overridden")
        finish.set()
    assert denial["error_kind"] == "skill_import_pending"
    result = await read({"name": "private-method"}, ctx)
    assert result["content"] == "CURRENT_BODY"
    assert not result.get("_cached")
    sibling_result = await read({"name": "private-method"}, peer)
    assert sibling_result["content"] == "CURRENT_BODY"
    assert sibling_result["_cached"] is True
    assert reads == 2


async def test_disabled_skill_denial_drops_old_body_before_reenable(tmp_path, monkeypatch):
    monkeypatch.setenv("BOWHEAD_SKILL_READ_CACHE", "1")
    manifest, store, ctx = await _case(tmp_path)
    registry = ToolRegistry()
    register_skill_tools(registry, SkillCatalog(), SkillRuntime())
    read = registry.get("skill_read").handler
    args = {"name": "private-method"}
    assert "ORIGINAL_PRIVATE_BODY" in str(await read(args, ctx))
    assert await store.set_enabled(user_id="u_owner", name="private-method", enabled=False)
    denial = await read(args, ctx)
    assert denial["error_kind"] == "resource_revoked"
    manifest.write_text(manifest.read_text().replace("ORIGINAL_PRIVATE_BODY", "REENABLED_BODY"))
    assert await store.set_enabled(user_id="u_owner", name="private-method", enabled=True)
    result = await read(args, ctx)
    assert "REENABLED_BODY" in str(result)
    assert "ORIGINAL_PRIVATE_BODY" not in str(result)
    assert not result.get("_cached")
