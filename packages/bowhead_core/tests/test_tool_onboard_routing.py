"""external-capability-onboarding §3.2 — proactive tool-onboarding routing block in the Lead prompt.

The `<tool_onboarding>` guidance (review P1-4: don't rely on the tool_creator schema description
alone) is injected into `build_lead_system_prompt` ONLY when both BOWHEAD_CCA_TOOL_CREATOR and
BOWHEAD_CCA_TOOL_CREATOR_PROACTIVE are ON. Default OFF → "" → dropped by the empty-filter → the
prompt is byte-identical to today (zero behavior change on the hot path).
"""

from __future__ import annotations

from bowhead_core.agents.prompts.lead import build_lead_system_prompt

_TAG = "<tool_onboarding>"


def _build() -> str:
    # facts=[] avoids the fact-store fetch.
    # block is not part of the default budget lint (measured separately when the flag flips).
    return build_lead_system_prompt("ws_test", facts=[])


def test_block_absent_by_default(monkeypatch):
    monkeypatch.delenv("BOWHEAD_CCA_TOOL_CREATOR", raising=False)
    monkeypatch.delenv("BOWHEAD_CCA_TOOL_CREATOR_PROACTIVE", raising=False)
    assert _TAG not in _build()


def test_block_present_when_both_flags_on(monkeypatch):
    monkeypatch.setenv("BOWHEAD_CCA_TOOL_CREATOR", "1")
    monkeypatch.setenv("BOWHEAD_CCA_TOOL_CREATOR_PROACTIVE", "1")
    prompt = _build()
    assert _TAG in prompt
    assert "tool_creator" in prompt  # tells the Lead which tool to call
    assert "MCP" in prompt  # ends out the hosted-MCP lane, not OpenAPI-only


def test_block_requires_tool_creator_flag_too(monkeypatch):
    # proactive ON but tool_creator OFF → block NOT injected (never advertise an unregistered tool)
    monkeypatch.delenv("BOWHEAD_CCA_TOOL_CREATOR", raising=False)
    monkeypatch.setenv("BOWHEAD_CCA_TOOL_CREATOR_PROACTIVE", "1")
    assert _TAG not in _build()


def test_block_present_when_base_on_and_proactive_default(monkeypatch):
    # §3.2 default ON (code-flip): base tool_creator on + proactive unset → block injected.
    monkeypatch.setenv("BOWHEAD_CCA_TOOL_CREATOR", "1")
    monkeypatch.delenv("BOWHEAD_CCA_TOOL_CREATOR_PROACTIVE", raising=False)
    assert _TAG in _build()


def test_proactive_can_be_disabled_via_env(monkeypatch):
    # kill switch: base on but PROACTIVE=0 → block NOT injected.
    monkeypatch.setenv("BOWHEAD_CCA_TOOL_CREATOR", "1")
    monkeypatch.setenv("BOWHEAD_CCA_TOOL_CREATOR_PROACTIVE", "0")
    assert _TAG not in _build()


def test_block_keeps_llm_provider_exclusion(monkeypatch):
    """A0 dogfood（2026-08-20，116/1.1.33）逮到的过度触发，锁住修法。

    「帮我加一个 DashScope 的模型」命中本块的「连接某 API 或服务、装个 X」→ Lead 调了
    `tool_creator` 开「新建工具」卡，并照本块的 ①② 端出 OpenAPI / hosted MCP 两条路
    （turn t_83a7992d1bf8：`tool_creator` ok=True + 一整段错误引导）。而 `llm_provider`
    面带 `POLICY_SECRET`，注册期断言禁止它开 chat 通道（config_copilot.py 的
    chat_draft_supported 互斥），所以正确行为是引导去设置页而**不是**开卡。

    本条锁的是「例外仍在块里」；配对的 negative control 见
    test_exclusion_absent_when_block_absent —— 块不注入时它必须一起消失，
    否则这个断言在块被整体删掉后仍会误绿。
    """
    monkeypatch.setenv("BOWHEAD_CCA_TOOL_CREATOR", "1")
    monkeypatch.setenv("BOWHEAD_CCA_TOOL_CREATOR_PROACTIVE", "1")
    prompt = _build()
    assert _TAG in prompt
    for kw in ("LLM model", "DashScope", "never call `tool_creator`", "Settings → Models"):
        assert kw in prompt, f"tool_onboarding 丢了 LLM provider 例外关键词：{kw!r}"


def test_exclusion_absent_when_block_absent(monkeypatch):
    """正对照：块不注入时例外也必须不在——证明上一条断言确实锚在这个块上。"""
    monkeypatch.delenv("BOWHEAD_CCA_TOOL_CREATOR", raising=False)
    monkeypatch.delenv("BOWHEAD_CCA_TOOL_CREATOR_PROACTIVE", raising=False)
    prompt = _build()
    assert _TAG not in prompt
    assert "never call `tool_creator`" not in prompt
