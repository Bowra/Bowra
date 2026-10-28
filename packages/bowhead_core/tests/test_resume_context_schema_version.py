"""resume_context 快照的 schema 版本号（Agent SDK 调研 A3）。

`resume_context` 是 Lead 在 sentinel 处写下的自由字典，可能**跨一次发版**才被读回来
重建 TurnRequest。此前它没有任何版本标记：快照形状一改，旧行会静默按新读法重建，
缺的字段变默认值、漂移的字段被当新语义用，全程没有一处能发现。

这是整个 SDK 调研里唯一一条「LangGraph 有、我们真缺」的东西（RunState 版本化）——
偷意图不引依赖：写侧在存储咽喉点盖版本号，读侧按版本决定认不认。

兼容立场是**故意不对称**的，两个方向都钉住：
  · 旧行无版本键 → v0，照收（存量行不能因为加了版本号就集体不可恢复）
  · 版本比本进程新 → 拒绝（回滚后旧进程读到新快照，正是静默错重建最危险的场景）
"""

from __future__ import annotations

import json

import pytest
from bowhead_core.clarification_store import (
    RESUME_CONTEXT_SCHEMA_VERSION,
    RESUME_CONTEXT_VERSION_KEY,
    ClarificationStore,
    resume_context_is_readable,
    resume_context_version,
    stamp_resume_context,
)


def test_stamp_preserves_ordinary_format_and_versions_bounded_questions() -> None:
    out = stamp_resume_context({"user_text": "q"})
    assert out == {"user_text": "q", RESUME_CONTEXT_VERSION_KEY: 1}
    assert stamp_resume_context({"question_round": {}})[RESUME_CONTEXT_VERSION_KEY] == RESUME_CONTEXT_SCHEMA_VERSION


def test_stamp_does_not_clobber_explicit_version() -> None:
    out = stamp_resume_context({"user_text": "q", RESUME_CONTEXT_VERSION_KEY: 99})
    assert out[RESUME_CONTEXT_VERSION_KEY] == 99


def test_stamp_passes_through_none() -> None:
    assert stamp_resume_context(None) is None


def test_legacy_row_without_version_is_v0_and_readable() -> None:
    """加版本号**之前**写的存量行必须照常恢复——否则等于把在途 HITL 全废掉。"""
    legacy = {"user_text": "q", "session_id": "s1"}
    assert resume_context_version(legacy) == 0
    assert resume_context_is_readable(legacy) is True


def test_future_version_is_refused() -> None:
    """回滚场景：新版进程写的 v99 被旧版进程读到 → 宁可不恢复也不能错重建。"""
    future = {"user_text": "q", RESUME_CONTEXT_VERSION_KEY: RESUME_CONTEXT_SCHEMA_VERSION + 1}
    assert resume_context_is_readable(future) is False


def test_current_version_is_readable() -> None:
    assert resume_context_is_readable(stamp_resume_context({"user_text": "q"})) is True


@pytest.mark.parametrize("version", [0, 1, 2])
def test_supported_explicit_versions_remain_readable(version: int) -> None:
    context = {"__schema_version": version}
    assert resume_context_version(context) == version
    assert resume_context_is_readable(context)


@pytest.mark.parametrize("version", ["1", True, False, None, -1, 1.0, 1.5, {}, []])
def test_malformed_version_is_not_legacy(version: object) -> None:
    context = {"__schema_version": version}
    assert resume_context_version(context) is None
    assert not resume_context_is_readable(context)


# ── Shared boundary: stamp both write paths (patching individual sites inevitably misses the fifth) ──


async def test_put_stamps_version_on_disk(tmp_path) -> None:
    store = ClarificationStore(db_path=str(tmp_path / "c.db"))
    await store.initialize()
    req = await store.put(
        turn_id="t1",
        user_id="u1",
        workspace_id="w1",
        token="",
        rationale="r",
        candidates=[],
        resume_context={"user_text": "q", "session_id": "s1"},
    )
    got = await store.get(req.request_id)
    assert got is not None
    assert got.resume_context[RESUME_CONTEXT_VERSION_KEY] == 1
    assert got.resume_context["user_text"] == "q"


async def test_set_resume_context_stamps_version_on_disk(tmp_path) -> None:
    store = ClarificationStore(db_path=str(tmp_path / "c2.db"))
    await store.initialize()
    req = await store.put(
        turn_id="t2",
        user_id="u1",
        workspace_id="w1",
        token="",
        rationale="r",
        candidates=[],
    )
    assert await store.set_resume_context(req.request_id, {"user_text": "q2", "session_id": "s2"})
    got = await store.get(req.request_id)
    assert got is not None
    assert got.resume_context[RESUME_CONTEXT_VERSION_KEY] == 1


async def test_put_without_resume_context_writes_null(tmp_path) -> None:
    """没有快照的卡不该被盖出一个空壳 {} —— 会把「不可恢复」变成「可恢复但没内容」。"""
    store = ClarificationStore(db_path=str(tmp_path / "c3.db"))
    await store.initialize()
    req = await store.put(
        turn_id="t3",
        user_id="u1",
        workspace_id="w1",
        token="",
        rationale="r",
        candidates=[],
    )
    got = await store.get(req.request_id)
    assert got is not None
    assert got.resume_context is None


def test_version_key_survives_json_round_trip() -> None:
    """键名以 __ 开头，确认没有哪层 JSON 处理会把它当私有字段丢掉。"""
    stamped = stamp_resume_context({"user_text": "q"})
    assert resume_context_version(json.loads(json.dumps(stamped))) == 1
