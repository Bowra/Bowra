"""spawn-language —— subagent_spawn 的 language 参数 + ctx 确定性兜底。

真机根因（116 prod, t_c31e011b4b14）：用户英文提问 + "Answer in English only" +
en-US 语言设置，三个 compute peer 仍交出全中文报告。当时 peer 看不到用户原话
（task= 是 Lead 转述），`_LANGUAGE_RULE` 的「跟随用户提问语言」对 peer 不可执行；
（2026-08-31 注：spawn-user-verbatim e767a1232 之后原话已随 sub-task 下传，但那行
定位在核对实体词、有 300 字上限，不承担语言判据 —— `language` 参数依然必要，
理由换成「不传则末尾 directive 按账号设置渲染、与显式要求唱反调」。）
Lead 派工的 task 本身就是英文写的，peer 照样写中文——「靠 task 语言自己悟」
已被该轮证伪。语言必须像 delivery 一样结构化下传：

- Lead 显式传 `language`（仅当用户消息里点名语言——只有 Lead 看得见原话）
- 没传 → ctx 的 output_language 确定性兜底（i18n 设置驱动，不依赖 Lead 服从性）
- 都没有 → 不注入（i18n flag-off workspace 行为逐字节不变）
"""

from __future__ import annotations

from types import SimpleNamespace

from bowhead_core.language_context import LanguageContext
from bowhead_core.subagent import (
    SubagentResult,
    register_subagent_tool,
    render_spawn_language_line,
    resolve_spawn_language,
)
from bowhead_core.tool_registry import ToolExecutionContext, build_default_registry


def test_spawn_schema_advertises_language() -> None:
    registry = build_default_registry()
    register_subagent_tool(registry)
    schema = registry.get("subagent_spawn").schema.input_schema
    # With German/French support, spawn output-language declarations include de-DE and fr-FR.
    assert schema["properties"]["language"]["enum"] == ["en-US", "zh-CN", "de-DE", "fr-FR"]
    assert "language" not in schema["required"]


class _CapturingRunner:
    """假 runner：只捕获 extra_instructions，返回最小成功结果。"""

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


async def _spawn_with(args: dict, language_context: LanguageContext | None = None) -> str:
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
        language_context=language_context,
    )
    inv = await registry.execute("subagent_spawn", {"profile": "compute", "task": "查今年利润前三的品牌", **args}, ctx)
    assert inv.error is None, inv.error
    assert runner.kwargs, f"runner.run 未被调用: {inv.result}"
    return str(runner.kwargs.get("extra_instructions") or "")


async def _spawn_and_get_ctx(args: dict, language_context: LanguageContext | None = None):
    """与 `_spawn_with` 同一条路，但把 ctx 交回来 —— 批6-B 的判据在 bus 上，不在 extra_instructions。"""
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
        language_context=language_context,
    )
    inv = await registry.execute("subagent_spawn", {"profile": "compute", "task": "t", **args}, ctx)
    assert inv.error is None, inv.error
    return ctx


_EN_MARK = "**language**: en-US"
_ZH_MARK = "**language**: zh-CN"


async def test_lead_explicit_en_injected() -> None:
    extra = await _spawn_with({"language": "en-US"})
    assert _EN_MARK in extra
    # Write instructions in the target language itself and name payload fields (observed: responding to users was interpreted literally
    # as chat prose only, leaving all payload titles/labels in Chinese).
    assert "Write ALL user-visible text in English" in extra
    assert "KPI labels" in extra


async def test_lead_explicit_zh_injected() -> None:
    extra = await _spawn_with({"language": "zh-CN"})
    assert _ZH_MARK in extra
    assert "所有用户可见文字一律用中文" in extra


async def test_ctx_output_language_is_the_deterministic_fallback() -> None:
    """Lead 没传 → i18n 设置驱动注入，不依赖 Lead 的 SOP 服从性。"""
    en_ctx = LanguageContext(ui_language="en-US", output_language="en-US")
    extra = await _spawn_with({}, language_context=en_ctx)
    assert _EN_MARK in extra


async def test_lead_explicit_beats_ctx_settings() -> None:
    """用户消息里点名的语言（经 Lead 传参）覆盖语言设置。"""
    en_ctx = LanguageContext(ui_language="en-US", output_language="en-US")
    extra = await _spawn_with({"language": "zh-CN"}, language_context=en_ctx)
    assert _ZH_MARK in extra
    assert _EN_MARK not in extra


async def test_no_param_no_ctx_injects_nothing() -> None:
    """i18n flag-off workspace：行为与 spawn-language 之前逐字节一致。
    review P2-4：`"language" not in extra` 在 extra 非空时是弱断言——这里 spawn
    未带任何 sub-task 参数，修前 extra 就是空，修后必须还是空（真·逐字节不变）。"""
    extra = await _spawn_with({})
    assert not extra


async def test_invalid_value_without_ctx_treated_as_omitted() -> None:
    """LLM 可写面防御：认不出的值当没传，不报错打断 spawn。"""
    extra = await _spawn_with({"language": "ja-JP"})
    assert "language" not in extra


async def test_invalid_value_with_ctx_falls_back_to_settings() -> None:
    en_ctx = LanguageContext(ui_language="en-US", output_language="en-US")
    extra = await _spawn_with({"language": "ja-JP"}, language_context=en_ctx)
    assert _EN_MARK in extra


def test_alias_normalization() -> None:
    assert resolve_spawn_language("english", None) == "en-US"
    assert resolve_spawn_language("EN", None) == "en-US"
    assert resolve_spawn_language("中文", None) == "zh-CN"
    assert resolve_spawn_language("zh-cn", None) == "zh-CN"
    assert resolve_spawn_language("français", None) == "fr-FR"
    assert resolve_spawn_language("French", None) == "fr-FR"
    assert resolve_spawn_language("法语", None) == "fr-FR"
    assert resolve_spawn_language("klingon", None) == ""
    # Every declarable language has an injected instruction (French instructions are themselves in French).
    assert "français" in render_spawn_language_line("fr-FR")


def test_render_unknown_language_renders_nothing() -> None:
    assert render_spawn_language_line("") == ""
    assert render_spawn_language_line("ja-JP") == ""


def test_lead_prompt_carries_rule_on_manifest_default_path() -> None:
    """peers=None derives the non-turn default from system manifests."""
    from bowhead_core.agents.prompts.lead import build_lead_system_prompt

    prompt = build_lead_system_prompt("ws_test", facts=[])
    assert "Set `language` when spawning" in prompt
    assert "'en-US' / 'zh-CN' / 'de-DE' / 'fr-FR'" in prompt
    assert "contradicting the user's explicit request" in prompt


def test_lead_prompt_carries_rule_on_dynamic_live_path() -> None:
    """Explicit peer rosters carry the spawn-language contract."""
    from bowhead_core.agents.prompts.lead import build_lead_system_prompt
    from bowhead_core.agents.prompts.peer_block import PeerEntry

    prompt = build_lead_system_prompt(
        "ws_test",
        facts=[],
        peers=[PeerEntry(name="compute", description="报告/查数", source="system")],
    )
    assert "Set `language` when spawning" in prompt
    assert "contradicting the user's explicit request" in prompt


def test_language_rule_single_sourced_in_roster_renderer() -> None:
    from bowhead_core.agents.prompts.peer_block import SPAWN_LANGUAGE_RULE, PeerEntry, render_peer_block

    dynamic = render_peer_block([PeerEntry(name="compute", description="d", source="system")])
    assert SPAWN_LANGUAGE_RULE in dynamic


async def test_handler_threads_language_override_into_run() -> None:
    """★review P1-2：prompt 最末的 build_language_directive 只认 ctx——Lead 显式
    传 en-US 而设置是 zh-CN 时，末尾段会用中文唱反调（显式要求再次输给设置）。
    handler 必须把裁决后的语言经 language_override 带进 run()。"""
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
        language_context=LanguageContext(ui_language="zh-CN", output_language="zh-CN"),
    )
    inv = await registry.execute("subagent_spawn", {"profile": "compute", "task": "t", "language": "en-US"}, ctx)
    assert inv.error is None, inv.error
    assert runner.kwargs.get("language_override") == "en-US"


def test_directive_tail_follows_override_not_settings() -> None:
    """P1-2 的闸本体：_legacy_run 用 dataclasses.replace 改写 output_language 后，
    末尾「输出语言与术语规范」必须说英语——与 Sub-task 行同向，不再唱反调。"""
    from dataclasses import replace

    from bowhead_core.language_directive import build_language_directive

    zh_ctx = LanguageContext(ui_language="zh-CN", output_language="zh-CN")
    effective = replace(zh_ctx, output_language="en-US")
    directive = build_language_directive(effective, "compute", include_tool_constraint=False)
    assert "英语（美式）" in directive
    assert "output_language=en-US" in directive
    assert "中文（简体）" not in directive.split("数据库内容")[0]  # Output-language line is no longer Chinese.


# ─── Batch 6-B: pin Lead's explicit language to the turn ──────────────────────────────


async def test_explicit_spawn_language_pins_the_turn(monkeypatch) -> None:
    """Lead 显式传 language ⇒ 钉 turn pin，于是 chrome 与**后续每个** peer 都跟着走。

    116 G5 的一半病灶：显式语言只影响了被点名的那一个 peer，chrome 仍按设置走。
    """
    from bowhead_core.effective_language import read_turn_pin

    monkeypatch.setenv("FEATURE_LANGUAGE_DECISION_V2", "1")
    ctx = await _spawn_and_get_ctx({"language": "en-US"}, LanguageContext(ui_language="zh-CN", output_language="zh-CN"))
    assert read_turn_pin(ctx.parent_bus) == "en-US"


async def test_ctx_fallback_does_not_count_as_explicit(monkeypatch) -> None:
    """★ 正对照：Lead **没传**时不算"点名"。

    兜底值本来就等于 turn 头钉的设置值，再钉一次是 no-op —— 但那会让"pin 被显式改过"
    这件事变得不可分辨。这条钉的是"只认 Lead 的参数"。
    """
    from bowhead_core.effective_language import read_turn_pin

    monkeypatch.setenv("FEATURE_LANGUAGE_DECISION_V2", "1")
    ctx = await _spawn_and_get_ctx({}, LanguageContext(ui_language="en-US", output_language="en-US"))
    assert read_turn_pin(ctx.parent_bus) == "", "没传 language 不该产生 pin"


async def test_explicit_spawn_language_is_inert_when_flag_off(monkeypatch) -> None:
    """回滚绳：flag 关时逐字节回到今天（只影响那一个 peer，不钉 turn）。"""
    from bowhead_core.effective_language import read_turn_pin

    monkeypatch.setenv("FEATURE_LANGUAGE_DECISION_V2", "0")
    ctx = await _spawn_and_get_ctx({"language": "en-US"}, LanguageContext(ui_language="zh-CN", output_language="zh-CN"))
    assert read_turn_pin(ctx.parent_bus) == ""


# ─── Review B2: unadmitted German output falls back using the same rule as LanguageContext.resolve ───


async def test_german_spawn_language_falls_back_when_german_output_is_not_admitted(monkeypatch) -> None:
    """提示词开始教 `'de-DE'` 后，Lead 显式传参会绕过 resolve 的准入闸；spawn 入口必须再挡一次。

    回落口径与 `LanguageContext.resolve` 逐字相同：界面语言（非德语时），否则 en-US。
    """
    from bowhead_core.effective_language import read_turn_pin

    monkeypatch.delenv("FEATURE_GERMAN_AI_OUTPUT", raising=False)
    monkeypatch.setenv("FEATURE_LANGUAGE_DECISION_V2", "1")
    zh_ui = LanguageContext(ui_language="zh-CN", output_language="zh-CN")
    extra = await _spawn_with({"language": "de-DE"}, language_context=zh_ui)
    assert _ZH_MARK in extra and "**Sprache**: de-DE" not in extra
    ctx = await _spawn_and_get_ctx({"language": "de-DE"}, zh_ui)
    assert read_turn_pin(ctx.parent_bus) == "zh-CN", "pin 也不能钉成未准入的德语"
    # German UI language ⇒ en-US; ctx fallback passes the same gate.
    de_ui = LanguageContext(ui_language="de-DE", output_language="de-DE")
    assert resolve_spawn_language("de-DE", SimpleNamespace(language_context=de_ui)) == "en-US"
    assert resolve_spawn_language(None, SimpleNamespace(language_context=de_ui)) == "en-US"
    assert resolve_spawn_language("deutsch", None) == "en-US"


async def test_french_spawn_language_needs_no_gate(monkeypatch) -> None:
    """法语回答直接开放：德语闸关着时，法语的显式传参与 ctx 兜底都照常下传与钉住。"""
    from bowhead_core.effective_language import read_turn_pin

    monkeypatch.delenv("FEATURE_GERMAN_AI_OUTPUT", raising=False)
    monkeypatch.setenv("FEATURE_LANGUAGE_DECISION_V2", "1")
    zh_ui = LanguageContext(ui_language="zh-CN", output_language="zh-CN")
    extra = await _spawn_with({"language": "français"}, language_context=zh_ui)
    assert "**Langue** : fr-FR" in extra
    ctx = await _spawn_and_get_ctx({"language": "fr-FR"}, zh_ui)
    assert read_turn_pin(ctx.parent_bus) == "fr-FR"
    fr_ui = LanguageContext(ui_language="fr-FR", output_language="fr-FR")
    assert resolve_spawn_language(None, SimpleNamespace(language_context=fr_ui)) == "fr-FR"
    # German gate off, French UI ⇒ German falls back to UI language (French), matching LanguageContext.resolve.
    assert resolve_spawn_language("de-DE", SimpleNamespace(language_context=fr_ui)) == "fr-FR"


async def test_german_spawn_language_passes_when_german_output_is_admitted(monkeypatch) -> None:
    """正对照：开关打开时德语照常下传与钉住（否则上一条可能只是恒等回落）。"""
    from bowhead_core.effective_language import read_turn_pin

    monkeypatch.setenv("FEATURE_GERMAN_AI_OUTPUT", "1")
    monkeypatch.setenv("FEATURE_LANGUAGE_DECISION_V2", "1")
    zh_ui = LanguageContext(ui_language="zh-CN", output_language="zh-CN")
    extra = await _spawn_with({"language": "de-DE"}, language_context=zh_ui)
    assert "**Sprache**: de-DE" in extra
    ctx = await _spawn_and_get_ctx({"language": "de-DE"}, zh_ui)
    assert read_turn_pin(ctx.parent_bus) == "de-DE"
