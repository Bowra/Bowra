"""memory-revive S1 · maybe_dream_post_turn —— Dreaming Light 的 turn 收尾触发。

与 test_memory_tools 的 NL intent post-turn 测试同法：用 REAL TurnRequest/TurnResult
驱动被抽出的触发函数，字段读错（当年 request.user_message 那类 P0）在这里必红。
这个触发点正是 discovery 里「测试绿 prod 暗」的断线处——docstring 承诺的
Orchestrator.on_turn_done() 从未存在，本文件锚死新接线不再回到那个状态。
"""

from __future__ import annotations

import pytest
from bowhead_core.turn_runner import maybe_dream_post_turn
from bowhead_core.types import IntentType, ToolInvocation, TurnRequest, TurnResult, TurnStatus


class _CapturingDreaming:
    def __init__(self) -> None:
        self.observed = []

    async def observe(self, obs, *, router=None):  # credential-readiness D2.1 signature alignment.
        self.observed.append(obs)
        return [obs]  # Any nonempty return represents the created list.


def _request(**overrides) -> TurnRequest:
    base: dict = {
        "session_id": "s",
        "workspace_id": "ws_retail",
        "user_id": "u1",
        "member_uid": "m",
        "text": "我更喜欢看环比",
        "turn_id": "t_1",
    }
    base.update(overrides)
    return TurnRequest(**base)


def _result(**overrides) -> TurnResult:
    base: dict = {
        "turn_id": "t_1",
        "status": TurnStatus.COMPLETED,
        "intent": IntentType.LOOKUP,
        "final_answer": "好的，环比 +3%",
    }
    base.update(overrides)
    return TurnResult(**base)


class _Orch:
    def __init__(self, dreaming=None) -> None:
        self.dreaming_light = dreaming
        self.user_memory_store = None
        self.memory_notes_store = None
        self.memory_candidate_store = None


@pytest.mark.asyncio
async def test_dream_post_turn_builds_observation_from_real_request():
    dreaming = _CapturingDreaming()
    result = _result(
        tool_invocations=[
            ToolInvocation(tool_name="bi_query", arguments={}),
            ToolInvocation(tool_name="python_sandbox", arguments={}),
        ]
    )
    created = await maybe_dream_post_turn(_Orch(dreaming), _request(), result)

    assert len(created) == 1
    obs = dreaming.observed[0]
    assert obs.turn_id == "t_1"
    assert obs.user_id == "u1"
    assert obs.user_text == "我更喜欢看环比"
    assert obs.final_answer == "好的，环比 +3%"
    assert obs.intent == "lookup"
    assert obs.tool_chain == ["bi_query", "python_sandbox"]
    assert obs.workspace_id == "ws_retail"


@pytest.mark.asyncio
async def test_dream_post_turn_noop_when_dreaming_none():
    assert await maybe_dream_post_turn(_Orch(dreaming=None), _request(), _result()) == []


@pytest.mark.asyncio
async def test_dream_post_turn_skips_non_completed():
    """failed/cancelled turn 的 final_answer 是噪声，不蒸馏。"""
    dreaming = _CapturingDreaming()
    out = await maybe_dream_post_turn(_Orch(dreaming), _request(), _result(status=TurnStatus.FAILED))
    assert out == [] and dreaming.observed == []


@pytest.mark.asyncio
async def test_dream_post_turn_skips_agent_authoring_turn():
    """Coach editing an Agent's output format must not become a personal-memory suggestion."""
    dreaming = _CapturingDreaming()
    result = _result(tool_invocations=[ToolInvocation(tool_name="subagent_spawn", arguments={"profile": "agent-coach"})])
    out = await maybe_dream_post_turn(_Orch(dreaming), _request(text="百分比保留一位小数，表格末尾必须包含合计行"), result)
    assert out == [] and dreaming.observed == []


@pytest.mark.asyncio
async def test_dream_post_turn_skips_when_remember_fired(caplog):
    """显式 remember 已落库的 turn 不再蒸馏——与 NL intent 同款防冗余规则。

    ★ 让路本身是对的，但必须**留痕**：此前这条 return 连一行日志都没有，
    于是「蒸馏没跑」和「蒸馏跑了但没提议」在日志里一模一样。
    2026-09-16 在 116 上为此误判成限流、查了半天（那轮根本不在限流窗口内）。
    ★ 突变验证：删掉这行 `logger.info` ⇒ 本用例红。
    """
    import logging

    dreaming = _CapturingDreaming()
    result = _result(tool_invocations=[ToolInvocation(tool_name="remember", arguments={})])
    with caplog.at_level(logging.INFO, logger="bowhead_core.turn_runner"):
        out = await maybe_dream_post_turn(_Orch(dreaming), _request(), result)
    assert out == [] and dreaming.observed == []
    msgs = [r.getMessage() for r in caplog.records]
    assert any("dreaming.skipped" in m and "remember_fired" in m for m in msgs), msgs


@pytest.mark.asyncio
async def test_dream_post_turn_skips_empty_text():
    dreaming = _CapturingDreaming()
    out = await maybe_dream_post_turn(_Orch(dreaming), _request(text="   "), _result())
    assert out == [] and dreaming.observed == []


@pytest.mark.asyncio
async def test_dream_post_turn_never_raises():
    class _Boom:
        async def observe(self, obs, *, router=None):  # credential-readiness D2.1 signature alignment.
            raise RuntimeError("llm down")

    out = await maybe_dream_post_turn(_Orch(_Boom()), _request(), _result())
    assert out == []


@pytest.mark.asyncio
async def test_dream_post_turn_digest_feeds_existing_memory():
    """orch 上的 stores 有货时，existing_memory 摘要必须带上（语义去重的原料）。"""

    class _Note:
        content = "- 用户老板叫张总"

    class _NotesStore:
        async def get(self, *, user_id):
            return _Note()

    class _Cand:
        kind = "preference"
        key = "prefer_qoq"
        value = "QoQ"

    class _CandStore:
        async def list_for_user(self, *, user_id, status, limit):
            return [_Cand()]

    dreaming = _CapturingDreaming()
    orch = _Orch(dreaming)
    orch.memory_notes_store = _NotesStore()
    orch.memory_candidate_store = _CandStore()

    await maybe_dream_post_turn(orch, _request(), _result())
    obs = dreaming.observed[0]
    assert "张总" in obs.existing_memory
    assert "prefer_qoq" in obs.existing_memory


@pytest.mark.asyncio
async def test_dream_post_turn_emits_backstop_notification():
    """产出候选后调 orch.notification_service.emit_memory_candidates（兜底 badge）。"""
    calls = []

    class _Notify:
        async def emit_memory_candidates(self, *, user_id, created_count, preview=""):
            calls.append((user_id, created_count, preview))

    class _Cand:
        value = "偏好环比"

    class _DreamOne:
        async def observe(self, obs, *, router=None):  # credential-readiness D2.1 signature alignment.
            return [_Cand()]

    orch = _Orch(_DreamOne())
    orch.notification_service = _Notify()
    created = await maybe_dream_post_turn(orch, _request(), _result())
    assert len(created) == 1
    assert calls == [("u1", 1, "偏好环比")]


@pytest.mark.asyncio
async def test_dream_post_turn_notify_failure_not_fatal():
    class _BadNotify:
        async def emit_memory_candidates(self, **kwargs):
            raise RuntimeError("notify down")

    orch = _Orch(_CapturingDreaming())
    orch.notification_service = _BadNotify()
    created = await maybe_dream_post_turn(orch, _request(), _result())
    assert len(created) == 1  # Notification failure must not discard distilled results.


@pytest.mark.asyncio
async def test_dream_post_turn_digest_store_failure_not_fatal():
    """任一 store 坏掉只丢那一段摘要，蒸馏照跑。"""

    class _BadStore:
        async def get(self, *, user_id):
            raise RuntimeError("db locked")

        async def list_for_user(self, **kwargs):
            raise RuntimeError("db locked")

    dreaming = _CapturingDreaming()
    orch = _Orch(dreaming)
    orch.memory_notes_store = _BadStore()
    orch.memory_candidate_store = _BadStore()

    created = await maybe_dream_post_turn(orch, _request(), _result())
    assert len(created) == 1
    assert dreaming.observed[0].existing_memory == ""
