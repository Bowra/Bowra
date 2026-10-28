"""turn-latency A —— `<available_refs>` 从 system prompt 移到 messages 最末。

依据（实测，不是推理）：`llm_call.shape` 显示 `sys_sha8` **逐圈全变**而
`sys_head_sha8` 恒定 ⇒ 变化全在 system 尾部，对得上 `_ν15_allowlist.to_prompt_xml()`
每圈 refresh 后拼进 system。后果是 provider 的可缓存前缀被**截断在 allowlist 开始处**：
实测 `cached` 恒为 5,888 ≈ 基座大小、不随对话增长，真实命中率只有 30–35%。
📏 docs/sprints/turn-latency-2026-08-21/五轮采样-116-2026-08-23.md §🎯结案。

本文件钉的是**结构性判据**（服从性只能真机 A/B，本地闸测不出来）：

1. `sys_sha8` 逐圈恒定 —— 这是收益的**因**。★ 配正对照：flag OFF 时它必须逐圈变，
   否则「恒定」可能只是我的 fixture 里 allowlist 压根没长大（闸是死的）。
2. refs 挂在**最末**且**只有一条** —— 插中间/攒多条 = 换个地方犯同样的错。
3. 它**不进 `messages` 本体** —— 否则下一圈历史里带着上一圈的过期清单。
4. 两档下 XML **逐字相同** —— 只换位置不换措辞，否则真机 A/B 分不清是哪一半的功劳。
5. 指令面不得再说它「在 system」（`kb-analyst.md` 那句）—— 说了就是与运行时相反。

⚠ 为什么多圈用例跑的是 `lead` profile：peer 一旦拿到过 tool 结果就改走 `stream()`
（`want_stream`），而 `MockAdapter.stream` 只吐 content、**丢掉 tool_calls** ⇒ 循环第 2 圈
就收工，多圈判据根本走不到。Lead 在不传 `stream_callback` 时恒走 `invoke`，而
refs 注入那段代码**与 profile 无关**（同一个 `SubagentRunner.run` 循环）。
peer + 流式那条出口另有 `test_peer_stream_path_also_carries_refs` 单独钉。
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import bowhead_core.subagent as subagent_mod
import pytest
from bowhead_core.events import TurnEventBus
from bowhead_core.subagent import PROFILES, SubagentProfile, SubagentRunner
from bowhead_core.tool_registry import ToolExecutionContext, build_default_registry

from bowhead_router import (
    AgentBinding,
    LLMResponse,
    MockAdapter,
    ProviderConfig,
    ProviderRegistry,
    Router,
    TokenUsage,
    ToolCall,
)

REFS_OPEN = "<available_refs>"
STUB_BASE = "(稳定的 system 基座——本闸要证明它逐圈一个字节都不变)"


# ─────────────────────────────────────────────────────────────
# Stubs: allowlist **grows every iteration** (actual shape: each peer artifact expands it).
# ─────────────────────────────────────────────────────────────
class _Art:
    def __init__(self, n: int) -> None:
        self.id = f"art_{n:04d}"
        self.filename = f"数据{n}.csv"
        self.source = "bi"
        self.size = 1000 + n


class _GrowingArtifactStore:
    """每被刷一次就多一个 artifact —— 这正是 116 上让 system 逐圈变形的机制。"""

    def __init__(self) -> None:
        self.n = 0

    async def list_for_session(self, *, session_id: str, user_id: str) -> list[_Art]:
        self.n += 1
        return [_Art(i) for i in range(self.n)]


class _StubCanvas:
    id = "canvas_test"


class _StubCanvasStore:
    async def get_canvas_by_session(self, session_id: str) -> _StubCanvas:
        return _StubCanvas()

    async def list_section_ids_by_kind(self, canvas_id: str, *, kind: str) -> set[str]:
        return set()


class _RecordingRouter(Router):
    """逐字记下每次请求的 system 与 messages（role + content）。

    ⚠ **两条出口都要记**：peer 一旦拿到过 tool 结果就改走 `stream()`。只挂 `invoke`
    会让流式那条路完全不在判据里。
    """

    def __init__(self, reg: ProviderRegistry) -> None:
        super().__init__(reg)
        self.systems: list[str] = []
        self.messages: list[list[tuple[str, str]]] = []
        self.paths: list[str] = []  # Whether each request used invoke or stream.

    def _record(self, req: Any, path: str) -> None:
        self.systems.append(req.system or "")
        self.messages.append([(m.role, m.content or "") for m in req.messages])
        self.paths.append(path)

    async def invoke(self, agent_type, req, **kw):  # type: ignore[override]
        self._record(req, "invoke")
        return await super().invoke(agent_type, req, **kw)

    async def stream(self, agent_type, req, **kw):  # type: ignore[override]
        self._record(req, "stream")
        async for chunk in super().stream(agent_type, req, **kw):
            yield chunk


def _router(n_tool_rounds: int) -> _RecordingRouter:
    """前 n 轮返 tool_call（推动循环往下走），最后一轮直答收尾。"""
    a = MockAdapter(provider_name="p1")
    a.scripted_responses = [
        LLMResponse(
            content="",
            tool_calls=[ToolCall(id=f"c{i}", name="kb_search", arguments={"query": f"q{i}"})],
            usage=TokenUsage(input_tokens=100, output_tokens=10, cached_tokens=0),
        )
        for i in range(n_tool_rounds)
    ] + [LLMResponse(content="收尾直答", usage=TokenUsage(input_tokens=100, output_tokens=10, cached_tokens=0))]
    reg = ProviderRegistry()
    reg.load([ProviderConfig(name="p1", type="mock")])
    reg._adapters["p1"] = a  # type: ignore[attr-defined]
    r = _RecordingRouter(reg)
    for t in ("lead", "analyst", "echo"):
        r.bind(AgentBinding(agent_type=t, primary=("p1", "mock")))
    return r


@pytest.fixture()
def _stub_profiles() -> Any:
    """lead / compute 都换成「只白名单一个 kb_search」的极简 profile。

    system_prompt 用固定串，好让「基座逐圈不变」这条判据不受真 prompt 变更影响。
    """
    prev = {n: PROFILES.get(n) for n in ("lead", "compute")}
    for n, agent_type in (("lead", "lead"), ("compute", "analyst")):
        PROFILES[n] = SubagentProfile(
            name=n,
            llm_agent_type=agent_type,
            tool_whitelist=("kb_search",),
            system_prompt=STUB_BASE,
            max_iterations=8,
        )
    try:
        yield
    finally:
        for n, p in prev.items():
            if p is not None:
                PROFILES[n] = p
            else:
                PROFILES.pop(n, None)


def _ctx(turn_id: str) -> ToolExecutionContext:
    return ToolExecutionContext(
        workspace_id="w",
        member_uid="m",
        user_id="u",
        turn_id=turn_id,
        datarc=None,  # type: ignore[arg-type]
        session_id="s_refs",
        canvas_store=_StubCanvasStore(),
        artifact_store=_GrowingArtifactStore(),
    )


async def _run(
    monkeypatch,
    *,
    flag_on: bool,
    profile: str = "lead",
    n_tool_rounds: int = 3,
    ctx: Any = None,
    min_rounds: int = 4,
) -> _RecordingRouter:
    monkeypatch.setenv("BOWHEAD_REFS_IN_MESSAGES", "1" if flag_on else "0")
    monkeypatch.setenv("BOWHEAD_REF_ALLOWLIST_INJECT", "1")
    # ⚠ Pin this too: shape logging defaults on, but exporting `=0` silences it entirely;
    # ⑤ then fails with zero log rows, the same environment dependency as the earlier env leak.
    monkeypatch.setenv("BOWHEAD_LLM_SHAPE_LOG", "1")
    # ⚠ Pin bail thresholds too: resolve_peer_bail_iters reads both
    # BOWHEAD_PEER_FORCE_BAIL_ITER and BOWHEAD_ANALYSIS_DEPTH; if force parses to ≤1,
    # run exits via force-bail before soft injection triggers, so ⑧ fails its own positive control.
    monkeypatch.setenv("BOWHEAD_PEER_FORCE_BAIL_ITER", "99")
    monkeypatch.delenv("BOWHEAD_ANALYSIS_DEPTH", raising=False)
    # Without a language_context, language decision v2 adds the fallback language rule in the final
    # tail slot (context_assembly's "language last" contract). These cases measure refs placement
    # without that tail, so pin the switch off.
    monkeypatch.setenv("FEATURE_LANGUAGE_DECISION_V2", "0")
    router = _router(n_tool_rounds)
    runner = SubagentRunner(router=router, tool_registry=build_default_registry(), backend="legacy")

    async def _stub_exec(tc: Any, *args: Any, **kwargs: Any) -> Any:
        return subagent_mod.ToolInvocation(
            tool_name=tc.name,
            arguments=tc.arguments,
            result={"ok": True, "chunks": []},
            error=None,
        )

    runner._execute_tool = _stub_exec  # type: ignore[method-assign]
    await runner.run(
        profile_name=profile,
        task="refs 位置契约测试",
        parent_turn_id="t_refs",
        parent_bus=TurnEventBus(),
        parent_ctx=ctx if ctx is not None else _ctx("t_refs"),
    )
    assert len(router.systems) >= min_rounds, f"循环只跑了 {len(router.systems)} 圈（要 ≥{min_rounds}），逐圈判据无从谈起"
    return router


# ─────────────────────────────────────────────────────────────
# ① Structural gain: system stays constant each iteration (with positive control).
# ─────────────────────────────────────────────────────────────
@pytest.mark.unit
@pytest.mark.asyncio
async def test_flag_off_system_changes_every_iteration(monkeypatch, _stub_profiles) -> None:
    """★正对照：改前（也是默认档）system **必须**逐圈变。

    没有它，下一条的「逐圈恒定」可能只是因为 fixture 里 allowlist 根本没长大——
    闸看起来绿，其实什么都没测到（本 sprint 已经这么栽过）。
    """
    r = await _run(monkeypatch, flag_on=False)
    assert len(set(r.systems)) == len(r.systems), "flag OFF 下 system 居然逐圈相同——fixture 没让 allowlist 长大，后一条判据是死的"
    assert all(REFS_OPEN in s for s in r.systems), "flag OFF 下 refs 本该在 system 里"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_flag_on_system_is_byte_identical_every_iteration(monkeypatch, _stub_profiles) -> None:
    """判据 1（收益的因）：flag ON 后 system 逐圈**逐字相同** ⇒ 可缓存前缀不再被截断。"""
    r = await _run(monkeypatch, flag_on=True)
    assert len(set(r.systems)) == 1, f"system 仍在逐圈变（{len(set(r.systems))} 个不同值）——前缀缓存照样被打断"
    assert REFS_OPEN not in r.systems[0], "refs 还留在 system 里"


# ─────────────────────────────────────────────────────────────
# ② Placement: last, unique, absent from history.
# ─────────────────────────────────────────────────────────────
@pytest.mark.unit
@pytest.mark.asyncio
async def test_refs_message_is_last_and_singular(monkeypatch, _stub_profiles) -> None:
    """判据 2：每次请求里 refs 只有一条，且必须是**最后一条**、role=user。

    ⚠ 插在中间 = 把截断点从 system 挪到 messages，等于换个地方犯同样的错。
    """
    r = await _run(monkeypatch, flag_on=True)
    for i, msgs in enumerate(r.messages):
        hits = [j for j, (_role, c) in enumerate(msgs) if REFS_OPEN in c]
        assert len(hits) == 1, f"第 {i} 圈有 {len(hits)} 条 refs（应恰好 1 条）"
        assert hits[0] == len(msgs) - 1, f"第 {i} 圈 refs 在第 {hits[0]} 条 / 共 {len(msgs)} 条——不在最末"
        assert msgs[hits[0]][0] == "user", f"第 {i} 圈 refs 的 role 是 {msgs[hits[0]][0]}"


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("profile", ["lead", "compute"])
async def test_output_language_role_preserves_reference_data_and_stable_prefix(monkeypatch, _stub_profiles, profile) -> None:
    """Lead keeps runtime rules in system; peers retain their existing tail order."""
    from bowhead_core.language_context import LanguageContext

    ctx = _ctx("t_refs_lang")
    ctx.language_context = LanguageContext(ui_language="zh-CN", output_language="en-US", data_language="zh-CN")
    r = await _run(monkeypatch, flag_on=True, ctx=ctx, profile=profile, min_rounds=2)
    assert len(set(r.systems)) == 1
    for system, msgs in zip(r.systems, r.messages, strict=True):
        refs_at = [j for j, (_role, content) in enumerate(msgs) if REFS_OPEN in content]
        language_at = [j for j, (_role, content) in enumerate(msgs) if "输出语言与术语规范" in content]
        assert len(refs_at) == 1 and msgs[refs_at[0]][0] == "user"
        assert REFS_OPEN not in system
        if profile == "lead":
            assert not language_at
            assert "output_language=en-US" in system
            assert refs_at[0] == len(msgs) - 1
        else:
            assert len(language_at) == 1
            assert language_at[0] > refs_at[0]
            assert language_at[0] == len(msgs) - 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_refs_message_never_enters_history(monkeypatch, _stub_profiles) -> None:
    """判据 3：refs 只进「发出去的那份拷贝」，不进 `messages` 本体。

    落进本体 ⇒ 下一圈历史里带着上一圈的过期清单：既污染上下文，又让前缀
    在历史中段就被打断（收益归零）。判据 = 除最后一条外，历史里零 refs。
    """
    r = await _run(monkeypatch, flag_on=True)
    for i, msgs in enumerate(r.messages):
        for j, (_role, c) in enumerate(msgs[:-1]):
            assert REFS_OPEN not in c, f"第 {i} 圈的历史第 {j} 条里出现了 refs——它落进 messages 本体了"
    # History actually grows (otherwise absent-from-history could mean no history exists).
    assert len(r.messages[-1]) > len(r.messages[0]), "messages 没有增长——本条判据是死的"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_peer_stream_path_also_carries_refs(monkeypatch, _stub_profiles) -> None:
    """peer 的第 2 圈起走 `stream()`——那条出口也必须带着末尾的 refs。

    `MockAdapter.stream` 丢 tool_calls，所以这里只跑得到 2 圈；判据因此只看
    「流式那次请求的形状」，不看多圈稳定性（那条由 lead 用例覆盖）。
    """
    r = await _run(monkeypatch, flag_on=True, profile="compute", n_tool_rounds=1, min_rounds=2)
    # ★ First prove the streaming exit was used; otherwise these three only retest invoke.
    assert "stream" in r.paths, f"没走到流式出口（paths={r.paths}），本条判据是死的"
    i = r.paths.index("stream")
    assert REFS_OPEN in r.messages[i][-1][1], "流式那条出口的请求末尾没有 refs"
    assert r.messages[i][-1][0] == "user"
    assert REFS_OPEN not in r.systems[i], "流式那条出口的 system 里仍有 refs"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_hard_bail_directive_keeps_the_last_slot(monkeypatch, _stub_profiles) -> None:
    """hard-bail「下一步必须立即 subagent_return」是**指令**，靠最末位置吃 recency。

    refs 只是上下文；刚注入 bail 的那一圈必须让位（refs 排它前面）。
    ⚠ 但 refs **不能整圈跳过**：收尾 payload 要引清单里的 id，没清单就只能编，ν1 一律拒。
    突变：把那圈也改成 `[*messages, refs]` ⇒ 本条红。
    """
    monkeypatch.setenv("BOWHEAD_PEER_HARD_BAIL_ITER", "1")
    r = await _run(monkeypatch, flag_on=True, profile="compute", n_tool_rounds=6, min_rounds=2)

    bail_rounds = [i for i, msgs in enumerate(r.messages) if any("强制收尾" in c for _role, c in msgs)]
    assert bail_rounds, "没触发 hard-bail 注入——判据是死的（检查 BOWHEAD_PEER_HARD_BAIL_ITER 是否被读）"
    i = bail_rounds[0]
    msgs = r.messages[i]
    assert "强制收尾" in msgs[-1][1], f"第 {i} 圈最末是 refs 而不是 bail 指令：{[m[0] for m in msgs]}"
    assert REFS_OPEN in msgs[-2][1], "refs 没排在 bail 指令之前——那一圈把清单整个丢了"


# ─────────────────────────────────────────────────────────────
# ②b Acceptance instrument: llm_call.shape must measure what was actually sent.
# ─────────────────────────────────────────────────────────────
@pytest.mark.unit
@pytest.mark.asyncio
async def test_shape_log_measures_what_is_actually_sent(monkeypatch, _stub_profiles, caplog) -> None:
    """真机验收全靠 `llm_call.shape`：`sys_sha8` 恒定 + `msgs_len` 与 `in+cached` 对拍。

    ⚠ 若它量的是 `messages` 本体（漏掉末尾那条 refs），对拍恒对不上，
    而我会把「对不上」误读成 provider 的问题——仪器必须与真实路径同源。
    """
    with caplog.at_level(logging.INFO, logger="bowhead_core.subagent"):
        r = await _run(monkeypatch, flag_on=True)

    lines = [rec.getMessage() for rec in caplog.records if "llm_call.shape" in rec.getMessage()]
    assert len(lines) >= len(r.systems), f"形状日志只有 {len(lines)} 行，请求有 {len(r.systems)} 次"
    for i, line in enumerate(lines[: len(r.systems)]):
        want_msgs_len = sum(len(c) for _role, c in r.messages[i])
        got = int(re.search(r"msgs_len=(\d+)", line).group(1))  # type: ignore[union-attr]
        assert got == want_msgs_len, f"第 {i} 圈 msgs_len={got}，真正发出去的是 {want_msgs_len}——仪器没量到末尾那条 refs"
        assert "refs_loc=msg" in line, f"第 {i} 圈 refs_loc 不是 msg：{line}"
        got_sys = int(re.search(r"sys_len=(\d+)", line).group(1))  # type: ignore[union-attr]
        assert got_sys == len(r.systems[i])


# ─────────────────────────────────────────────────────────────
# ③ Unchanged content: move it without changing wording.
# ─────────────────────────────────────────────────────────────
@pytest.mark.unit
@pytest.mark.asyncio
async def test_xml_is_byte_identical_across_both_modes(monkeypatch, _stub_profiles) -> None:
    """判据 4：同一圈里，两档发出去的 XML **逐字相同**。

    改了措辞就没法把真机 A/B 的服从性差异归到「换了位置」上——那正是这次要测的唯一变量。
    """
    off = await _run(monkeypatch, flag_on=False)
    on = await _run(monkeypatch, flag_on=True)
    assert len(off.systems) == len(on.systems), "两档圈数不同，逐圈对拍不成立"
    for i in range(len(on.systems)):
        # Flag OFF is `system + "\n\n" + xml + "\n\n" + date anchor`; remove the tail before comparison.
        tail = off.systems[i][off.systems[i].index(REFS_OPEN) :]
        xml_off = tail[: tail.rindex("\n\n<current_date>")]
        xml_on = on.messages[i][-1][1]
        assert xml_on == xml_off, f"第 {i} 圈两档的 XML 不一致——换位置的同时把内容也改了"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_history_and_system_base_are_untouched_by_the_move(monkeypatch, _stub_profiles) -> None:
    """搬动只该动 refs：system 基座与整段历史必须两档逐字相同。"""
    off = await _run(monkeypatch, flag_on=False)
    on = await _run(monkeypatch, flag_on=True)
    for i in range(len(on.systems)):
        base_off = off.systems[i][: off.systems[i].index(REFS_OPEN)].rstrip("\n")
        assert on.systems[i].startswith(base_off), f"第 {i} 圈 system 基座被改了"
        assert on.messages[i][:-1] == off.messages[i], f"第 {i} 圈的历史两档不一致"


# ─────────────────────────────────────────────────────────────
# ④ Rollback safeguard
# ─────────────────────────────────────────────────────────────
@pytest.mark.unit
@pytest.mark.asyncio
async def test_default_is_on(monkeypatch, _stub_profiles) -> None:
    """默认档＝**开**（2026-08-25 真机 A/B 之后翻的）：env 完全不设时 refs 应挂 messages 最末。

    ⚠ 这条是「默认值」的闸，和上面那些显式 setenv 的用例不是一回事——
    显式设 `=0`/`=1` 的用例即使默认值被改反也全绿，只有这条会红。
    突变：默认值改回 `"0"` ⇒ 本条红。
    """
    monkeypatch.delenv("BOWHEAD_REFS_IN_MESSAGES", raising=False)
    monkeypatch.setenv("BOWHEAD_REF_ALLOWLIST_INJECT", "1")
    monkeypatch.setenv("BOWHEAD_LLM_SHAPE_LOG", "1")
    router = _router(3)
    runner = SubagentRunner(router=router, tool_registry=build_default_registry(), backend="legacy")

    async def _stub_exec(tc: Any, *args: Any, **kwargs: Any) -> Any:
        return subagent_mod.ToolInvocation(tool_name=tc.name, arguments=tc.arguments, result={"ok": True}, error=None)

    runner._execute_tool = _stub_exec  # type: ignore[method-assign]
    await runner.run(
        profile_name="lead",
        task="默认档",
        parent_turn_id="t_refs_default",
        parent_bus=TurnEventBus(),
        parent_ctx=_ctx("t_refs_default"),
    )
    assert len(router.systems) >= 4
    assert all(REFS_OPEN not in s for s in router.systems), "默认档没生效——refs 还留在 system 里"
    assert len(set(router.systems)) == 1, "默认档下 system 仍逐圈变——收益没拿到"
    for msgs in router.messages:
        assert REFS_OPEN in msgs[-1][1], "默认档下 refs 不在 messages 最末"


# ─────────────────────────────────────────────────────────────
# ⑤ Instructions must not contradict runtime.
# ─────────────────────────────────────────────────────────────
@pytest.mark.unit
def test_no_prompt_claims_refs_live_in_system() -> None:
    """`kb-analyst.md:48` 原话是「N 是 **system 给你的** `<available_refs>`」。

    搬走后不改这句 ⇒ 指令面说了与运行时相反的话（compute 批1 专治的病）。
    改成位置中立的说法后，**两档都成立**，所以这条闸对 flag 的两个状态都该绿。
    """
    root = Path(__file__).resolve().parents[1] / "src" / "bowhead_core" / "agents" / "prompts"
    # "system … available_refs" in one sentence is a violation (no intervening full stop / newline).
    pat = re.compile(r"system[^。\n]{0,30}available_refs")
    offenders = [
        f"{p.relative_to(root)}:{i}"
        for p in sorted(root.rglob("*.md"))
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
        if pat.search(line)
    ]
    assert not offenders, f"prompt 仍宣称 refs 来自 system，与 BOWHEAD_REFS_IN_MESSAGES=1 时的运行时相反：{offenders}"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_rollback_rope_env_zero_restores_old_shape(monkeypatch, _stub_profiles) -> None:
    """★ 回滚绳：`BOWHEAD_REFS_IN_MESSAGES=0` 必须**逐字回到改前形态**。

    真机出事时救场靠它。突变：把读 env 那行写死成 True ⇒ 本条红。
    ⚠ 订正：我先前写的「这是唯一还在守『关得掉』的闸」不准确——同一个突变下
    `test_flag_off_system_changes_every_iteration` / `test_xml_is_byte_identical_across_both_modes`
    / `test_history_and_system_base_are_untouched_by_the_move` 也会红。本条的价值在于
    **判据直指「逐字回到改前形态」**，而不在于它独一份。
    """
    r = await _run(monkeypatch, flag_on=False)
    assert all(REFS_OPEN in s for s in r.systems), "关不掉——refs 没回到 system"
    assert all(all(REFS_OPEN not in c for _role, c in msgs) for msgs in r.messages), "关不掉——messages 里还有 refs"
