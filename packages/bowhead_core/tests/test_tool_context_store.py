"""批2/c1 (workspace-space-split) — 用户级 tool 上下文存储契约.

PRD §3.1：kind='tool_context', key='{tool}/{slot}', value={id,name,source,updated_at}。
datarc 第一实例；形状通用（PBI 未来零 schema 变化）。
"""

from __future__ import annotations

import pytest
from bowhead_core.user_memory import UserMemoryStore


@pytest.fixture
async def store(tmp_path):
    s = UserMemoryStore(tmp_path / "mem.db")
    await s.initialize()
    return s


async def test_round_trip_datarc_workspace(store):
    await store.set_tool_context(
        "u_1",
        "datarc",
        context_id="42",
        context_name="零售集团",
        source="explicit",
    )
    ctx = await store.get_tool_context("u_1", "datarc")
    assert ctx is not None
    assert ctx["id"] == "42"
    assert ctx["name"] == "零售集团"
    assert ctx["source"] == "explicit"
    assert ctx["updated_at"]  # ISO timestamp is nonempty


async def test_missing_returns_none(store):
    assert await store.get_tool_context("u_nobody", "datarc") is None


async def test_upsert_overwrites(store):
    await store.set_tool_context("u_1", "datarc", context_id="42", source="auto")
    await store.set_tool_context(
        "u_1",
        "datarc",
        context_id="7",
        context_name="华东大区",
        source="switch",
    )
    ctx = await store.get_tool_context("u_1", "datarc")
    assert ctx["id"] == "7"
    assert ctx["source"] == "switch"


async def test_empty_context_id_is_noop(store):
    """空 id 不许清掉既有值（防误清——清偏好走 clear_tool_context 显式路径）."""
    await store.set_tool_context("u_1", "datarc", context_id="42")
    await store.set_tool_context("u_1", "datarc", context_id="")
    assert (await store.get_tool_context("u_1", "datarc"))["id"] == "42"


async def test_clear_then_get_none_then_set_revives(store):
    """PRD §2.1 step1: 撤权 → 清偏好；再选 → 同行复活（软删对偶 upsert）."""
    await store.set_tool_context("u_1", "datarc", context_id="42")
    await store.clear_tool_context("u_1", "datarc")
    assert await store.get_tool_context("u_1", "datarc") is None
    await store.set_tool_context("u_1", "datarc", context_id="7", source="explicit")
    assert (await store.get_tool_context("u_1", "datarc"))["id"] == "7"


async def test_isolation_across_users_and_tools(store):
    """user 键隔离 + tool 维度隔离（通用形状：powerbi 槽位与 datarc 互不干扰）."""
    await store.set_tool_context("u_a", "datarc", context_id="1")
    await store.set_tool_context("u_b", "datarc", context_id="2")
    await store.set_tool_context("u_a", "powerbi", context_id="guid-x", slot="workspace")
    assert (await store.get_tool_context("u_a", "datarc"))["id"] == "1"
    assert (await store.get_tool_context("u_b", "datarc"))["id"] == "2"
    assert (await store.get_tool_context("u_a", "powerbi"))["id"] == "guid-x"
    assert await store.get_tool_context("u_b", "powerbi") is None


# ─── BUG1 regression (caught in live production): session overrides no longer erase permanent defaults ───


async def test_session_switch_does_not_clobber_explicit_default(store):
    """真机 bug：用户勾记住设 explicit 默认，后用「换 XX」(source=session) →
    单 key 时代把 explicit 整行覆盖，新会话丢默认弹 picker。双 key 后应共存。"""
    # 1) Explicit permanent default (remember selected)
    await store.set_tool_context(
        "u_1",
        "datarc",
        context_id="1",
        context_name="商超零售场景",
        source="explicit",
    )
    # 2) Same user "换 XX" → session overlay (with session_id)
    await store.set_tool_context(
        "u_1",
        "datarc",
        context_id="142",
        context_name="银行客户",
        source="session",
        session_id="s_A",
    )
    # Explicit default remains (base key not overwritten)
    default = await store.get_tool_context("u_1", "datarc")
    assert default is not None and default["id"] == "1" and default["source"] == "explicit"
    # Session overlay stored separately (@session key)
    overlay = await store.get_tool_context("u_1", "datarc", session_scoped=True)
    assert overlay is not None and overlay["id"] == "142" and overlay["session_id"] == "s_A"


async def test_clear_session_scoped_leaves_default(store):
    """决策1：清 session 覆盖层不动永久默认。"""
    await store.set_tool_context("u_1", "datarc", context_id="1", source="explicit")
    await store.set_tool_context(
        "u_1",
        "datarc",
        context_id="142",
        source="session",
        session_id="s_A",
    )
    await store.clear_tool_context("u_1", "datarc", session_scoped=True)
    assert await store.get_tool_context("u_1", "datarc", session_scoped=True) is None
    default = await store.get_tool_context("u_1", "datarc")
    assert default is not None and default["id"] == "1"
