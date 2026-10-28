"""个人设置「答案风格」必须真的进模型的提示词 —— Lead 与 peer 两侧都要。

前一轮（`954c250e1`）把 `answer_style` 修进了白名单并回落进 `TurnRequest.user_profile`，
但那个 dict **全仓没有任何消费方**（`grep -rn "user_profile\\[" packages/` 零命中；语言轴
早在 `e4ccdbd2c` 改走 `user_profile_language` 形参）。净效果是「存得进去、界面回显对、
回答一个字不变」—— 比直接报错更隐蔽，也正是单测最容易漏的形态。

所以这一组用例的重心不在纯函数上，而在**接线**：必须穿过 `SubagentRunner` 真的跑一遍，
断言风格规则落进了喂给模型的 system / messages。纯函数用例单独绿是没用的
（本仓有过整块 JSX 删掉、865 条测试仍全绿的先例）。

突变锚点（每条都实测过会红）：
- 把 `subagent._legacy_run` 里 `language_block=_blk_language + _blk_answer_style`
  改回 `language_block=_blk_language` ⇒ Lead / peer 两条接线用例红；
- 把 spawn handler 里的 `answer_style_line` 从 `ctx_lines` 摘掉 ⇒ Sub-task 行用例红；
- 让 `resolve_answer_style` 在未设置时返回 "detailed" ⇒「不造默认值」两条红。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from bowhead_core.answer_style import (
    SUPPORTED_ANSWER_STYLES,
    build_answer_style_directive,
    render_answer_style_line,
    resolve_answer_style,
)
from bowhead_core.events import TurnEventBus
from bowhead_core.subagent import SubagentResult, SubagentRunner, register_subagent_tool
from bowhead_core.tool_registry import ToolExecutionContext, build_default_registry

from bowhead_router import (
    AgentBinding,
    LLMResponse,
    MockAdapter,
    ProviderConfig,
    ProviderRegistry,
    Router,
    TokenUsage,
)

# Four settings-select options (`components/settings/profile.tsx`); this defines the value set.
_SETTINGS_PAGE_OPTIONS = ("concise", "detailed", "technical", "business")


# ── The criterion itself: no invented defaults when unset ────────────────────────


@pytest.mark.unit
def test_supported_styles_match_the_settings_page_options() -> None:
    """取值集合以设置页的四个 option 为准；多一档少一档都算漂移。"""
    assert SUPPORTED_ANSWER_STYLES == _SETTINGS_PAGE_OPTIONS


@pytest.mark.unit
@pytest.mark.parametrize(
    "profile",
    [None, {}, {"display_name": "alice"}, {"answer_style": ""}],
)
def test_no_setting_means_no_style_and_no_default(profile: dict | None) -> None:
    """★ 用户没保存过这个设置 ⇒ 不注入，也**不**替他选「详细」。

    设置页默认显示「详细」只是前端初值，不是服务端事实（前一轮已钉过这条：
    `test_no_stored_answer_style_leaves_the_profile_untouched`），别在这儿把它造出来。
    """
    ctx = _ctx(profile)
    assert resolve_answer_style(ctx) == ""
    assert build_answer_style_directive(resolve_answer_style(ctx)) == ""
    assert render_answer_style_line(resolve_answer_style(ctx)) == ""


@pytest.mark.unit
def test_unknown_stored_value_is_not_guessed_into_a_style() -> None:
    """老账号里的存量值 / 前端加了档而后端没跟上 ⇒ 宁可不注入，不要按猜的那档改写回答。"""
    assert resolve_answer_style(_ctx({"answer_style": "verbose"})) == ""


@pytest.mark.unit
@pytest.mark.parametrize("style", _SETTINGS_PAGE_OPTIONS)
def test_every_settings_option_resolves_and_renders(style: str) -> None:
    assert resolve_answer_style(_ctx({"answer_style": style})) == style
    assert f"answer_style={style}" in build_answer_style_directive(style)
    assert f"**answer_style**: {style}" in render_answer_style_line(style)


@pytest.mark.unit
def test_stored_value_is_normalized() -> None:
    """服务端 KV 是自由字符串（无 Literal 约束），大小写/空白不该让设置静默失效。"""
    assert resolve_answer_style(_ctx({"answer_style": " Concise "})) == "concise"


@pytest.mark.unit
def test_the_four_styles_say_different_things() -> None:
    """四档必须给出**不同**的可执行指令 —— 否则设置换了档，模型看到的字是一样的。"""
    rendered = {s: build_answer_style_directive(s) for s in _SETTINGS_PAGE_OPTIONS}
    assert len(set(rendered.values())) == len(_SETTINGS_PAGE_OPTIONS)


@pytest.mark.unit
def test_lead_and_peer_read_the_same_rule_table() -> None:
    """★ Lead 的末尾 directive 与 peer 的 Sub-task 行必须同源。

    「同一个概念在 Lead 与 peer 两侧各写一份、只改一侧」是本仓反复发作的毛病；
    这条钉的就是两个渲染点共用 `_STYLE_RULES`。突变：把其中一处的文案改一个字 ⇒ 红。
    """
    for style in _SETTINGS_PAGE_OPTIONS:
        directive = build_answer_style_directive(style)
        line = render_answer_style_line(style)
        rule = line.split("—— ", 1)[1].replace("用户在本轮消息里若显式要求了别的长度或结构，以消息为准。", "")
        assert rule and rule in directive, f"{style}: peer 那行的规则文字不在 Lead 的 directive 里"


@pytest.mark.unit
def test_directive_keeps_style_out_of_the_data_rules() -> None:
    """风格只管「文字怎么写」，不能被读成「可以少给数字 / 少标来源」。"""
    directive = build_answer_style_directive("concise")
    assert "不改变取数口径、数字与引用规范" in directive


# ── Wiring gate ①: TurnRequest → ctx (the previous failure occurred here, without coverage) ──


_TURN_RUNNER = Path(__file__).parent.parent / "src" / "bowhead_core" / "turn_runner.py"


def _ctx_blocks(src: str) -> list[str]:
    """抠出每个 `ToolExecutionContext(...)` 构造块（照 `test_datarc_per_turn_client_ctx` 的写法）。"""
    blocks: list[str] = []
    start = 0
    while True:
        idx = src.find("ToolExecutionContext(", start)
        if idx == -1:
            break
        i = idx + len("ToolExecutionContext(")
        depth = 1
        while i < len(src) and depth > 0:
            if src[i] == "(":
                depth += 1
            elif src[i] == ")":
                depth -= 1
            i += 1
        blocks.append(src[idx:i])
        start = i
    return blocks


@pytest.mark.unit
def test_turn_request_user_profile_reaches_the_ctx() -> None:
    """★ gateway 把设置回落进 `TurnRequest.user_profile` 之后，ctx 必须真的拿到它。

    用产品自己的 ctx 工厂（`build_tool_exec_ctx` 的存在理由就是"给测试钉镜像不变量"）。
    """
    from bowhead_core.turn_runner import build_tool_exec_ctx

    request = SimpleNamespace(
        workspace_id="ws",
        user_id="u",
        turn_id="t",
        user_profile={"answer_style": "business"},
    )
    orch = SimpleNamespace(tool_registry=build_default_registry())
    ctx = build_tool_exec_ctx(orch, request, None)
    assert ctx.user_profile == {"answer_style": "business"}
    assert resolve_answer_style(ctx) == "business"


@pytest.mark.unit
def test_both_ctx_sites_pass_user_profile() -> None:
    """★★ 镜像双点：`turn_runner` 有两处 ctx 构造，**生产真正跑的是 inline 那处**。

    这两处历史上漂移过（inline_preview / datarc client 各一次，各自留了守卫），
    只接一处 = Lead 在生产上仍然读不到设置，而工厂那条路的用例还是绿的。
    """
    src = _TURN_RUNNER.read_text(encoding="utf-8")
    blocks = [b for b in _ctx_blocks(src) if "language_context=" in b]
    assert len(blocks) >= 2, f"期望 ≥2 处带 language_context 的 ctx 构造；实际 {len(blocks)}"
    for j, block in enumerate(blocks):
        assert 'user_profile=getattr(request, "user_profile", None)' in block, (
            f"ctx block #{j} 没有把 `TurnRequest.user_profile` 传下去 —— 答案风格在这条路上"
            f"会静默失效（值到了 TurnRequest，模型看不到）。Block head:\n{block[:240]}"
        )


# ── Wiring gate ②: must pass through SubagentRunner ───────────────────────────────


def _ctx(user_profile: dict | None) -> ToolExecutionContext:
    return ToolExecutionContext(
        workspace_id="w",
        member_uid="m",
        user_id="u",
        turn_id="t",
        datarc=None,
        user_profile=user_profile,
    )


class _CaptureRouter(Router):
    """记下每次请求的 system 与 messages（照 `test_context_prefix_order` 的做法）。"""

    def __init__(self, reg: ProviderRegistry) -> None:
        super().__init__(reg)
        self.calls: list[tuple[str, list[str]]] = []

    def _rec(self, req) -> None:  # noqa: ANN001
        self.calls.append((req.system or "", [(getattr(m, "content", "") or "") for m in (req.messages or [])]))

    async def invoke(self, agent_type, req, **kw):  # type: ignore[override]
        self._rec(req)
        return await super().invoke(agent_type, req, **kw)

    async def stream(self, agent_type, req, **kw):  # type: ignore[override]
        self._rec(req)
        return await super().stream(agent_type, req, **kw)


async def _prompt_seen_by_model(*, profile_name: str, user_profile: dict | None) -> str:
    """真的跑一遍 `SubagentRunner`，返回模型这次看到的 system + messages 全文。"""
    adapter = MockAdapter(provider_name="p1")
    adapter.scripted_responses = [LLMResponse(content="直答", usage=TokenUsage(input_tokens=10, output_tokens=5))]
    reg = ProviderRegistry()
    reg.load([ProviderConfig(name="p1", type="mock")])
    reg._adapters["p1"] = adapter  # type: ignore[attr-defined]
    router = _CaptureRouter(reg)
    for agent_type in ("lead", "analyst", "compute", "narrative", "echo"):
        router.bind(AgentBinding(agent_type=agent_type, primary=("p1", "mock")))

    runner = SubagentRunner(router=router, tool_registry=build_default_registry(), backend="legacy")
    await runner.run(
        profile_name=profile_name,
        task="今年的销售情况怎么样",
        parent_turn_id="t",
        parent_bus=TurnEventBus(),
        parent_ctx=_ctx(user_profile),
        extra_instructions="EXTRA",
    )
    assert router.calls, "没记到任何请求，这条闸是死的"
    system, messages = router.calls[0]
    return system + "\n".join(messages)


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("profile_name", ["lead", "compute"])
async def test_configured_style_reaches_the_model(profile_name: str) -> None:
    """★★ 本轮的核心：Lead 与 peer **两侧**都要拿到风格约束。

    走的是所有执行体的共同必经点，所以一条接线覆盖两侧；这里两个 profile 各验一次，
    免得「只改一侧」那种老毛病靠单侧用例蒙混过关。
    """
    prompt = await _prompt_seen_by_model(profile_name=profile_name, user_profile={"answer_style": "concise"})
    assert "## 回答风格" in prompt
    assert "answer_style=concise" in prompt
    assert "直接给结论" in prompt


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("profile_name", ["lead", "compute"])
async def test_no_setting_changes_the_prompt_not_at_all(profile_name: str) -> None:
    """正对照：没设置时提示词里连「回答风格」四个字都不该出现（逐字节不变的可观察面）。"""
    prompt = await _prompt_seen_by_model(profile_name=profile_name, user_profile=None)
    assert "回答风格" not in prompt
    assert "answer_style" not in prompt


@pytest.mark.unit
@pytest.mark.asyncio
async def test_switching_the_setting_switches_the_words_the_model_sees() -> None:
    """★ 判「改没改」看渲染出来的字：两档的提示词必须真的不同，且各自带自己的规则。"""
    concise = await _prompt_seen_by_model(profile_name="lead", user_profile={"answer_style": "concise"})
    business = await _prompt_seen_by_model(profile_name="lead", user_profile={"answer_style": "business"})
    assert concise != business
    assert "不写小标题" in concise and "不写小标题" not in business
    assert "可执行的建议" in business and "可执行的建议" not in concise


# ── Peer Sub-task context line (alongside the spawn-language line) ────────────────


class _CapturingRunner:
    """假 runner：只捕获 extra_instructions（照 `test_subagent_spawn_language` 的做法）。"""

    def __init__(self) -> None:
        self.kwargs: dict = {}

    async def run(self, **kwargs):
        self.kwargs = kwargs
        return SubagentResult(
            profile=str(kwargs.get("profile_name") or "compute"),
            subagent_id="sub_test",
            ok=True,
            final_answer="42",
        )


class _FakeBus:
    async def emit(self, ev) -> None:  # noqa: ARG002
        return None


async def _spawn_extra_instructions(user_profile: dict | None) -> str:
    registry = build_default_registry()
    register_subagent_tool(registry)
    runner = _CapturingRunner()
    ctx = ToolExecutionContext(
        workspace_id="1",
        member_uid="m",
        user_id="u",
        turn_id="parent_t",
        datarc=None,
        subagent_runner=runner,
        parent_bus=_FakeBus(),
        user_profile=user_profile,
    )
    inv = await registry.execute(
        "subagent_spawn",
        {"profile": "compute", "task": "查今年利润前三的品牌"},
        ctx,
    )
    assert inv.error is None, inv.error
    assert runner.kwargs, f"runner.run 未被调用: {inv.result}"
    return str(runner.kwargs.get("extra_instructions") or "")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_spawn_passes_the_style_down_as_sub_task_context() -> None:
    """peer 看不到用户原话里的风格诉求 ⇒ 与 language 同款结构化下传。"""
    extra = await _spawn_extra_instructions({"answer_style": "technical"})
    assert "- **answer_style**: technical" in extra
    assert "指标口径" in extra


@pytest.mark.unit
@pytest.mark.asyncio
async def test_spawn_without_the_setting_adds_no_line() -> None:
    extra = await _spawn_extra_instructions(None)
    assert "answer_style" not in extra
