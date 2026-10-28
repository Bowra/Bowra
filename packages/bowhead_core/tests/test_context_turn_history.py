"""上下文工程 sprint 批 3 —— turn 级历史代替 digest 的内容。

三层：纯函数（H 构造 / 规则块 / split 尾块）→ 穿过 SubagentRunner 的接线 → 穿过 TurnRunner→Lead 的接线。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from bowhead_core.context_assembly import INCOMPLETE_TURN_MARKERS, build_turn_history, split_system_for_prefix
from bowhead_core.context_history_flags import turn_history_enabled, turn_history_token_budget
from bowhead_core.cross_turn_digest import _strip_markers, render_history_rules_block, render_recent_turns_block
from bowhead_core.events import TurnEventBus
from bowhead_core.subagent import SubagentRunner
from bowhead_core.tool_registry import ToolExecutionContext, build_default_registry
from bowhead_router.types import Message

from bowhead_router import (
    AgentBinding,
    LLMResponse,
    MockAdapter,
    ProviderConfig,
    ProviderRegistry,
    Router,
    TokenUsage,
)

_FLAG = "BOWHEAD_CTX_TURN_HISTORY"
_ONE = len


def _turn(i: int, *, status: str = "completed", user: str | None = None, answer: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        id=f"t{i}",
        seq=i,
        status=status,
        user_text=user if user is not None else f"问题{i}",
        final_answer=answer if answer is not None else f"回答{i}",
    )


# ── flag ──


@pytest.mark.unit
def test_flag_defaults_on_and_zero_is_kill_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(_FLAG, raising=False)
    assert turn_history_enabled()
    monkeypatch.setenv(_FLAG, "0")
    assert not turn_history_enabled()


@pytest.mark.unit
def test_budget_env_garbage_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BOWHEAD_CTX_TURN_HISTORY_TOKENS", raising=False)
    assert turn_history_token_budget() == 40_000
    monkeypatch.setenv("BOWHEAD_CTX_TURN_HISTORY_TOKENS", "abc")
    assert turn_history_token_budget() == 40_000
    monkeypatch.setenv("BOWHEAD_CTX_TURN_HISTORY_TOKENS", "-5")
    assert turn_history_token_budget() == 40_000
    monkeypatch.setenv("BOWHEAD_CTX_TURN_HISTORY_TOKENS", "12345")
    assert turn_history_token_budget() == 12345


# ── H construction ──


@pytest.mark.unit
def test_history_is_user_assistant_pairs_oldest_first() -> None:
    th = build_turn_history([_turn(1), _turn(2)], exclude_turn_id="t3", budget_tokens=0, estimate=_ONE)
    assert [(m["role"], m["content"]) for m in th.messages] == [("user", "问题1"), ("assistant", "回答1"), ("user", "问题2"), ("assistant", "回答2")]
    assert th.turns_used == 2 and th.turns_dropped == 0


@pytest.mark.unit
def test_current_turn_is_excluded() -> None:
    th = build_turn_history([_turn(1), _turn(2)], exclude_turn_id="t2", budget_tokens=0, estimate=_ONE)
    assert th.turns_used == 1 and th.messages[0]["content"] == "问题1"


@pytest.mark.unit
def test_pending_running_and_empty_turns_do_not_enter() -> None:
    """dev 库查实：clarification_pending 轮为空、running 轮还在跑、completed 但空的轮用户没看到回答。都不进。"""
    turns = [
        _turn(2, status="clarification_pending", answer=""),
        _turn(3, status="running", answer=""),
        _turn(4, status="completed", answer=""),
        _turn(5, status="completed", answer="我需要知道你在哪个城市，等你确认城市再查。"),  # Prose follow-up: included
    ]
    th = build_turn_history(turns, exclude_turn_id=None, budget_tokens=0, estimate=_ONE)
    assert th.turns_used == 1
    assert th.messages[1]["content"].startswith("我需要知道")


@pytest.mark.unit
def test_incomplete_turns_enter_as_user_text_plus_fixed_marker() -> None:
    """r7（116 第五轮）：取消 / 中止 / 失败的轮进历史——用户看见过自己那句话，下一句「改成第二季度」指的就是它。
    assistant 侧是软件写死的标记，不渲 failed 轮的系统重启文案；没原话的轮仍不进。"""
    from bowhead_core.context_assembly import INCOMPLETE_TURN_MARKERS

    turns = [
        _turn(1, status="cancelled", user="统计上半年各品牌营业额，写成画布报告", answer=""),
        _turn(2, status="failed", answer="[系统：会话在 turn 进行中被重启，未完成.]"),
        _turn(3, status="aborted", answer=""),
        _turn(4, status="cancelled", user="", answer=""),
        _turn(5, status="completed", answer="回答5"),
    ]
    th = build_turn_history(turns, exclude_turn_id=None, budget_tokens=0, estimate=_ONE)
    assert th.turns_used == 4 and th.turn_sizes == [2, 2, 2, 2]
    m = th.messages
    assert m[0] == {"role": "user", "content": "统计上半年各品牌营业额，写成画布报告"}
    assert m[1] == {"role": "assistant", "content": INCOMPLETE_TURN_MARKERS["cancelled"]}
    assert m[3]["content"] == INCOMPLETE_TURN_MARKERS["failed"] and "重启" not in m[3]["content"]
    assert m[5]["content"] == INCOMPLETE_TURN_MARKERS["aborted"]
    assert m[7]["content"] == "回答5"
    assert "用户中止" in INCOMPLETE_TURN_MARKERS["cancelled"] and "cancelled" in INCOMPLETE_TURN_MARKERS["cancelled"]
    assert th.incomplete_turns == 3


@pytest.mark.unit
def test_failed_marker_carries_error_kind_and_dropped_incomplete_turns_are_uncounted() -> None:
    """failed 轮的 ``error_kind`` 是软件写的枚举，进标记（「再试一次」怎么试取决于它）；预算掉掉的未完成轮不再计入 incomplete_turns。"""
    from bowhead_core.context_assembly import incomplete_turn_marker

    assert incomplete_turn_marker("failed", "timeout") == "[本轮执行失败，未完成 / failed before completion: timeout]"
    assert incomplete_turn_marker("failed", None) == INCOMPLETE_TURN_MARKERS["failed"]
    assert incomplete_turn_marker("cancelled", "user_cancel") == INCOMPLETE_TURN_MARKERS["cancelled"]
    t_failed = _turn(1, status="failed", answer="")
    t_failed.error_kind = "llm_failed"
    turns = [t_failed, _turn(2, status="cancelled", answer=""), _turn(3, status="completed", answer="回答3")]
    th = build_turn_history(turns, exclude_turn_id=None, budget_tokens=0, estimate=_ONE)
    assert th.messages[1]["content"].endswith(": llm_failed]") and th.incomplete_turns == 2
    # The budget fits only the last turn: drop both incomplete turns and reset their count to zero.
    small = build_turn_history(turns, exclude_turn_id=None, budget_tokens=10, hysteresis=1.0, estimate=_ONE)
    assert small.turns_dropped == 2 and small.incomplete_turns == 0


@pytest.mark.unit
def test_incomplete_turn_with_spawn_records_keeps_the_tool_calls() -> None:
    """取消前已派出的 peer 有 summary 时照常渲成 tool_calls——Lead 该知道自己派过谁、干到哪。"""
    from bowhead_core.context_assembly import INCOMPLETE_TURN_MARKERS

    turns = [_turn(1, status="cancelled", answer="")]
    th = build_turn_history(turns, exclude_turn_id=None, budget_tokens=0, estimate=_ONE, spawns={"t1": [_spawn("call_a", status="cancelled")]})
    assert th.turns_used == 1 and th.turn_sizes == [4]
    assert th.messages[1]["tool_calls"][0]["name"] == "subagent_spawn"
    assert th.messages[3]["content"] == INCOMPLETE_TURN_MARKERS["cancelled"]
    # A synthetic record (no peer receipt) cannot claim zero canvas sections written: the killed 116 C3 peer had already written one.
    from bowhead_core.context_assembly import render_spawn_result

    synth = _spawn("call_b", status="cancelled", child_tool_count=2, canvas_sections=(), outcome="", synthesized=True)
    rendered = render_spawn_result(synth)
    assert rendered.startswith("cancelled｜子工具 2 次｜没有回执") and "画布写入" not in rendered
    assert "画布写入 0 节" in render_spawn_result(_spawn("call_c", canvas_sections=(), synthesized=False))


@pytest.mark.unit
def test_markers_are_stripped_from_answers_only() -> None:
    turns = [_turn(1, user="看 [cite:1]", answer="营业额 92.94 亿 [cite:1] [num:3]。")]
    th = build_turn_history(turns, exclude_turn_id=None, budget_tokens=0, estimate=_ONE, strip=_strip_markers)
    assert th.messages[0]["content"] == "看 [cite:1]", "用户原话不动"
    assert th.messages[1]["content"] == "营业额 92.94 亿。"


@pytest.mark.unit
def test_over_budget_drops_whole_oldest_turns_with_hysteresis() -> None:
    """每轮 20 字；上限 100 → 超（6 轮=120）；掉到 0.75×100=75 以下 ⇒ 掉 3 轮（剩 60）而不是 1 轮。"""
    turns = [_turn(i, user="u" * 10, answer="a" * 10) for i in range(6)]
    th = build_turn_history(turns, exclude_turn_id=None, budget_tokens=100, hysteresis=0.75, estimate=_ONE)
    assert th.turns_dropped == 3 and th.turns_used == 3 and th.tokens == 60
    assert th.messages[0]["content"] == "u" * 10 and len(th.messages) == 6


@pytest.mark.unit
def test_never_truncates_inside_a_turn() -> None:
    long_answer = "x" * 200
    turns = [_turn(1, answer="y" * 300), _turn(2, answer=long_answer)]
    # Total ≈ 510 > 400; floor 300 ⇒ drop the oldest whole turn (≈ 203 remain); retain the other answer in full, without trimming to the limit.
    th = build_turn_history(turns, exclude_turn_id=None, budget_tokens=400, estimate=_ONE)
    assert th.turns_dropped == 1 and th.turns_used == 1
    assert th.messages[1]["content"] == long_answer
    # One turn alone exceeds the floor ⇒ drop it whole, without truncation: no history is preferable to partial history.
    th2 = build_turn_history([_turn(1, answer="z" * 500)], exclude_turn_id=None, budget_tokens=300, estimate=_ONE)
    assert th2.turns_used == 0 and th2.turns_dropped == 1


@pytest.mark.unit
def test_under_budget_keeps_everything() -> None:
    turns = [_turn(i) for i in range(5)]
    th = build_turn_history(turns, exclude_turn_id=None, budget_tokens=10_000, estimate=_ONE)
    assert th.turns_used == 5 and th.turns_dropped == 0


@pytest.mark.unit
def test_custom_message_factory_is_used() -> None:
    th = build_turn_history([_turn(1)], exclude_turn_id=None, budget_tokens=0, estimate=_ONE, make_message=lambda r, c: Message(role=r, content=c))
    assert isinstance(th.messages[0], Message) and th.messages[0].role == "user"


# ── Rule block: rules and metadata only, no content ──

_DIGESTS = [
    {
        "user_text": "上半年营业额",
        "final_answer_full": "营业额 92.94 亿，完整报告见画布。",
        "conclusion": "增长 23.7%",
        "key_facts": [{"label": "利润率", "value": "17.5%", "delta": "+3.4pp"}],
        "insights": ["金辉福贡献最大"],
        "answered_via": {"source": "dataset"},
    },
    {"user_text": "天气呢", "final_answer_full": "今天北京晴", "answered_via": {"source": "web", "open_world": True}, "canvas_id": "cv_9"},
]


@pytest.mark.unit
def test_rules_block_carries_rules_labels_and_report_facts_but_not_answers() -> None:
    """review 修：结论 / 关键数据 / 洞察来自报告 payload，不是聊天回答的投影（报告轮的回答只是「见画布」），
    要留在规则块里；回答正文本身由历史承载，不重复。轮次按用户原话点名，不按「第 i 轮」编号。"""
    block = render_history_rules_block(_DIGESTS, source_continuity=True, section_revise=True)
    for answer in ("营业额 92.94 亿", "今天北京晴"):
        assert answer not in block, answer
    assert "Turn 1" not in block and "Turn 2" not in block
    assert 'the turn "上半年营业额": source: the Datarc workspace dataset (BI) · conclusion: 增长 23.7%' in block
    # r4: omit key data / insights from the tail block; they fuel fabricated numbers (116 R6 copied prior-turn numbers just before the task and claimed recalculation).
    assert "key data" not in block and "17.5%" not in block
    assert "insights:" not in block and "金辉福贡献最大" not in block
    assert 'the turn "天气呢": source: web / external knowledge' in block
    assert "Topic continuity" in block
    assert "Source continuity" in block
    assert "canvas cv_9" in block
    assert "Follow-up mode" in block


@pytest.mark.unit
def test_rules_block_force_renders_rules_without_digests() -> None:
    """r7：会话里只有未完成轮时没有 digest，但话题连续性规则恰恰是这时最需要的——force 只渲规则不渲每轮事实；不 force 仍是空串。"""
    from bowhead_core.cross_turn_digest import render_history_rules_block

    assert render_history_rules_block([]) == ""
    forced = render_history_rules_block([], force=True)
    lines = forced.split("\n")
    assert lines[0] == "## Follow-up rules for this session" and len(lines) == 3, lines
    assert "Topic continuity" in lines[2] and "cancelled" in lines[2] and "conclusion:" not in forced and "source:" not in forced
    assert render_history_rules_block(_DIGESTS, force=True) == render_history_rules_block(_DIGESTS)


@pytest.mark.unit
def test_rules_block_conditions_match_the_digest_block() -> None:
    off = render_history_rules_block(_DIGESTS, source_continuity=False, section_revise=False)
    assert "Source continuity" not in off and "source:" not in off and "Follow-up mode" not in off
    assert "Topic continuity" in off and "canvas cv_9" in off
    assert 'the turn "上半年营业额": conclusion: 增长 23.7%' in off, "来源不给时报告事实仍在"
    no_open = render_history_rules_block([_DIGESTS[0]], source_continuity=True, section_revise=False)
    assert "Source continuity" not in no_open, "近轮没走开放世界就不给来源连续性规则（与 digest 块同条件）"
    assert render_history_rules_block([], source_continuity=True, section_revise=True) == ""


@pytest.mark.unit
def test_rule_prose_is_byte_identical_between_the_two_renderers() -> None:
    """规则文案抽成常量后，digest 块（flag OFF）与规则块（flag ON）必须逐字节同一份话。"""
    digest = render_recent_turns_block(_DIGESTS, source_continuity=True, section_revise=True)
    rules = render_history_rules_block(_DIGESTS, source_continuity=True, section_revise=True)
    for line in rules.splitlines():
        if line.startswith(("**话题连续性**", "**来源连续性**", "- **续轮档位**", "- 产出: 报告已在画布")):
            assert line in digest, line[:30]


# ── split: put the rule block first in the tail block; byte-identical when empty ──


def _blocks() -> dict[str, str]:
    return {
        "profile_prompt": "P",
        "extra_instructions_block": "E",
        "memory_block": "M",
        "language_block": "\n\nL",
    }


@pytest.mark.unit
def test_rules_block_leads_the_tail_and_stays_out_of_system(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOWHEAD_CTX_PREFIX_ORDER", "1")
    sp = split_system_for_prefix(**_blocks(), extra_is_stable=True, turn_rules_block="R")
    assert "R" not in sp.stable_system
    assert sp.turn_tail.startswith("R")
    assert sp.turn_tail_language == "L"
    # Peer tier: the per-spawn extra instructions also go to the tail, after the rules block.
    peer = split_system_for_prefix(**_blocks(), turn_rules_block="R")
    assert peer.turn_tail.index("R") < peer.turn_tail.index("E")


@pytest.mark.unit
def test_empty_rules_block_is_byte_identical(monkeypatch: pytest.MonkeyPatch) -> None:
    for flag in ("1", "0"):
        monkeypatch.setenv("BOWHEAD_CTX_PREFIX_ORDER", flag)
        a = split_system_for_prefix(**_blocks(), extra_is_stable=True)
        b = split_system_for_prefix(**_blocks(), extra_is_stable=True, turn_rules_block="")
        assert a == b


# ── Wiring through SubagentRunner ──


class _CaptureRouter(Router):
    def __init__(self, reg: ProviderRegistry) -> None:
        super().__init__(reg)
        self.calls: list[tuple[str, list[tuple[str, str]]]] = []

    def _rec(self, req: Any) -> None:
        self.calls.append((req.system or "", [(m.role, m.content or "") for m in (req.messages or [])]))

    async def invoke(self, agent_type, req, **kw):  # type: ignore[override]
        self._rec(req)
        return await super().invoke(agent_type, req, **kw)

    async def stream(self, agent_type, req, **kw):  # type: ignore[override]
        self._rec(req)
        async for chunk in super().stream(agent_type, req, **kw):
            yield chunk


async def _run(monkeypatch: pytest.MonkeyPatch, profile_name: str = "compute", **extra: Any) -> tuple[str, list[tuple[str, str]]]:
    monkeypatch.setenv("BOWHEAD_CTX_PREFIX_ORDER", "1")
    adapter = MockAdapter(provider_name="p1")
    adapter.scripted_responses = [LLMResponse(content="直答", usage=TokenUsage(input_tokens=10, output_tokens=5))]
    reg = ProviderRegistry()
    reg.load([ProviderConfig(name="p1", type="mock")])
    reg._adapters["p1"] = adapter  # type: ignore[attr-defined]
    router = _CaptureRouter(reg)
    for t in ("lead", "analyst", "compute", "narrative", "echo"):
        router.bind(AgentBinding(agent_type=t, primary=("p1", "mock")))
    runner = SubagentRunner(router=router, tool_registry=build_default_registry(), backend="legacy")
    ctx = ToolExecutionContext(workspace_id="w", member_uid="m", user_id="u", turn_id="t", datarc=None)  # type: ignore[arg-type]
    ctx.language_context = extra.pop("language_context", None)
    await runner.run(profile_name=profile_name, task="TASK_MARKER", parent_turn_id="t", parent_bus=TurnEventBus(), parent_ctx=ctx, **extra)
    assert router.calls, "没记到任何请求，这条闸是死的"
    return router.calls[0]


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["en-US", "zh-CN", "de-DE", "fr-FR"])
async def test_lead_language_precedence_is_system_guidance_without_changing_user_material(monkeypatch, language):
    from bowhead_core.language_context import LanguageContext

    system, messages = await _run(monkeypatch, profile_name="lead", language_context=LanguageContext(output_language=language))
    assert messages == [("user", "TASK_MARKER")]
    assert system.index("explicitly names an answer language") < system.index(f"Otherwise use the resolved output_language={language}")
    assert "pass it on every Agent dispatch" in system


@pytest.mark.unit
@pytest.mark.asyncio
async def test_lead_system_prompt_has_no_shared_chinese_prefix_but_peers_keep_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """Lead 提示词改英文（D4）：Lead 的 extra_instructions 就是整份英文系统提示词，不再套「额外指令：」；peer 逐字节不变。"""
    lead_system, _ = await _run(monkeypatch, profile_name="lead", extra_instructions="<lead_orchestrator>LEAD_MARKER</lead_orchestrator>")
    assert "<lead_orchestrator>LEAD_MARKER</lead_orchestrator>" in lead_system
    assert "额外指令" not in lead_system
    peer_system, peer_msgs = await _run(monkeypatch, extra_instructions="PEER_MARKER")
    assert "额外指令：\nPEER_MARKER" in peer_system + "\n".join(c for _, c in peer_msgs)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_wiring_history_precedes_task_and_rules_land_in_the_tail(monkeypatch: pytest.MonkeyPatch) -> None:
    """突变锚点：把 `messages = [*_history_msgs, task]` 改回 `[task]` ⇒ 红；把 turn_rules_block 传空 ⇒ 红。"""
    hist = [Message(role="user", content="H_USER_1"), Message(role="assistant", content="H_ASSISTANT_1")]
    system, msgs = await _run(monkeypatch, history=hist, history_rules="RULES_MARKER")
    roles_contents = [(r, c) for r, c in msgs]
    assert roles_contents[0] == ("user", "H_USER_1") and roles_contents[1] == ("assistant", "H_ASSISTANT_1")
    task_idx = next(i for i, (_, c) in enumerate(roles_contents) if c == "TASK_MARKER")
    assert task_idx == 2, roles_contents
    assert "RULES_MARKER" not in system, "规则块不该进 system（那会把 Lead 稳定段打花）"
    rules_idx = next(i for i, (_, c) in enumerate(roles_contents) if "RULES_MARKER" in c)
    assert rules_idx > task_idx, "规则块在尾块，位于本轮 task 之后"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_wiring_without_history_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    system_a, msgs_a = await _run(monkeypatch)
    system_b, msgs_b = await _run(monkeypatch, history=None, history_rules=None)
    assert system_a == system_b and msgs_a == msgs_b
    assert msgs_a[0][1] == "TASK_MARKER"


# ── Wiring through TurnRunner → Lead (only Lead passes history; flag OFF uses digests) ──


def _turn_runner_helpers() -> Any:
    """`tests/` 不是包；按文件路径把 test_turn_runner_unit 的 _make_orch / _req 借过来。"""
    import importlib.util
    from pathlib import Path

    path = Path(__file__).with_name("test_turn_runner_unit.py")
    spec = importlib.util.spec_from_file_location("_turn_runner_unit_helpers", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _session_store_with(turns: list[Any], digests: list[dict]) -> Any:
    orch = _turn_runner_helpers()._make_orch()
    ss = orch.session_store

    async def _list_turns(**kw: Any) -> list[Any]:
        return turns

    async def _list_recent_digests(**kw: Any) -> list[dict]:
        return digests

    ss.list_turns = _list_turns
    ss.list_recent_digests = _list_recent_digests
    return ss


@pytest.mark.asyncio
async def test_lead_passes_history_and_rules_and_drops_digest_content(
    monkeypatch: pytest.MonkeyPatch, fake_subagent_runner, make_subagent_result
) -> None:
    """flag ON：Lead 收到 H + 规则块；digest 的**内容**不再进 lead_prompt。"""
    from bowhead_core.turn_runner import TurnRunner

    helpers = _turn_runner_helpers()
    _make_orch, _req = helpers._make_orch, helpers._req

    monkeypatch.setenv(_FLAG, "1")
    fake_subagent_runner.results = [make_subagent_result(final_answer="ok")]
    ss = _session_store_with([_turn(1, user="上半年营业额", answer="92.94 亿 [cite:1]")], _DIGESTS)
    orch = _make_orch(subagent_runner=fake_subagent_runner, session_store=ss)
    await TurnRunner(orch).run(_req("那利润呢"))
    inv = fake_subagent_runner.invocations[0]
    assert inv["profile_name"] == "lead"
    hist = inv.get("history")
    assert hist and [(m.role, m.content) for m in hist] == [("user", "上半年营业额"), ("assistant", "92.94 亿")]
    assert "Topic continuity" in (inv.get("history_rules") or "")
    assert "## Recent turns in this session" not in (inv.get("extra_instructions") or ""), "digest 内容块不该再进 lead_prompt"
    assert "今天北京晴" not in (inv.get("extra_instructions") or "")


@pytest.mark.asyncio
async def test_lead_flag_off_keeps_the_digest_path(monkeypatch: pytest.MonkeyPatch, fake_subagent_runner, make_subagent_result) -> None:
    from bowhead_core.turn_runner import TurnRunner

    helpers = _turn_runner_helpers()
    _make_orch, _req = helpers._make_orch, helpers._req

    monkeypatch.setenv(_FLAG, "0")
    fake_subagent_runner.results = [make_subagent_result(final_answer="ok")]
    ss = _session_store_with([_turn(1)], _DIGESTS)
    orch = _make_orch(subagent_runner=fake_subagent_runner, session_store=ss)
    await TurnRunner(orch).run(_req("那利润呢"))
    inv = fake_subagent_runner.invocations[0]
    assert inv.get("history") is None and inv.get("history_rules") is None
    assert "## Recent turns in this session" in (inv.get("extra_instructions") or ""), "flag OFF 必须逐字节回到 digest 形态"


@pytest.mark.unit
def test_special_token_text_does_not_kill_history_or_the_guard() -> None:
    """review 修（P1）：tiktoken 对 `<|endoftext|>` 默认抛 ValueError；一条讲 tokenizer 的消息会让整个会话的历史永久失效、
    守卫直接把 turn 打崩。估算器改为把特殊 token 当普通文本数。"""
    from bowhead_core.context_assembly import estimate_messages_tokens, guard_window
    from bowhead_core.token_estimator import estimate_tokens

    txt = "what is <|endoftext|> and <|fim_prefix|>?"
    assert estimate_tokens(txt) > 0
    th = build_turn_history([_turn(1, user=txt, answer="它们是 tokenizer 的特殊 token <|endoftext|>")], exclude_turn_id=None, budget_tokens=40_000)
    assert th.turns_used == 1
    msgs = [
        Message(role="user", content=txt),
        Message(role="assistant", content="", tool_calls=None),
        Message(role="tool", content=txt * 50, tool_call_id="c", name="t"),
    ]
    assert estimate_messages_tokens(msgs) > 0
    assert guard_window(msgs, budget_tokens=10) == 0  # The only tool follows the last assistant message ⇒ protected; nevertheless, do not raise.


@pytest.mark.asyncio
async def test_lead_falls_back_to_digest_block_when_history_is_empty(
    monkeypatch: pytest.MonkeyPatch, fake_subagent_runner, make_subagent_result
) -> None:
    """review 修二：历史为空（这里：唯一的先前轮是 clarification_pending；r7 起 failed 轮会进历史）而 digest 存在
    ⇒ 走 digest 块，规则块不能指着不存在的对话。"""
    from bowhead_core.turn_runner import TurnRunner

    helpers = _turn_runner_helpers()
    _make_orch, _req = helpers._make_orch, helpers._req

    monkeypatch.setenv(_FLAG, "1")
    fake_subagent_runner.results = [make_subagent_result(final_answer="ok")]
    ss = _session_store_with([_turn(1, status="clarification_pending", answer="")], _DIGESTS)
    orch = _make_orch(subagent_runner=fake_subagent_runner, session_store=ss)
    await TurnRunner(orch).run(_req("那利润呢"))
    inv = fake_subagent_runner.invocations[0]
    assert inv.get("history") is None and inv.get("history_rules") is None
    assert "## Recent turns in this session" in (inv.get("extra_instructions") or ""), "历史为空时应回落到 digest 内容块"


@pytest.mark.asyncio
async def test_lead_history_budget_is_capped_by_the_declared_window(
    monkeypatch: pytest.MonkeyPatch, fake_subagent_runner, make_subagent_result
) -> None:
    """review 修：目录声明了窗口 ⇒ 历史预算 = min(env, window/4)。这里 window=400 ⇒ 预算 100，三轮各 ≈60 token 的历史只剩最新一轮。"""
    from types import SimpleNamespace

    from bowhead_core.turn_runner import TurnRunner

    helpers = _turn_runner_helpers()
    _make_orch, _req = helpers._make_orch, helpers._req

    monkeypatch.setenv(_FLAG, "1")
    monkeypatch.setenv("BOWHEAD_CTX_TURN_HISTORY_TOKENS", "40000")
    fake_subagent_runner.results = [make_subagent_result(final_answer="ok")]
    fake_subagent_runner.router = SimpleNamespace(context_window_for=lambda agent_type, profile=None, user_pref=None: 400)
    turns = [_turn(i, user="x " * 20, answer="y " * 40) for i in range(3)]  # Approximately 60 tokens per turn
    ss = _session_store_with(turns, [])
    orch = _make_orch(subagent_runner=fake_subagent_runner, session_store=ss)
    await TurnRunner(orch).run(_req("那利润呢"))
    hist = fake_subagent_runner.invocations[0].get("history") or []
    assert 0 < len(hist) < 6, f"预算 100 token 装不下三轮（{len(hist)} 条）"


@pytest.mark.unit
def test_fabrication_collector_does_not_treat_prior_turn_numbers_as_evidence() -> None:
    """116 dogfood 撤回：上一轮的 key_facts 不是本轮的证据——Lead 照抄上一轮数字时守卫必须还能拦。"""
    from types import SimpleNamespace

    from bowhead_core.agents.lead_orchestrator import collect_fabrication_tool_results
    from bowhead_core.analysis_payload import build_observed_numbers

    ctx = SimpleNamespace(_recent_digest_key_facts=[{"key_facts": [{"label": "利润率", "value": "17.5%"}]}])
    out = collect_fabrication_tool_results(SimpleNamespace(tool_invocations=[]), ctx)
    assert out == []
    assert 0.175 not in build_observed_numbers(out)


# ── r4: history includes `subagent_spawn` calls (116 R6: with only [original request, final answer], Lead copied examples and claimed recalculation without delegation) ──

import json  # noqa: E402

from bowhead_core.agents.lead_orchestrator import final_answer_bounce_directive  # noqa: E402
from bowhead_core.context_assembly import (  # noqa: E402
    drop_history_turns,
    guard_window,
    history_call_id,
    render_spawn_arguments,
    render_spawn_result,
    turn_sizes_from_messages,
)
from bowhead_core.session_store import TurnSpawnRecord  # noqa: E402


def _spawn(call_id: str = "call_1", **kw: Any) -> TurnSpawnRecord:
    base: dict[str, Any] = {
        "call_id": call_id,
        "profile": "compute",
        "task": "把口径改成 2026年1-3月，就地修订",
        "rationale": "用户改口径，要重算",
        "status": "succeeded",
        "failure_reason": None,
        "child_tool_count": 16,
        "canvas_sections": (("hbar", "各省退款率排名"), ("dual_axis", "各品牌营业额")),
        "outcome": "1-3 月营业额 51.80 亿",
    }
    base.update(kw)
    return TurnSpawnRecord(**base)


@pytest.mark.unit
def test_history_renders_spawn_turns_as_tool_calls_and_sizes_follow() -> None:
    """派过活的轮 = user → assistant(tool_calls) → tool → assistant；没派活的轮仍两条。turn_sizes 与之一致。"""
    th = build_turn_history([_turn(1), _turn(2)], exclude_turn_id=None, budget_tokens=0, spawns={"t1": [_spawn()]})
    m = th.messages
    assert th.turn_sizes == [4, 2] and th.turns_used == 2
    assert m[0] == {"role": "user", "content": "问题1"}
    assert m[1]["role"] == "assistant" and m[1]["content"] == ""
    (tc,) = m[1]["tool_calls"]
    assert tc["id"] == history_call_id(1, "call_1") == "h1_call_1" and tc["name"] == "subagent_spawn"
    assert list(tc["arguments"].keys()) == ["profile", "rationale", "task"], "参数键序固定（缓存前缀逐字节稳定）"
    assert m[2]["role"] == "tool" and m[2]["tool_call_id"] == "h1_call_1" and m[2]["name"] == "subagent_spawn"
    assert "succeeded" in m[2]["content"] and "子工具 16 次" in m[2]["content"] and "画布写入 2 节" in m[2]["content"]
    assert "hbar「各省退款率排名」" in m[2]["content"] and "结论：1-3 月营业额 51.80 亿" in m[2]["content"]
    assert m[3] == {"role": "assistant", "content": "回答1"}
    assert m[4:] == [{"role": "user", "content": "问题2"}, {"role": "assistant", "content": "回答2"}]


@pytest.mark.unit
def test_multiple_spawns_in_one_turn_render_in_order_and_failures_are_honest() -> None:
    recs = [_spawn("call_a"), _spawn("call_b", profile="canvas-writer", status="failed", failure_reason="peer_timeout", canvas_sections=())]
    th = build_turn_history([_turn(1)], exclude_turn_id=None, budget_tokens=0, spawns={"t1": recs})
    m = th.messages
    assert th.turn_sizes == [5]
    assert [tc["id"] for tc in m[1]["tool_calls"]] == ["h1_call_a", "h1_call_b"]
    assert m[2]["tool_call_id"] == "h1_call_a" and m[3]["tool_call_id"] == "h1_call_b"
    assert "failed" in m[3]["content"] and "失败原因：peer_timeout" in m[3]["content"] and "画布写入 0 节" in m[3]["content"]


@pytest.mark.unit
def test_spawn_rendering_is_deterministic_and_carries_no_relative_index() -> None:
    """同一条记录渲两次逐字节相同；不含「第 i 轮」这类掉最老一轮就会漂移的相对标号；duration 不进。"""
    r = _spawn()
    assert json.dumps(render_spawn_arguments(r), ensure_ascii=False) == json.dumps(render_spawn_arguments(r), ensure_ascii=False)
    text = render_spawn_result(r)
    assert text == render_spawn_result(r)
    assert "第" not in text and "ms" not in text


@pytest.mark.unit
def test_spawn_records_without_call_id_are_ignored() -> None:
    th = build_turn_history([_turn(1)], exclude_turn_id=None, budget_tokens=0, spawns={"t1": [_spawn(call_id="")]})
    assert th.turn_sizes == [2] and len(th.messages) == 2


@pytest.mark.unit
def test_spawn_tokens_count_toward_budget_and_a_dropped_turn_takes_its_tool_messages_along() -> None:
    est = len
    rec = _spawn()
    spawn_cost = est(json.dumps(render_spawn_arguments(rec), ensure_ascii=False)) + est(render_spawn_result(rec))
    plain = est("问题1") + est("回答1")
    # Budget exactly fits two plain-text turns; include the extra spawn tokens in turn 1 ⇒ over budget ⇒ drop all of turn 1.
    budget = plain * 2
    assert spawn_cost > 0
    th = build_turn_history([_turn(1), _turn(2)], exclude_turn_id=None, budget_tokens=budget, hysteresis=1.0, estimate=est, spawns={"t1": [rec]})
    assert th.turns_dropped == 1 and th.turn_sizes == [2]
    assert th.messages[0]["content"] == "问题2" and all(m["role"] != "tool" for m in th.messages)


@pytest.mark.unit
def test_drop_history_turns_drops_whole_variable_size_turns_and_pops_sizes() -> None:
    est = len
    msgs = [
        Message(role="user", content="a" * 100),
        Message(role="assistant", content="", tool_calls=None),
        Message(role="tool", content="b" * 100, tool_call_id="c1", name="subagent_spawn"),
        Message(role="assistant", content="c" * 100),
        Message(role="user", content="d" * 100),
        Message(role="assistant", content="e" * 100),
        Message(role="user", content="TASK"),
    ]
    sizes = [4, 2]
    # Total ≈ 504; budget 300, hysteresis 1.0 ⇒ drop turn 1 (300), then stop at 204 ≤ 300.
    dropped, tokens = drop_history_turns(msgs, turn_sizes=sizes, budget_tokens=300, estimate=est, hysteresis=1.0)
    assert dropped == 1 and tokens == 300
    assert sizes == [2] and [m.role for m in msgs] == ["user", "assistant", "user"]
    assert msgs[-1].content == "TASK"


@pytest.mark.unit
def test_drop_history_turns_is_a_noop_under_budget_and_with_no_history() -> None:
    msgs = [Message(role="user", content="x" * 10)]
    assert drop_history_turns(msgs, turn_sizes=[], budget_tokens=5, estimate=len) == (0, 0)
    assert drop_history_turns(msgs, turn_sizes=[1], budget_tokens=1000, estimate=len) == (0, 0)


@pytest.mark.unit
def test_bounce_directive_follows_language() -> None:
    assert "subagent_spawn" in final_answer_bounce_directive("zh-CN") and "画布" in final_answer_bounce_directive("zh-CN")
    assert "subagent_spawn" in final_answer_bounce_directive("en-US") and "canvas" in final_answer_bounce_directive("en-US")
    # No language → English fallback (language-policy.md).
    assert final_answer_bounce_directive(None) == final_answer_bounce_directive("en-US")


@pytest.mark.asyncio
async def test_lead_history_carries_spawn_tool_calls_and_turn_sizes(
    monkeypatch: pytest.MonkeyPatch, fake_subagent_runner, make_subagent_result
) -> None:
    """接线：session_store 有 `list_turn_spawns` 时，Lead 收到的 history 里带 tool 消息，且 history_turn_sizes 跟着走。"""
    from bowhead_core.turn_runner import TurnRunner

    helpers = _turn_runner_helpers()
    _make_orch, _req = helpers._make_orch, helpers._req

    monkeypatch.setenv(_FLAG, "1")
    fake_subagent_runner.results = [make_subagent_result(final_answer="ok")]
    ss = _session_store_with([_turn(1, user="改成 1-3 月", answer="口径已改 [cite:1]"), _turn(2, user="谢谢", answer="不客气")], _DIGESTS)
    asked: list[list[str]] = []

    async def _list_turn_spawns(turn_ids: list[str]) -> dict[str, list[TurnSpawnRecord]]:
        asked.append(list(turn_ids))
        return {"t1": [_spawn()]}

    ss.list_turn_spawns = _list_turn_spawns
    orch = _make_orch(subagent_runner=fake_subagent_runner, session_store=ss)
    await TurnRunner(orch).run(_req("第二季度"))
    inv = fake_subagent_runner.invocations[0]
    assert asked and set(asked[0]) >= {"t1", "t2"}
    hist = inv["history"]
    assert [m.role for m in hist] == ["user", "assistant", "tool", "assistant", "user", "assistant"]
    assert hist[1].tool_calls and hist[1].tool_calls[0].id == "h1_call_1" and hist[1].tool_calls[0].name == "subagent_spawn"
    assert hist[1].tool_calls[0].arguments["profile"] == "compute"
    assert hist[2].tool_call_id == "h1_call_1" and "画布写入 2 节" in hist[2].content
    assert hist[3].content == "口径已改"
    assert turn_sizes_from_messages(hist) == [4, 2]


@pytest.mark.asyncio
async def test_lead_history_keeps_a_cancelled_turn_with_its_spawn_and_forces_the_rules_block(
    monkeypatch: pytest.MonkeyPatch, fake_subagent_runner, make_subagent_result
) -> None:
    """r7 接线（116 C1 的形状）：会话里唯一的先前轮被用户中止、没有 digest ⇒ 历史=「原话 → spawn → 标记」，规则块照样给。"""
    from bowhead_core.turn_runner import TurnRunner

    helpers = _turn_runner_helpers()
    _make_orch, _req = helpers._make_orch, helpers._req

    monkeypatch.setenv(_FLAG, "1")
    fake_subagent_runner.results = [make_subagent_result(final_answer="ok")]
    ss = _session_store_with([_turn(1, status="cancelled", user="统计上半年各品牌营业额，写成画布报告", answer="")], [])
    asked: list[tuple[list[str], dict[str, str]]] = []

    async def _list_turn_spawns(turn_ids: list[str], *, incomplete_status: dict[str, str] | None = None) -> dict[str, list[TurnSpawnRecord]]:
        asked.append((list(turn_ids), dict(incomplete_status or {})))
        return {"t1": [_spawn("call_k", status="cancelled", child_tool_count=2, canvas_sections=(), outcome="")]}

    ss.list_turn_spawns = _list_turn_spawns
    orch = _make_orch(subagent_runner=fake_subagent_runner, session_store=ss)
    await TurnRunner(orch).run(_req("改成第二季度"))
    inv = fake_subagent_runner.invocations[0]
    assert asked and asked[0][0] == ["t1"] and asked[0][1] == {"t1": "cancelled"}
    hist = inv["history"]
    assert [m.role for m in hist] == ["user", "assistant", "tool", "assistant"]
    assert hist[0].content == "统计上半年各品牌营业额，写成画布报告"
    assert hist[1].tool_calls[0].name == "subagent_spawn" and hist[2].content.startswith("cancelled")
    assert hist[3].content == INCOMPLETE_TURN_MARKERS["cancelled"]
    rules = inv.get("history_rules") or ""
    assert rules.startswith("## Follow-up rules for this session") and "cancelled" in rules
    assert "## Recent turns in this session" not in (inv.get("extra_instructions") or "")


@pytest.mark.asyncio
async def test_lead_installs_final_answer_gate_that_bounces_report_claims_without_spawn(
    monkeypatch: pytest.MonkeyPatch, fake_subagent_runner, make_subagent_result
) -> None:
    """Lead 装了终答闸：零派活 + 声称「见画布」⇒ 退回指令（只一次）；派过活 ⇒ 放行；非 lead ⇒ 放行。"""
    from bowhead_core.turn_runner import TurnRunner

    helpers = _turn_runner_helpers()
    _make_orch, _req = helpers._make_orch, helpers._req

    captured: dict[str, Any] = {}

    class _Runner:
        def __init__(self, inner: Any) -> None:
            self._inner = inner
            self.router = getattr(inner, "router", None)

        async def run(self, **kw: Any) -> Any:
            captured["gate"] = getattr(kw["parent_ctx"], "_final_answer_gate", None)
            captured["ctx"] = kw["parent_ctx"]
            return await self._inner.run(**kw)

        def __getattr__(self, name: str) -> Any:
            return getattr(self._inner, name)

    fake_subagent_runner.results = [make_subagent_result(final_answer="ok")]
    orch = _make_orch(subagent_runner=_Runner(fake_subagent_runner))
    await TurnRunner(orch).run(_req("第二季度"))
    gate = captured.get("gate")
    assert gate is not None, "Lead 没装终答闸"
    spawn_inv = SimpleNamespace(tool_name="subagent_spawn", result={"ok": True, "delivered": "payload"})
    r6 = "口径已就地改为「第二季度」，全报告各段正文与 5 张图均按新口径重算重画。"
    assert await gate(r6, "compute", []) is None
    assert await gate(r6, "lead", [spawn_inv]) is None, "派了活且交付了 ⇒ 放行"
    assert await gate(r6, "lead", [SimpleNamespace(tool_name="subagent_spawn", result="not-a-dict")]) is None, "结果未知 ⇒ 不在拿不准时退"
    assert await gate(r6, "lead", [SimpleNamespace(tool_name="subagent_spawn", result={"ok": True, "delivered": "none"}), spawn_inv]) is None, (
        "有一个交付了就放行"
    )
    assert await gate("第二季度营业额 12 亿。", "lead", []) is None, "没声称改过报告就不退"
    assert await gate("上一轮的报告已在画布，你想改哪一段？", "lead", []) is None, "只指路不算修订声明（review 修）"
    first = await gate(r6, "lead", [])
    assert first and "subagent_spawn" in first
    assert getattr(captured["ctx"], "_lead_narrative_superseded", False) is True, "被退回的旁白已流给前端，必须标 superseded 让渲染层压掉"
    assert await gate("报告已生成，见画布。", "lead", []) is not None, "r10：上限是 2，第二次仍退"
    assert await gate("报告已生成，见画布。", "lead", []) is None, "第三次被上限挡住"


@pytest.mark.unit
def test_unsourced_absolute_figures_separates_fabrication_from_restatement() -> None:
    """r8 判据（116 第六轮真机原文）：带绝对量单位、对话里没出处的数字才算编；复述、推算比率、纯指路都不算。"""
    from bowhead_core.analysis_payload import _written_precision, build_observed_numbers_from_texts, unsourced_absolute_figures

    # Position of the last written significant digit: trailing integer zeros do not imply precision at those positions; decimals use the written places.
    assert [_written_precision(x) for x in ("90", "5000", "5010", "91.17", "1", "0.91", "2,217")] == [10.0, 1000.0, 10.0, 0.01, 1.0, 0.01, 1.0]

    hist = [
        "统计2026年上半年各品牌的营业额，写成一份画布报告",
        "2026 年上半年全渠道 1083 个可比品牌合计营业额 91.17 亿元，同比微增 +0.08%。金辉福以 3.80 亿元稳居第一。",
    ]
    pool = build_observed_numbers_from_texts(hist)
    # t_17a2cb7a3f43: all three figures are fabricated (actual value 3.80 亿), but the final year 2026 matches; the criterion cannot require all numbers to be unmatched.
    assert unsourced_absolute_figures("金辉福约 5010 万。阿迪达斯（约 2217 万）、哥弟（约 2066 万）。这是 2026 上半年前三名", pool) == [
        50100000.0,
        22170000.0,
        20660000.0,
    ]
    # t_8b05d38e92c7: one fabricated figure plus one matching year
    assert unsourced_absolute_figures("排名第七的品牌是银凤阁，营业额约 1.63 亿元（据 2026 年上半年数据）", pool) == [163000000.0]
    # E1 / E4: restate historical figures
    assert unsourced_absolute_figures("营业额最高的是金辉福，2026 年上半年 3.80 亿元。", pool) == []
    assert unsourced_absolute_figures("2026 年上半年全品牌合计约 91.17 亿元。", pool) == []
    # Exclude derived ratios (shares / growth can be computed from two conversational figures); plain directions contain no numbers.
    assert unsourced_absolute_figures("金辉福占全盘约 4.17%，前十合计 13.8%。", pool) == []
    assert unsourced_absolute_figures("报告就在画布上，点右侧面板即可。", pool) == []
    # Restatements can round or use approximating words; these remain the same facts and must not be rejected (six false positives observed in review).
    for legit in (
        "全渠道合计超过 90 亿元。",  # Written to the billion place ⇒ tolerance ±5 亿; the pool's 91.17 亿 falls within it.
        "合计近 91 亿元。",
        "远航约 0.91 亿元。",
        "第一名 3.8 亿元。",
        "前十里最小的一家约 9 千万元。",  # r9: `千万` now parses correctly (9e7), within 2% of the pool's 9069 万.
        "接近 1 亿元量级。",  # Written to the hundred-million place ⇒ tolerance ±0.5 亿; the pool's 9069 万 falls within it.
    ):
        assert unsourced_absolute_figures(legit, pool | {90690000.0}) == [], legit
    # ★ Equally coarse integers still count if far from every pooled number; the first version wrongly exempted them by skipping fewer than two significant digits.
    assert unsourced_absolute_figures("华中 5000 万元，排在最后。", pool | {90690000.0}) == [50000000.0]
    assert unsourced_absolute_figures("华中 2 亿元，排在最后。", pool | {90690000.0}) == [200000000.0]
    # The Han character immediately after the unit determines money: `元` means money; other Han characters count other things (or form compound units); punctuation/end count as money.
    for counted in (
        "这张表大概 12 万行。",
        "样本量 25 万条。",
        "报告正文大约 1.2 万字。",
        "单次上传上限 100 万 token。",  # An English letter immediately after the unit ⇒ counting something else
    ):
        assert unsourced_absolute_figures(counted, pool) == [], counted
    # ⚠ Known false positive since r9: correct `万亿` parsing flags unsourced general/macroeconomic figures, like 「中国人口约 14 亿」.
    # It costs only one rejection; on the second attempt the model explains this is general knowledge, not workspace data. These sentences are rare in BI; add no semantic classifier.
    assert unsourced_absolute_figures("国内零售市场规模大约 47 万亿元。", pool) == [47000000000000.0]
    # Actual fabrications remain too far away for tolerance: 5010万 vs 3.80亿 differs by 87%; 1.63亿 vs 9069万 by 80%.
    # (The observed sentence 「营业额约 5010 万。」 lacks `元`; the criterion therefore cannot require `元`.)
    assert unsourced_absolute_figures("金辉福约 5010 万。", pool) == [50100000.0]
    assert unsourced_absolute_figures("银凤阁约 1.63 亿元。", pool) == [163000000.0]


@pytest.mark.unit
def test_multi_char_magnitude_units_are_parsed_and_cross_unit_restatement_matches() -> None:
    """r9（116 第七轮真机 `t_7b1b7bc0fac8`）：Lead 把第七名编成「银凤阁，约 118 百万元」——
    `百万` 整个不在 r8 的单位表里，闸看不见就放过去了。而 `百万元 / ¥M` 是本产品自己的规范写法。
    多字单位（万亿 / 百万 / 千万）现在最长优先解析，池子与判据两侧共用同一套 ⇒ 跨单位复述也能对上。"""
    from bowhead_core.analysis_payload import build_observed_numbers_from_texts, unsourced_absolute_figures

    # History uses 「百万元」 and the answer restates in 「亿元」; normalization makes both the same number.
    pool_m = build_observed_numbers_from_texts(["金辉福以约 379.6 百万元断层领先。"])
    assert unsourced_absolute_figures("金辉福 3.80 亿元。", pool_m) == []
    assert unsourced_absolute_figures("金辉福约 379.6 百万元。", pool_m) == []
    # The observed fabrication: 118 百万 = 1.18 亿, absent from the pool.
    assert unsourced_absolute_figures("排名第七的是银凤阁，约 118 百万元。", pool_m) == [118000000.0]
    # Reverse direction: history uses 「亿元」 and the answer uses 「百万元」.
    pool_y = build_observed_numbers_from_texts(["全渠道合计 91.17 亿元。"])
    assert unsourced_absolute_figures("合计 9117 百万元。", pool_y) == []
    # Still exclude counting units (a different noun follows `万`).
    assert unsourced_absolute_figures("这张表 12 万行，样本 25 万条。", pool_y) == []


@pytest.mark.unit
def test_currency_suffix_notation_is_parsed_both_directions() -> None:
    """r10（116 第八轮真机）：`¥379.55M` 是画布 KPI 卡的默认渲染，用户说一句「金额用 ¥M 展示」后 Lead 整段都这么写。
    不认识它会**两个方向都错**：`t_efde3ac984b3` 用 `¥85.49M` 编数没被拦；
    `t_9160f02cec89` 上一轮报告写 `¥9,116.78M`，Lead 如实复述成「91.17 亿元」反被退回。"""
    from bowhead_core.analysis_payload import build_observed_numbers_from_texts, unsourced_absolute_figures

    pool_yM = build_observed_numbers_from_texts(
        ["整体营业额 ¥9,116.78M，金辉福以 ¥379.55M 领跑，六福珠宝（¥137.88M）与阿迪达斯（¥115.49M）分列二、三。"]
    )
    # Missed detection: ¥85.49M has no source in the pool.
    assert unsourced_absolute_figures("排名第七的品牌是银凤阁，营业额 ¥85.49M。", pool_yM) == [85490000.0]
    # False positive: history uses ¥M and the answer uses 亿元 for the same fact.
    assert unsourced_absolute_figures("2026 年上半年各品牌营业额合计 91.17 亿元。", pool_yM) == []
    assert unsourced_absolute_figures("金辉福 3.80 亿元。", pool_yM) == []
    assert unsourced_absolute_figures("合计 ¥9,116.78M。", pool_yM) == []
    # ⚠ Require a currency symbol: bare `3M` is a brand name; `1.2M` may be a version number.
    assert unsourced_absolute_figures("我们和 3M 公司谈了合作，另有 1.2M 版本。", pool_yM) == []
    # Exclude an immediately following letter (such as `¥5MB`).
    assert unsourced_absolute_figures("缓存上限 ¥5MB。", pool_yM) == []
    # A currency symbol overrides the trailing-character rule: `居` in `¥3.80 亿居首` would otherwise mean counting something else.
    assert unsourced_absolute_figures("银凤阁 ¥8.55 亿居首。", pool_yM) == [855000000.0000001]
    # Report the same number in two notations only once (do not double the logged n=).
    assert unsourced_absolute_figures("第七名 2.11 亿元（¥211M）。", pool_yM) == [211000000.0]


@pytest.mark.unit
def test_pool_accepts_bare_kmb_but_the_predicate_still_requires_a_currency_symbol() -> None:
    """r10 修复轮（review 量了本 sprint 语料：6% 的 K/M/B 金额没有货币符号）：Lead 写列表时 `¥` 只写一次、
    后面几项裸着写。**证据池必须收裸 `N[KMB]`，判据侧必须不收**——池子多收只会漏判、不会冤枉；
    判据侧一旦放宽，裸 `3M`（公司名）、`1.2M`（版本号）就会被当成钱。两侧不对称是刻意的。"""
    from bowhead_core.analysis_payload import build_observed_numbers_from_texts, unsourced_absolute_figures

    pool_bare = build_observed_numbers_from_texts(
        ["金辉福以 148.3 百万元居首；六福珠宝（56.7M，-0.03%）、阿迪达斯（54.3M）、哥弟（52.0M）、耐克（49.1M）。"]
    )
    # History with bare M ⇒ allow restatements in all three notations.
    assert unsourced_absolute_figures("六福珠宝 ¥56.7M。", pool_bare) == []
    assert unsourced_absolute_figures("六福珠宝 0.567 亿元。", pool_bare) == []
    assert unsourced_absolute_figures("阿迪达斯 5430 万元。", pool_bare) == []
    # Still detect numbers absent from that same history.
    assert unsourced_absolute_figures("银凤阁 ¥85.49M。", pool_bare) == [85490000.0]
    # The detection criterion rejects bare M; otherwise company names and version numbers would become money.
    assert unsourced_absolute_figures("我们和 3M 公司谈了合作，另有 1.2M 版本。", pool_bare) == []
    # Empty pool ⇒ no penalty when sourcing cannot be determined.
    assert unsourced_absolute_figures("营业额 5010 万", set()) == []
    # Text-built pools share normalization with tool results: both 亿 / 万 must match.
    assert build_observed_numbers_from_texts(["合计 91.17 亿元"]) == build_observed_numbers_from_texts(["合计 9117000000"])


@pytest.mark.asyncio
async def test_final_answer_gate_bounces_zero_tool_turn_that_states_unsourced_figures(
    monkeypatch: pytest.MonkeyPatch, fake_subagent_runner, make_subagent_result
) -> None:
    """r8 接线（116 第六轮 C2/F1 的形状）：零工具轮报了对话里没出处的业务数字 ⇒ 退回；
    复述历史里的数、调过工具的轮、历史为空的轮都放行。"""
    from bowhead_core.turn_runner import TurnRunner

    helpers = _turn_runner_helpers()
    _make_orch, _req = helpers._make_orch, helpers._req

    monkeypatch.setenv(_FLAG, "1")
    captured: dict[str, Any] = {}

    class _Runner:
        def __init__(self, inner: Any) -> None:
            self._inner = inner
            self.router = getattr(inner, "router", None)

        async def run(self, **kw: Any) -> Any:
            captured["gate"] = getattr(kw["parent_ctx"], "_final_answer_gate", None)
            captured["ctx"] = kw["parent_ctx"]
            return await self._inner.run(**kw)

        def __getattr__(self, name: str) -> Any:
            return getattr(self._inner, name)

    fake_subagent_runner.results = [make_subagent_result(final_answer="ok")]
    ss = _session_store_with(
        [
            _turn(
                1, user="统计2026年上半年各品牌的营业额，写成一份画布报告", answer="全渠道 1083 个品牌合计营业额 91.17 亿元，金辉福以 3.80 亿元居首。"
            )
        ],
        [],
    )
    orch = _make_orch(subagent_runner=_Runner(fake_subagent_runner), session_store=ss)
    await TurnRunner(orch).run(_req("不用重新查，直接告诉我营业额最高的品牌是哪个"))
    gate = captured["gate"]
    assert gate is not None
    ok_restate = "营业额最高的品牌是金辉福，2026 年上半年营业额 3.80 亿元。"
    made_up = "营业额最高的品牌是金辉福，上半年营业额约 5010 万。阿迪达斯约 2217 万。"
    assert await gate(ok_restate, "lead", []) is None, "复述历史里的数 ⇒ 放行"
    assert await gate("金辉福占全盘约 4.17%。", "lead", []) is None, "推算比率 ⇒ 放行"
    assert await gate(made_up, "lead", [SimpleNamespace(tool_name="bi_query", result={"ok": True})]) is None, "调过工具 ⇒ 不归这条闸"
    bounced = await gate(made_up, "lead", [])
    # `_req` carries no language context, so the bounce directive uses the English fallback.
    from bowhead_core.agents.lead_orchestrator import final_answer_bounce_directive as _directive

    assert bounced == _directive("en-US", cause="zero_evidence_numbers") and "subagent_spawn" in bounced
    assert getattr(captured["ctx"], "_lead_narrative_superseded", False) is True
    assert await gate(made_up, "lead", []) is not None, "r10：上限是 2，第二次仍退"
    assert await gate(made_up, "lead", []) is None, "第三次被上限挡住"


@pytest.mark.asyncio
async def test_each_bounce_cause_fires_once_and_a_turn_bounces_at_most_twice(
    monkeypatch: pytest.MonkeyPatch, fake_subagent_runner, make_subagent_result
) -> None:
    """r10（116 第八轮真机 `t_b4a7d77462a2`）：原来「一轮只退一次」——那一轮第一次退的是**报告声明**，
    模型改口后在第二版答案里编了个数，而闸已经用掉唯一的机会。改成每个 cause 各一次、一轮至多两次。"""
    from bowhead_core.turn_runner import TurnRunner

    helpers = _turn_runner_helpers()
    _make_orch, _req = helpers._make_orch, helpers._req

    monkeypatch.setenv(_FLAG, "1")
    captured: dict[str, Any] = {}

    class _Runner:
        def __init__(self, inner: Any) -> None:
            self._inner = inner
            self.router = getattr(inner, "router", None)

        async def run(self, **kw: Any) -> Any:
            captured["gate"] = getattr(kw["parent_ctx"], "_final_answer_gate", None)
            return await self._inner.run(**kw)

        def __getattr__(self, name: str) -> Any:
            return getattr(self._inner, name)

    fake_subagent_runner.results = [make_subagent_result(final_answer="ok")]
    ss = _session_store_with([_turn(1, user="各品牌营业额", answer="合计 91.17 亿元，金辉福 3.80 亿元。")], [])
    orch = _make_orch(subagent_runner=_Runner(fake_subagent_runner), session_store=ss)
    await TurnRunner(orch).run(_req("最高的是哪个"))
    gate = captured["gate"]
    claim = "全报告各段已按新口径重算重画。"
    made_up = "营业额最高的是金辉福，约 5010 万元。"
    # `_req` carries no language context, so the bounce directive uses the English fallback.
    from bowhead_core.agents.lead_orchestrator import final_answer_bounce_directive as _directive

    first = await gate(claim, "lead", [])
    assert first == _directive("en-US", cause="no_spawn"), "first bounce: the report claim"
    second = await gate(made_up, "lead", [])
    assert second == _directive("en-US", cause="zero_evidence_numbers"), "a different cause still bounces"
    assert await gate("另一个没出处的数：2.02 亿元。", "lead", []) is None, "一轮至多两次——上限是次数，这一条必须被上限挡住"
    # Two consecutive occurrences of the same cause count twice (review: the second fabrication with the same cause escaped; the limit was always two).
    captured["gate2"] = None
    await TurnRunner(orch).run(_req("再问一次"))
    g2 = captured["gate"]
    assert (await g2(made_up, "lead", [])) is not None
    assert (await g2("换一个没出处的数：3.33 亿元。", "lead", [])) is not None, "同 cause 第二次仍要退"
    assert (await g2("再来一个：4.44 亿元。", "lead", [])) is None, "第三次被上限挡住"


@pytest.mark.asyncio
async def test_zero_evidence_gate_is_inert_without_history(monkeypatch: pytest.MonkeyPatch, fake_subagent_runner, make_subagent_result) -> None:
    """首轮（历史为空）：证据池为空 ⇒ 这条分支整条不判，逐字节回到 r8 之前的行为。"""
    from bowhead_core.turn_runner import TurnRunner

    helpers = _turn_runner_helpers()
    _make_orch, _req = helpers._make_orch, helpers._req

    monkeypatch.setenv(_FLAG, "1")
    captured: dict[str, Any] = {}

    class _Runner:
        def __init__(self, inner: Any) -> None:
            self._inner = inner
            self.router = getattr(inner, "router", None)

        async def run(self, **kw: Any) -> Any:
            captured["gate"] = getattr(kw["parent_ctx"], "_final_answer_gate", None)
            return await self._inner.run(**kw)

        def __getattr__(self, name: str) -> Any:
            return getattr(self._inner, name)

    fake_subagent_runner.results = [make_subagent_result(final_answer="ok")]
    orch = _make_orch(subagent_runner=_Runner(fake_subagent_runner), session_store=_session_store_with([], []))
    # ⚠ The user's original words contain numbers: a nonempty-pool criterion lets this year arm the gate (observed in review),
    # even on the supposedly safest rollback path BOWHEAD_CTX_TURN_HISTORY=0. Test whether a grounding source exists instead.
    await TurnRunner(orch).run(_req("2026 年营业额多少"))
    assert await captured["gate"]("上半年营业额 12 亿。", "lead", []) is None


@pytest.mark.asyncio
async def test_zero_evidence_pool_includes_injected_digest_block(monkeypatch: pytest.MonkeyPatch, fake_subagent_runner, make_subagent_result) -> None:
    """review P1：零工具轮的数字来源不止历史——digest 块（`BOWHEAD_CTX_TURN_HISTORY=0` 的回退面）、
    附件抽取文、记忆包都是本轮注入给模型的内容。它们必须进证据池，否则闸在**本该最安全的回退路上最凶**。
    这里用 digest 通道端到端验：没有历史、只有 digest ⇒ digest 里的数复述放行，别的数仍退。"""
    from bowhead_core.turn_runner import TurnRunner

    helpers = _turn_runner_helpers()
    _make_orch, _req = helpers._make_orch, helpers._req

    monkeypatch.setenv(_FLAG, "1")
    captured: dict[str, Any] = {}

    class _Runner:
        def __init__(self, inner: Any) -> None:
            self._inner = inner
            self.router = getattr(inner, "router", None)

        async def run(self, **kw: Any) -> Any:
            captured["gate"] = getattr(kw["parent_ctx"], "_final_answer_gate", None)
            return await self._inner.run(**kw)

        def __getattr__(self, name: str) -> Any:
            return getattr(self._inner, name)

    fake_subagent_runner.results = [make_subagent_result(final_answer="ok")]
    digests = [{"user_text": "各品牌营业额", "final_answer_full": "全渠道合计 91.17 亿元，金辉福 3.80 亿元。", "answered_via": {"source": "dataset"}}]
    orch = _make_orch(subagent_runner=_Runner(fake_subagent_runner), session_store=_session_store_with([], digests))
    await TurnRunner(orch).run(_req("最高的是哪个"))
    gate = captured["gate"]
    assert await gate("金辉福，3.80 亿元。", "lead", []) is None, "digest 块里的数复述 ⇒ 放行"
    bounced = await gate("金辉福约 5010 万元。", "lead", [])
    # `_req` carries no language context, so the bounce directive uses the English fallback.
    from bowhead_core.agents.lead_orchestrator import final_answer_bounce_directive as _directive

    assert bounced == _directive("en-US", cause="zero_evidence_numbers"), "the digest arms the gate; unsourced numbers still bounce"


@pytest.mark.asyncio
async def test_report_work_claim_wins_over_zero_evidence_cause(monkeypatch: pytest.MonkeyPatch, fake_subagent_runner, make_subagent_result) -> None:
    """review P2：零工具轮**同时**声称改过报告 + 报了没出处的数 ⇒ 用户最需要听到的是「报告没动」，
    只有 no_spawn 那条指令带着「不要说已修订 / 见画布」。顺序必须是报告声明优先。"""
    from bowhead_core.turn_runner import TurnRunner

    helpers = _turn_runner_helpers()
    _make_orch, _req = helpers._make_orch, helpers._req

    monkeypatch.setenv(_FLAG, "1")
    captured: dict[str, Any] = {}

    class _Runner:
        def __init__(self, inner: Any) -> None:
            self._inner = inner
            self.router = getattr(inner, "router", None)

        async def run(self, **kw: Any) -> Any:
            captured["gate"] = getattr(kw["parent_ctx"], "_final_answer_gate", None)
            return await self._inner.run(**kw)

        def __getattr__(self, name: str) -> Any:
            return getattr(self._inner, name)

    fake_subagent_runner.results = [make_subagent_result(final_answer="ok")]
    ss = _session_store_with([_turn(1, user="各品牌营业额", answer="合计 91.17 亿元。")], [])
    orch = _make_orch(subagent_runner=_Runner(fake_subagent_runner), session_store=ss)
    await TurnRunner(orch).run(_req("改成第二季度"))
    both = "全报告各段已按新口径重算重画，第二季度合计 5010 万元。"
    d = await captured["gate"](both, "lead", [])
    # `_req` carries no language context, so the bounce directive uses the English fallback.
    from bowhead_core.agents.lead_orchestrator import final_answer_bounce_directive as _directive

    assert d == _directive("en-US", cause="no_spawn"), "the report-claim directive must win"


@pytest.mark.asyncio
async def test_zero_evidence_gate_respects_the_rollback_flag(monkeypatch: pytest.MonkeyPatch, fake_subagent_runner, make_subagent_result) -> None:
    """回滚绳与 r4 闸共用：BOWHEAD_LEAD_FINAL_ANSWER_GATE=0 ⇒ 这条也不判。"""
    from bowhead_core.turn_runner import TurnRunner

    helpers = _turn_runner_helpers()
    _make_orch, _req = helpers._make_orch, helpers._req

    monkeypatch.setenv(_FLAG, "1")
    monkeypatch.setenv("BOWHEAD_LEAD_FINAL_ANSWER_GATE", "0")
    captured: dict[str, Any] = {}

    class _Runner:
        def __init__(self, inner: Any) -> None:
            self._inner = inner
            self.router = getattr(inner, "router", None)

        async def run(self, **kw: Any) -> Any:
            captured["gate"] = getattr(kw["parent_ctx"], "_final_answer_gate", None)
            return await self._inner.run(**kw)

        def __getattr__(self, name: str) -> Any:
            return getattr(self._inner, name)

    fake_subagent_runner.results = [make_subagent_result(final_answer="ok")]
    ss = _session_store_with([_turn(1, user="各品牌营业额", answer="合计 91.17 亿元，金辉福 3.80 亿元。")], [])
    orch = _make_orch(subagent_runner=_Runner(fake_subagent_runner), session_store=ss)
    await TurnRunner(orch).run(_req("最高的是哪个"))
    assert await captured["gate"]("金辉福，约 5010 万。阿迪达斯约 2217 万。", "lead", []) is None


# ── r4 final-answer gate: when the model finishes without tools, parent_ctx's gate can send it back through another iteration ──


@pytest.mark.unit
@pytest.mark.asyncio
async def test_final_answer_gate_bounces_once_and_the_second_answer_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOWHEAD_CTX_PREFIX_ORDER", "1")
    adapter = MockAdapter(provider_name="p1")
    adapter.scripted_responses = [
        LLMResponse(content="口径已改，完整报告见画布。", usage=TokenUsage(input_tokens=10, output_tokens=5)),
        LLMResponse(content="真答", usage=TokenUsage(input_tokens=10, output_tokens=5)),
    ]
    reg = ProviderRegistry()
    reg.load([ProviderConfig(name="p1", type="mock")])
    reg._adapters["p1"] = adapter  # type: ignore[attr-defined]
    router = _CaptureRouter(reg)
    for t in ("lead", "analyst", "compute", "narrative", "echo"):
        router.bind(AgentBinding(agent_type=t, primary=("p1", "mock")))
    runner = SubagentRunner(router=router, tool_registry=build_default_registry(), backend="legacy")
    ctx = ToolExecutionContext(workspace_id="w", member_uid="m", user_id="u", turn_id="t", datarc=None)  # type: ignore[arg-type]
    seen: list[tuple[str, str, int]] = []

    async def _gate(text: str, profile_name: str, invocations: list[Any]) -> str | None:
        seen.append((text, profile_name, len(invocations)))
        return "DIRECTIVE_MARKER" if len(seen) == 1 else None

    ctx._final_answer_gate = _gate  # type: ignore[attr-defined]
    result = await runner.run(profile_name="compute", task="TASK", parent_turn_id="t", parent_bus=TurnEventBus(), parent_ctx=ctx)
    assert result.final_answer == "真答"
    assert [s_[1] for s_ in seen] == ["compute", "compute"] and seen[0][0].startswith("口径已改")
    assert len(router.calls) == 2, "退回后必须再走一圈"
    second = router.calls[1][1]
    assert ("assistant", "口径已改，完整报告见画布。") in second, "被退回的终答作为 assistant 发言留在历史里"
    assert ("user", "DIRECTIVE_MARKER") in second, "指令作为 user 消息追加"
    assert second.index(("assistant", "口径已改，完整报告见画布。")) < second.index(("user", "DIRECTIVE_MARKER"))


@pytest.mark.unit
@pytest.mark.asyncio
async def test_final_answer_gate_absent_or_permissive_changes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOWHEAD_CTX_PREFIX_ORDER", "1")

    async def _one(gate: Any) -> tuple[str, int]:
        adapter = MockAdapter(provider_name="p1")
        adapter.scripted_responses = [LLMResponse(content="直答", usage=TokenUsage(input_tokens=10, output_tokens=5))]
        reg = ProviderRegistry()
        reg.load([ProviderConfig(name="p1", type="mock")])
        reg._adapters["p1"] = adapter  # type: ignore[attr-defined]
        router = _CaptureRouter(reg)
        for t in ("lead", "analyst", "compute", "narrative", "echo"):
            router.bind(AgentBinding(agent_type=t, primary=("p1", "mock")))
        runner = SubagentRunner(router=router, tool_registry=build_default_registry(), backend="legacy")
        ctx = ToolExecutionContext(workspace_id="w", member_uid="m", user_id="u", turn_id="t", datarc=None)  # type: ignore[arg-type]
        if gate is not None:
            ctx._final_answer_gate = gate  # type: ignore[attr-defined]
        r = await runner.run(profile_name="compute", task="TASK", parent_turn_id="t", parent_bus=TurnEventBus(), parent_ctx=ctx)
        return r.final_answer, len(router.calls)

    async def _permissive(text: str, profile_name: str, invocations: list[Any]) -> str | None:
        return None

    async def _broken(text: str, profile_name: str, invocations: list[Any]) -> str | None:
        raise RuntimeError("gate exploded")

    assert await _one(None) == ("直答", 1)
    assert await _one(_permissive) == ("直答", 1)
    assert await _one(_broken) == ("直答", 1), "闸自己炸了不能弄挂 turn"


# ── Review fixes: marker stripping / variable turn-size derivation / first guard preserves history / both adapters accept this shape / streaming rejection ──


@pytest.mark.unit
def test_spawn_rendering_strips_citation_markers_from_task_and_outcome() -> None:
    """peer 的 exec_summary 会被自动追加 `[cite:N]`、Lead 的任务里可能有 `[ref:N]`——那是上一轮的编号，进历史会被本轮当引用。"""
    rec = _spawn(
        task="按 [ref:3] 的口径重算", rationale="用户改口径 [cite:1]", outcome="营业额 92.94 亿 [cite:2]", failure_reason="peer_timeout [cite:9]"
    )
    args = render_spawn_arguments(rec, strip=_strip_markers)
    assert "[ref:" not in args["task"] and "[cite:" not in args["rationale"]
    text = render_spawn_result(rec, strip=_strip_markers)
    assert "[cite:" not in text and "92.94 亿" in text and "peer_timeout" in text
    th = build_turn_history([_turn(1)], exclude_turn_id=None, budget_tokens=0, strip=_strip_markers, spawns={"t1": [rec]})
    assert all("[cite:" not in json.dumps(m, ensure_ascii=False) for m in th.messages)


@pytest.mark.unit
def test_spawn_arguments_carry_facts_only_never_mode_labels() -> None:
    """r5（116 第二轮 dogfood A/H 对照）：`canvas_mode` / `delivery` 是分类标签，渲进历史就被当模板抄——
    前两轮 append ⇒ 裸口径 3/3 append，前一轮 revise ⇒ 3/3 revise。记录保留字段，历史不渲。"""
    rec = _spawn(delivery="report", canvas_mode="revise")
    args = render_spawn_arguments(rec)
    assert list(args.keys()) == ["profile", "rationale", "task"]
    assert "revise" not in json.dumps(args, ensure_ascii=False) and "report" not in json.dumps(args, ensure_ascii=False)
    assert rec.canvas_mode == "revise" and rec.delivery == "report", "落库事实仍在记录上"


@pytest.mark.unit
def test_turn_sizes_are_derived_from_the_messages_themselves() -> None:
    th = build_turn_history(
        [_turn(1), _turn(2), _turn(3)], exclude_turn_id=None, budget_tokens=0, spawns={"t1": [_spawn("a"), _spawn("b")], "t3": [_spawn("c")]}
    )
    msgs = [Message(role=m["role"], content=m["content"], tool_call_id=m.get("tool_call_id"), name=m.get("name")) for m in th.messages]
    assert turn_sizes_from_messages(msgs) == th.turn_sizes == [5, 2, 4]
    assert turn_sizes_from_messages([]) == []


@pytest.mark.unit
def test_layer_one_never_clears_history_tool_messages() -> None:
    """历史里的 spawn 摘要不归第一层清——占位文案「不要重跑同一个工具」对它读起来是「别再派活」；历史超线由第二层按整轮掉。"""
    est = len
    hist_tool = Message(role="tool", content="succeeded｜子工具 16 次｜画布写入 2 节：" + "x" * 300, tool_call_id="h1_c", name="subagent_spawn")
    msgs = [
        Message(role="user", content="问题1"),
        Message(role="assistant", content="", tool_calls=[]),
        hist_tool,
        Message(role="assistant", content="回答1"),
        Message(role="user", content="TASK"),
        Message(role="assistant", content="", tool_calls=[]),
        Message(role="tool", content="y" * 400, tool_call_id="c_now", name="bi_query"),
        Message(role="assistant", content="收尾"),
    ]
    cleared = guard_window(msgs, budget_tokens=500, estimate=est, hysteresis=1.0, protect_first=4)
    assert cleared > 0
    assert msgs[2].content == hist_tool.content and "x" * 300 in msgs[2].content, "历史 tool 消息原样"
    assert "已移出上下文" in msgs[6].content, "本轮的工具结果才被清"


@pytest.mark.unit
def test_history_shape_round_trips_through_both_adapter_faces() -> None:
    """[user, assistant(tool_calls, content=\"\"), tool, assistant, user] 在 OpenAI chat 面 / Responses 面 / Anthropic 面都能收：
    配对靠 id、顺序保持、不出现孤儿 tool 结果。"""
    from bowhead_router.adapters.anthropic_compat import AnthropicCompatibleAdapter
    from bowhead_router.adapters.openai_compat import OpenAICompatibleAdapter
    from bowhead_router.types import ToolCall

    th = build_turn_history([_turn(1)], exclude_turn_id=None, budget_tokens=0, spawns={"t1": [_spawn()]})
    hist = [
        Message(
            role=m["role"],
            content=m["content"],
            tool_call_id=m.get("tool_call_id"),
            name=m.get("name"),
            tool_calls=[ToolCall(id=c["id"], name=c["name"], arguments=c["arguments"]) for c in m["tool_calls"]] if m.get("tool_calls") else None,
        )
        for m in th.messages
    ]
    msgs = [*hist, Message(role="user", content="TASK")]

    chat = OpenAICompatibleAdapter._convert_messages(OpenAICompatibleAdapter.__new__(OpenAICompatibleAdapter), msgs, "SYS")
    roles = [m["role"] for m in chat]
    assert roles == ["system", "user", "assistant", "tool", "assistant", "user"], roles
    assert chat[2]["tool_calls"][0]["id"] == "h1_call_1" and chat[3]["tool_call_id"] == "h1_call_1"

    items = OpenAICompatibleAdapter._convert_input_items(OpenAICompatibleAdapter.__new__(OpenAICompatibleAdapter), msgs, has_instructions=True)
    kinds = [(it.get("type") or it.get("role")) for it in items]
    assert "function_call" in kinds and "function_call_output" in kinds, kinds
    fc = next(it for it in items if it.get("type") == "function_call")
    fco = next(it for it in items if it.get("type") == "function_call_output")
    assert fc["call_id"] == fco["call_id"] == "h1_call_1" and kinds.index("function_call") < kinds.index("function_call_output")

    anth = AnthropicCompatibleAdapter._convert_messages(msgs)
    aroles = [m["role"] for m in anth]
    assert aroles == ["user", "assistant", "user", "assistant", "user"], aroles
    tool_use = next(b for b in anth[1]["content"] if b.get("type") == "tool_use")
    tool_result = next(b for b in anth[2]["content"] if b.get("type") == "tool_result")
    assert tool_use["id"] == tool_result["tool_use_id"] == "h1_call_1"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_final_answer_gate_bounces_on_the_streaming_path_too(monkeypatch: pytest.MonkeyPatch) -> None:
    """Lead 生产恒走流式（stream_callback 非空）：被退回的答案已经流出去了，闸仍要在那条路上退回；流出去的文本由 Lead 侧标 superseded 压掉。"""
    monkeypatch.setenv("BOWHEAD_CTX_PREFIX_ORDER", "1")
    adapter = MockAdapter(provider_name="p1")
    adapter.scripted_responses = [
        LLMResponse(content="全报告各段均按新口径重算重画。", usage=TokenUsage(input_tokens=10, output_tokens=5)),
        LLMResponse(content="真答", usage=TokenUsage(input_tokens=10, output_tokens=5)),
    ]
    reg = ProviderRegistry()
    reg.load([ProviderConfig(name="p1", type="mock")])
    reg._adapters["p1"] = adapter  # type: ignore[attr-defined]
    router = _CaptureRouter(reg)
    for t in ("lead", "analyst", "compute", "narrative", "echo"):
        router.bind(AgentBinding(agent_type=t, primary=("p1", "mock")))
    runner = SubagentRunner(router=router, tool_registry=build_default_registry(), backend="legacy")
    ctx = ToolExecutionContext(workspace_id="w", member_uid="m", user_id="u", turn_id="t", datarc=None)  # type: ignore[arg-type]
    # In production only Lead streams when stream_callback exists; the test registry defaults to compute, so temporarily register lead.
    from bowhead_core.subagent import PROFILES, SubagentProfile

    _added_lead = "lead" not in PROFILES
    if _added_lead:
        PROFILES["lead"] = SubagentProfile(
            name="lead", llm_agent_type="lead", tool_whitelist=("subagent_spawn",), system_prompt="(stub lead)", max_iterations=4
        )
    streamed: list[str] = []

    async def _cb(delta: str) -> None:
        streamed.append(delta)

    bounced = {"n": 0}

    async def _gate(text: str, profile_name: str, invocations: list[Any]) -> str | None:
        bounced["n"] += 1
        return "DIRECTIVE_MARKER" if bounced["n"] == 1 else None

    ctx._final_answer_gate = _gate  # type: ignore[attr-defined]
    try:
        result = await runner.run(
            profile_name="lead", task="TASK", parent_turn_id="t", parent_bus=TurnEventBus(), parent_ctx=ctx, stream_callback=_cb
        )
    finally:
        if _added_lead:
            PROFILES.pop("lead", None)
    assert result.final_answer.strip() == "真答" and bounced["n"] == 2 and len(router.calls) == 2
    assert "".join(streamed).startswith("全报告各段均按新口径重算重画"), "被退回的答案确实已经流出去了——所以 Lead 侧必须标 superseded"
    second = router.calls[1][1]
    assert ("user", "DIRECTIVE_MARKER") in second
    bounced_idx = next(i for i, (r, c) in enumerate(second) if r == "assistant" and c.strip() == "全报告各段均按新口径重算重画。")
    assert second.index(("user", "DIRECTIVE_MARKER")) > bounced_idx
    assert second[-1] == ("user", "DIRECTIVE_MARKER"), "指令排最末吃 recency（与 hard-bail 同款，review 修）"


@pytest.mark.unit
def test_report_work_claim_predicate_matches_revision_verbs_but_not_pointers() -> None:
    from bowhead_core.overclaim_guard import detect_report_work_claims

    assert detect_report_work_claims("口径已就地改为「第二季度」，全报告各段正文与 5 张图均按新口径重算重画。")
    assert detect_report_work_claims("两段报告已更新并放上画布。")
    assert detect_report_work_claims("报告已生成，见画布。")
    assert detect_report_work_claims("第二章已修订。")
    assert not detect_report_work_claims("上一轮的报告已在画布，你想改哪一段？")
    assert not detect_report_work_claims("完整报告见画布。")
    assert not detect_report_work_claims("第二季度营业额 12 亿，同比已更新为 +3%。"), "泛「已更新」不紧邻报告名词时不算"
    assert not detect_report_work_claims("")


# ── r5: this turn's context manifest (pinned datasets / attachments / hints) belongs in the tail block, no longer the stable system prompt ──


@pytest.mark.unit
def test_split_puts_turn_context_block_in_the_tail_after_rules(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOWHEAD_CTX_PREFIX_ORDER", "1")
    sp = split_system_for_prefix(
        profile_prompt="P",
        extra_instructions_block="E",
        memory_block="M",
        language_block="\nLANG_BLOCK",
        extra_is_stable=True,
        turn_rules_block="RULES",
        turn_context_block="## Available pinned datasets (snapshot at turn start)\n- x",
    )
    assert "pinned datasets" not in sp.stable_system, "本轮清单不进稳定前缀"
    assert sp.turn_tail.index("RULES") < sp.turn_tail.index("pinned datasets")
    assert sp.turn_tail_language == "LANG_BLOCK"
    # Byte-identical when empty
    a = split_system_for_prefix(profile_prompt="P", extra_instructions_block="E", memory_block="", language_block="", extra_is_stable=True)
    b = split_system_for_prefix(
        profile_prompt="P",
        extra_instructions_block="E",
        memory_block="",
        language_block="",
        extra_is_stable=True,
        turn_context_block="",
    )
    assert a == b


@pytest.mark.unit
def test_split_flag_off_keeps_turn_context_in_system(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOWHEAD_CTX_PREFIX_ORDER", "0")
    sp = split_system_for_prefix(
        profile_prompt="P",
        extra_instructions_block="E",
        memory_block="",
        language_block="",
        turn_context_block="CTX",
    )
    assert "CTX" in sp.stable_system and sp.turn_tail == ""


@pytest.mark.unit
@pytest.mark.asyncio
async def test_wiring_turn_context_block_lands_after_task_not_in_system(monkeypatch: pytest.MonkeyPatch) -> None:
    system, msgs = await _run(monkeypatch, turn_context_block="PINNED_MARKER")
    assert "PINNED_MARKER" not in system
    task_idx = next(i for i, (_, c) in enumerate(msgs) if c == "TASK_MARKER")
    ctx_idx = next(i for i, (_, c) in enumerate(msgs) if "PINNED_MARKER" in c)
    assert ctx_idx > task_idx


def _pinned_harness(monkeypatch: pytest.MonkeyPatch, fake_subagent_runner: Any, make_subagent_result: Any) -> Any:
    """借 test_turn_runner_unit 的 M1 pinned 夹具形状：session_store.list_pinned_attachments + orch.artifact_store.get。"""
    from bowhead_core.artifact_store import Artifact

    helpers = _turn_runner_helpers()
    fake_subagent_runner.results = [make_subagent_result(final_answer="ok")]
    orch = helpers._make_orch(subagent_runner=fake_subagent_runner)
    art = Artifact(
        id="art_pin1",
        filename="q.csv",
        mime="text/csv",
        size=10,
        source="bi_query",
        path="/x",
        canvas_id=None,
        created_by=None,
        created_at="2026",
        kind="tool_data",
        inject="list",
    )

    async def _get(aid: str, **kw: Any) -> Any:
        return art if aid == "art_pin1" else None

    async def _list_pinned(**kw: Any) -> list[str]:
        return ["art_pin1"]

    orch.artifact_store = SimpleNamespace(get=_get)
    orch.session_store.list_pinned_attachments = _list_pinned
    return orch


@pytest.mark.asyncio
async def test_lead_moves_pinned_block_out_of_system_when_prefix_order_on(
    monkeypatch: pytest.MonkeyPatch, fake_subagent_runner, make_subagent_result
) -> None:
    """116 实测：pinned 清单拼在 lead_prompt 末尾（= 稳定前缀）⇒ system 每轮 +450 字、跨轮必 miss。flag ON 走 turn_context_block。"""
    from bowhead_core.turn_runner import TurnRunner

    monkeypatch.setenv("BOWHEAD_CTX_PREFIX_ORDER", "1")
    monkeypatch.setenv("BOWHEAD_PSI_PIN_INJECT", "1")
    orch = _pinned_harness(monkeypatch, fake_subagent_runner, make_subagent_result)
    await TurnRunner(orch).run(_turn_runner_helpers()._req("上半年营业额"))
    inv = fake_subagent_runner.invocations[0]
    assert "Available pinned datasets" not in (inv.get("extra_instructions") or ""), "清单不该再在稳定 system 里"
    assert "Available pinned datasets" in (inv.get("turn_context_block") or "") and "art_pin1" in inv["turn_context_block"]


@pytest.mark.asyncio
async def test_lead_keeps_pinned_block_in_prompt_when_prefix_order_off(
    monkeypatch: pytest.MonkeyPatch, fake_subagent_runner, make_subagent_result
) -> None:
    """回滚绳：flag OFF 逐字节回到旧拼法（清单在 lead_prompt 里，不传尾块）。"""
    from bowhead_core.turn_runner import TurnRunner

    monkeypatch.setenv("BOWHEAD_CTX_PREFIX_ORDER", "0")
    monkeypatch.setenv("BOWHEAD_PSI_PIN_INJECT", "1")
    orch = _pinned_harness(monkeypatch, fake_subagent_runner, make_subagent_result)
    await TurnRunner(orch).run(_turn_runner_helpers()._req("上半年营业额"))
    inv = fake_subagent_runner.invocations[0]
    assert "Available pinned datasets" in (inv.get("extra_instructions") or "")
    assert not inv.get("turn_context_block")


@pytest.mark.asyncio
async def test_final_answer_gate_bounces_when_every_spawn_delivered_nothing(
    monkeypatch: pytest.MonkeyPatch, fake_subagent_runner, make_subagent_result
) -> None:
    """r6：派了活但每个 spawn 都没交付（失败 / delivered=none），终答仍说「已修订」⇒ 退回，指令是「没交付」那套。
    116 当天实例：派了 4 个工具、画布 0 节、收尾守卫只加了注脚。"""
    from bowhead_core.agents.lead_orchestrator import final_answer_bounce_directive
    from bowhead_core.turn_runner import TurnRunner

    helpers = _turn_runner_helpers()
    _make_orch, _req = helpers._make_orch, helpers._req
    captured: dict[str, Any] = {}

    class _Runner:
        def __init__(self, inner: Any) -> None:
            self._inner = inner
            self.router = getattr(inner, "router", None)

        async def run(self, **kw: Any) -> Any:
            captured["gate"] = getattr(kw["parent_ctx"], "_final_answer_gate", None)
            return await self._inner.run(**kw)

        def __getattr__(self, name: str) -> Any:
            return getattr(self._inner, name)

    fake_subagent_runner.results = [make_subagent_result(final_answer="ok")]
    orch = _make_orch(subagent_runner=_Runner(fake_subagent_runner))
    await TurnRunner(orch).run(_req("第二季度"))
    gate = captured["gate"]
    failed = SimpleNamespace(tool_name="subagent_spawn", result={"ok": False, "error": "peer_timeout"})
    empty = SimpleNamespace(tool_name="subagent_spawn", result={"ok": True, "delivered": "none", "payload_summary": {"exec_summary": "取数超时"}})
    out = await gate("报告已按新口径修订完成，完整报告见画布。", "lead", [failed, empty])
    # `_req` carries no language context, so the bounce directive uses the English fallback.
    assert out == final_answer_bounce_directive("en-US", cause="spawn_delivered_nothing")
    assert "delivered nothing" in out and "subagent_spawn" not in out.split(".")[0]
    assert await gate("报告已按新口径修订完成。", "lead", [failed]) is not None, "r10：上限是 2，第二次仍退"
    assert await gate("报告已按新口径修订完成。", "lead", [failed]) is None, "第三次被上限挡住"


@pytest.mark.unit
def test_bounce_directive_has_two_causes() -> None:
    from bowhead_core.agents.lead_orchestrator import final_answer_bounce_directive

    a = final_answer_bounce_directive("zh-CN", cause="no_spawn")
    b = final_answer_bounce_directive("zh-CN", cause="spawn_delivered_nothing")
    assert a != b and "没有派任何 subagent" in a and "没有交付任何内容" in b
    assert "delivered nothing" in final_answer_bounce_directive("en-US", cause="spawn_delivered_nothing")


# ── r6 review fixes: any successful canvas-writing tool counts as delivery; the gate ignores spawns directly rejected by their handler ──


def _spawn_handler_result(tool_invocations: list[Any], payload: dict[str, Any] | None, *, profile: str = "compute") -> dict[str, Any]:
    """穿过真 `register_subagent_tool` handler，拿它回给 Lead 的结果 dict。"""
    import asyncio
    import importlib.util
    from pathlib import Path as _P

    from bowhead_core.subagent import PROFILES, SubagentProfile, SubagentResult, register_subagent_tool
    from bowhead_core.tool_registry import ToolRegistry

    spec = importlib.util.spec_from_file_location("_summary_emit_helpers2", _P(__file__).with_name("test_subagent_summary_emit.py"))
    assert spec and spec.loader
    h = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(h)
    added = profile not in PROFILES
    if added:
        PROFILES[profile] = SubagentProfile(
            name=profile,
            llm_agent_type="compute",
            tool_whitelist=("bi_query", "write_report", "subagent_return"),
            system_prompt="(stub)",
            max_iterations=4,
        )

    async def _go() -> dict[str, Any]:
        result = SubagentResult(
            profile=profile,
            subagent_id="x",
            ok=True,
            final_answer="",
            tokens_input=1,
            tokens_output=1,
            duration_ms=1,
            payload=payload,
            tool_invocations=tool_invocations,
        )
        bus = h._FakeBus()
        ctx = h._FakeCtx(bus)
        ctx.subagent_runner = h._FakeRunner(result)
        reg = ToolRegistry()
        register_subagent_tool(reg)
        out = await reg.execute("subagent_spawn", arguments={"profile": profile, "task": "改口径", "rationale": "r"}, ctx=ctx)
        assert out.error is None, out.error
        return out.result

    try:
        return asyncio.run(_go())
    finally:
        if added:
            PROFILES.pop(profile, None)


class _Inv:
    def __init__(self, tool_name: str, result: Any, error: Any = None) -> None:
        self.tool_name, self.result, self.error, self.arguments, self.nested = tool_name, result, error, {}, None


@pytest.mark.unit
def test_thin_revise_payload_with_write_report_counts_as_delivered() -> None:
    """revise 模式 payload 刻意只有 exec_summary；peer 用 write_report 改了段 ⇒ delivered=payload，闸不退。
    （两路 review 独立逮到的 P0：原来只数加图工具，这种轮被判 none，Lead 真的「已修订」被退成「没拿到结果」。）"""
    from bowhead_core.agents.lead_orchestrator import _spawn_delivered_nothing

    invs = [_Inv("bi_query", {"ok": True}), _Inv("write_report", {"ok": True, "section_id": "sec_1", "rev": 3})]
    out = _spawn_handler_result(invs, {"exec_summary": "口径已改为第二季度", "sources": []})
    assert out.get("delivered") == "payload", out
    assert _spawn_delivered_nothing(SimpleNamespace(tool_name="subagent_spawn", result=out)) is False
    out2 = _spawn_handler_result([_Inv("remove_chart", {"ok": True, "section_id": "sec_c1"})], {"exec_summary": "旧图已收起", "sources": []})
    assert out2.get("delivered") == "payload", "只收起旧图的修订轮同样算交付"
    out3 = _spawn_handler_result([_Inv("write_report", {"error": "not_owner"})], {"exec_summary": "", "sources": []})
    assert out3.get("delivered") == "none", "写失败 + 空 payload 仍是 none"
    assert _spawn_delivered_nothing(SimpleNamespace(tool_name="subagent_spawn", result=out3)) is True


@pytest.mark.asyncio
async def test_gate_ignores_handler_rejected_spawns_and_has_a_kill_switch(
    monkeypatch: pytest.MonkeyPatch, fake_subagent_runner, make_subagent_result
) -> None:
    from bowhead_core.turn_runner import TurnRunner

    helpers = _turn_runner_helpers()
    captured: dict[str, Any] = {}

    class _Runner:
        def __init__(self, inner: Any) -> None:
            self._inner = inner
            self.router = getattr(inner, "router", None)

        async def run(self, **kw: Any) -> Any:
            captured["gate"] = getattr(kw["parent_ctx"], "_final_answer_gate", None)
            return await self._inner.run(**kw)

        def __getattr__(self, name: str) -> Any:
            return getattr(self._inner, name)

    fake_subagent_runner.results = [make_subagent_result(final_answer="ok")]
    orch = helpers._make_orch(subagent_runner=_Runner(fake_subagent_runner))
    await TurnRunner(orch).run(helpers._req("第二季度"))
    gate = captured["gate"]
    rejected = SimpleNamespace(tool_name="subagent_spawn", result={"error": "canvas_mode is required", "error_kind": "invalid_args"})
    failed = SimpleNamespace(tool_name="subagent_spawn", result={"ok": False, "error": "peer_timeout"})
    claim = "报告已按新口径修订完成。"
    monkeypatch.setenv("BOWHEAD_LEAD_FINAL_ANSWER_GATE", "0")
    assert await gate(claim, "lead", [rejected, failed]) is None, "回滚绳：置 0 不退"
    monkeypatch.setenv("BOWHEAD_LEAD_FINAL_ANSWER_GATE", "1")
    out = await gate(claim, "lead", [rejected, failed])
    # `_req` carries no language context → English directive.
    assert out and "delivered nothing" in out, "rejected spawns do not count as dispatched; the rest all failed ⇒ bounce"
