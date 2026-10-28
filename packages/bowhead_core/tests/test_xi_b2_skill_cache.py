"""ξ B2 — skill.read per-turn cache wrapper.

Prod 10-turn audit: peer 单 turn 读 analyst-sop-output-contract 5 次 → 浪费.
B2 cache wrapper: same (skill_name, ref) within turn returns cached + marker.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from bowhead_core.skill_bridge import _wrap_skill_read_with_cache


async def _fake_real_handler_factory():
    """Return a handler that counts how many times it's actually called."""
    call_count = {"n": 0}

    async def _handler(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
        call_count["n"] += 1
        return {
            "ok": True,
            "name": args["name"],
            "content": f"<skill body for {args['name']}>",
        }

    return _handler, call_count


@pytest.mark.asyncio
async def test_b2_cache_hit_skips_real_handler() -> None:
    """Same (skill_name, ref) second call returns cached + _cached marker."""
    real_handler, call_count = await _fake_real_handler_factory()
    wrapped = _wrap_skill_read_with_cache(real_handler)
    ctx = SimpleNamespace()
    r1 = await wrapped({"name": "analyst-sop-output-contract"}, ctx)
    assert r1["ok"] is True
    assert call_count["n"] == 1
    # Second call: same name, should hit cache
    r2 = await wrapped({"name": "analyst-sop-output-contract"}, ctx)
    assert r2["ok"] is True
    assert r2.get("_cached") is True
    assert "本 turn 已读过" in r2.get("_cache_hint", "")
    assert call_count["n"] == 1  # real handler NOT called again


@pytest.mark.asyncio
async def test_b2_different_skill_no_cache_collision() -> None:
    """Different skill names cache independently."""
    real_handler, call_count = await _fake_real_handler_factory()
    wrapped = _wrap_skill_read_with_cache(real_handler)
    ctx = SimpleNamespace()
    await wrapped({"name": "analyst-framework"}, ctx)
    await wrapped({"name": "analyst-sop-diagnosis"}, ctx)
    assert call_count["n"] == 2  # both fresh reads
    # Re-read first: cached
    r3 = await wrapped({"name": "analyst-framework"}, ctx)
    assert r3.get("_cached") is True
    assert call_count["n"] == 2  # still 2 (cached hit)


@pytest.mark.asyncio
async def test_b2_file_reads_do_not_collide_with_manifest_or_each_other() -> None:
    async def read(args, ctx):
        return {"body": args.get("file", "SKILL.md")}

    wrapped = _wrap_skill_read_with_cache(read)
    ctx = SimpleNamespace()
    for path in (None, "scripts/first.py", "references/rules.json"):
        args = {"name": "recipe", **({"file": path} if path else {})}
        assert (await wrapped(args, ctx))["body"] == (path or "SKILL.md")
        assert (await wrapped(args, ctx))["_cached"] is True


@pytest.mark.asyncio
async def test_b2_ref_field_part_of_cache_key() -> None:
    """`ref` field partitions cache — different ref same name = different cache key."""
    real_handler, call_count = await _fake_real_handler_factory()
    wrapped = _wrap_skill_read_with_cache(real_handler)
    ctx = SimpleNamespace()
    await wrapped({"name": "analyst-sop-output-contract"}, ctx)
    await wrapped({"name": "analyst-sop-output-contract", "ref": "payload-schema"}, ctx)
    assert call_count["n"] == 2
    # Re-read same ref: cached
    await wrapped({"name": "analyst-sop-output-contract", "ref": "payload-schema"}, ctx)
    assert call_count["n"] == 2


@pytest.mark.asyncio
async def test_b2_args_field_part_of_cache_key() -> None:
    """`args` 参与 $ARGUMENTS 替换 → 必须进 key；同 name 换 args 不能返旧替换体（audit #16）."""
    real_handler, call_count = await _fake_real_handler_factory()
    wrapped = _wrap_skill_read_with_cache(real_handler)
    ctx = SimpleNamespace()
    await wrapped({"name": "analyst-framework", "args": "q1 revenue"}, ctx)
    r2 = await wrapped({"name": "analyst-framework", "args": "q2 margin"}, ctx)
    assert r2.get("_cached") is None
    assert call_count["n"] == 2
    # List-shaped args are distinguished too.
    await wrapped({"name": "analyst-framework", "args": ["a", "b"]}, ctx)
    assert call_count["n"] == 3
    # Reread with the same args: cache hit
    r4 = await wrapped({"name": "analyst-framework", "args": "q1 revenue"}, ctx)
    assert r4.get("_cached") is True
    assert call_count["n"] == 3


@pytest.mark.asyncio
async def test_b2_disable_via_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """BOWHEAD_SKILL_READ_CACHE=0 → bypass wrapper, no caching."""
    monkeypatch.setenv("BOWHEAD_SKILL_READ_CACHE", "0")
    real_handler, call_count = await _fake_real_handler_factory()
    wrapped = _wrap_skill_read_with_cache(real_handler)
    ctx = SimpleNamespace()
    await wrapped({"name": "x"}, ctx)
    await wrapped({"name": "x"}, ctx)
    # Both calls hit real handler — no caching
    assert call_count["n"] == 2


@pytest.mark.asyncio
async def test_b2_error_result_not_cached() -> None:
    """skill.read 返 error 不缓存 (失败可重试)."""
    call_count = {"n": 0}

    async def _failing_handler(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
        call_count["n"] += 1
        return {"ok": False, "error": "skill not found"}

    wrapped = _wrap_skill_read_with_cache(_failing_handler)
    ctx = SimpleNamespace()
    await wrapped({"name": "missing-skill"}, ctx)
    await wrapped({"name": "missing-skill"}, ctx)
    # Both call real (failure not cached)
    assert call_count["n"] == 2


@pytest.mark.asyncio
async def test_b2_missing_name_passthrough() -> None:
    """args 无 name → pass through to handler, no cache."""
    real_handler, _call_count = await _fake_real_handler_factory()
    wrapped = _wrap_skill_read_with_cache(real_handler)
    ctx = SimpleNamespace()
    # name missing → can't form cache key — call real handler directly
    # (real_handler will fail because it accesses args["name"], but wrapped
    # should not blow up before reaching handler)
    with pytest.raises(KeyError):
        await wrapped({}, ctx)


# ---------------------------------------------------------------------------
# skill-read-cache-2026-08-28: cache must survive copy.copy(ctx) in subagent dispatch.
#
# All seven cases above repeatedly use one SimpleNamespace with the wrapper, never modeling production dispatch,
# leaving the sole failure path (lazy setattr creates a dictionary only on the copy, unavailable to parent ctx)
# in a blind spot (tests pass; production subagent hit rate stays 0). Cases below use real ToolExecutionContext
# plus subagent.py should_swap's dispatch shape (dispatch = copy.copy(parent);
# dispatch.role = "analyst"). Retain old SimpleNamespace cases as regression for the stub fallback path.
# ---------------------------------------------------------------------------

import copy

from bowhead_core.tool_registry import ToolExecutionContext


def _real_parent_ctx() -> ToolExecutionContext:
    """真路径的 _peer_ctx 形状：role 非 None（paradigm v2 on ⇒ should_swap 成立）."""
    return ToolExecutionContext(
        workspace_id="ws_test",
        member_uid="m1",
        user_id="u1",
        turn_id="t1",
        datarc=None,
        role="analyst",
    )


@pytest.mark.asyncio
async def test_b2_cache_survives_subagent_dispatch_copy() -> None:
    """subagent 派发形态：每次调用前 copy.copy(parent)，第 2/3 次必须命中。

    修复前（wrapper lazy setattr）此用例红：字典每次建在新副本上，真 handler 调 3 次。
    """
    real_handler, call_count = await _fake_real_handler_factory()
    wrapped = _wrap_skill_read_with_cache(real_handler)
    parent = _real_parent_ctx()
    results = []
    for _ in range(3):
        # Mirror subagent.py `_execute_tool`'s should_swap branch (do not reuse the same copy).
        dispatch = copy.copy(parent)
        dispatch.role = "analyst"
        results.append(await wrapped({"name": "regional-sales-analysis"}, dispatch))
    assert call_count["n"] == 1, "第 2/3 次必须走缓存，真 handler 只许调 1 次"
    assert results[0].get("_cached") is None
    assert results[1].get("_cached") is True
    assert results[2].get("_cached") is True


@pytest.mark.asyncio
async def test_b2_dispatch_copy_write_flows_back_to_parent() -> None:
    """机制直证：经副本派发写入的缓存必须回流父 ctx（浅拷贝别名同一 dict）。

    只断言调用计数会漏掉「副本间偶然共享但父 ctx 拿不到」一类退化——这里直接钉别名。
    """
    real_handler, _ = await _fake_real_handler_factory()
    wrapped = _wrap_skill_read_with_cache(real_handler)
    parent = _real_parent_ctx()
    dispatch = copy.copy(parent)
    dispatch.role = "analyst"
    await wrapped({"name": "analyst-framework"}, dispatch)
    assert parent._ξ_skill_cache, "副本上的缓存写入没有回流到父 ctx"
    assert dispatch._ξ_skill_cache is parent._ξ_skill_cache


@pytest.mark.asyncio
async def test_b2_lead_path_no_copy_positive_control() -> None:
    """Lead 路径（running_profile=='lead' ⇒ 不拷贝）：原有行为不许被修复改坏。"""
    real_handler, call_count = await _fake_real_handler_factory()
    wrapped = _wrap_skill_read_with_cache(real_handler)
    ctx = _real_parent_ctx()  # Lead directly uses shared ctx, without copying.
    r1 = await wrapped({"name": "analyst-framework"}, ctx)
    r2 = await wrapped({"name": "analyst-framework"}, ctx)
    assert r1.get("_cached") is None
    assert r2.get("_cached") is True
    assert call_count["n"] == 1


@pytest.mark.asyncio
async def test_b2_save_overwrite_clears_same_turn_cache(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """R1：同 turn「读 → overwrite 保存 → 重读」必须拿到新版，不许命中 stale 缓存。

    走真 validator（.data 经 chdir 落 tmp）+ 真注册的 skill_save_generated handler；
    读侧 handler 每次返当次 body，靠 save 的 clear() 保证第 3 步是真读。
    """
    from bowhead_core.skill_save_tool import register_skill_save_tool
    from bowhead_core.tool_registry import ToolRegistry

    monkeypatch.chdir(tmp_path)  # Handler writes (Path(".data")/"user_skills").resolve()

    class _StoreStub:
        async def list_for_user(self, user_id: str):
            return []

        async def upsert(self, **kw):
            return None

        async def get(self, **kwargs):
            return None

    registry = ToolRegistry()
    register_skill_save_tool(registry)
    save_handler = registry.get("skill_save_generated").handler

    bodies = iter(["v1 body", "v2 body"])

    async def _read_handler(args, ctx):
        return {"ok": True, "name": args["name"], "content": next(bodies)}

    wrapped_read = _wrap_skill_read_with_cache(_read_handler)
    parent = _real_parent_ctx()
    parent.skill_instance_store = _StoreStub()

    # ① Read (populate cache)
    r1 = await wrapped_read({"name": "cache-inval-target"}, parent)
    assert r1["content"] == "v1 body"
    # ② Overwrite with new version via a subagent dispatch copy; clear must propagate through aliases to parent ctx.
    dispatch = copy.copy(parent)
    dispatch.role = "analyst"
    body_md = "---\nname: cache-inval-target\ndescription: d\nversion: 0.2.0\n---\nv2\n"
    saved = await save_handler({"body_md": body_md, "overwrite": True}, dispatch)
    assert saved.get("ok") is True, saved
    # ③ Reread: cache cleared ⇒ actual read returns new content, not stale v1.
    r3 = await wrapped_read({"name": "cache-inval-target"}, parent)
    assert r3.get("_cached") is None, "save 后缓存未失效——返了 stale"
    assert r3["content"] == "v2 body"


@pytest.mark.asyncio
async def test_b2_inflight_read_does_not_resurrect_stale_after_save(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """race guard：save clear 时仍在飞行中的读，落地后不得把 pre-clear 旧版写回缓存。

    真实形状：_read 先 resolve 到（旧）spec，其后还有 await 挂起点；save 在挂起期间
    完成 clear。无世代号比对时，旧版会在落地时塞回已失效的缓存——本 turn 后续全 stale。
    """
    import asyncio

    from bowhead_core.skill_save_tool import register_skill_save_tool
    from bowhead_core.tool_registry import ToolRegistry

    monkeypatch.chdir(tmp_path)

    class _StoreStub:
        async def list_for_user(self, user_id: str):
            return []

        async def upsert(self, **kw):
            return None

        async def get(self, **kwargs):
            return None

    registry = ToolRegistry()
    register_skill_save_tool(registry)
    save_handler = registry.get("skill_save_generated").handler

    gate = asyncio.Event()
    body = {"v": "v1 body"}

    async def _read_handler(args, ctx):
        snapshot = body["v"]  # ≈ _resolve: captures old spec before suspension
        await gate.wait()  # ≈ later _user_enabled awaits; save completes meanwhile
        return {"ok": True, "name": args["name"], "content": snapshot}

    wrapped_read = _wrap_skill_read_with_cache(_read_handler)
    parent = _real_parent_ctx()
    parent.skill_instance_store = _StoreStub()

    # ① Slow read starts (miss, resolved to v1, waiting on gate).
    inflight = asyncio.get_running_loop().create_task(wrapped_read({"name": "race-target"}, parent))
    await asyncio.sleep(0)
    # ② Save completes: clear + increment __gen__; disk now holds v2.
    body_md = "---\nname: race-target\ndescription: d\nversion: 0.2.0\n---\nv2\n"
    saved = await save_handler({"body_md": body_md, "overwrite": True}, parent)
    assert saved.get("ok") is True, saved
    body["v"] = "v2 body"
    # ③ Slow read completes: v1 may return to its own caller, but must not write back to cache.
    gate.set()
    r_slow = await inflight
    assert r_slow["content"] == "v1 body"
    # ④ Reread after save must actually return v2, never hit v1 revived by the slow read.
    r_after = await wrapped_read({"name": "race-target"}, parent)
    assert r_after.get("_cached") is None, "飞行中的旧读把 stale 写回了缓存——世代号守卫失效"
    assert r_after["content"] == "v2 body"
