"""Localized lookup rules follow admitted output language and preserve user text."""

from pathlib import Path
from typing import Any

import pytest
from bowhead_core.agents.prompts.lead import build_lead_system_prompt
from bowhead_core.agents.prompts.peer_block import PeerEntry
from bowhead_core.agents.prompts.simple_lookup_locale import load_lookup_templates, render_lookup_template
from bowhead_core.effective_language import pin_turn_output_language
from bowhead_core.events import TurnEventBus
from bowhead_core.language_context import LanguageContext
from bowhead_core.subagent import PROFILES, SubagentProfile, SubagentRunner, _build_compute_system_prompt
from bowhead_core.tool_registry import build_default_registry
from test_lead_bi_delegation import _CapturingRunner, _ctx, _mk_runtime, _mk_sctx, _recording_router

from bowhead_router import LLMResponse


@pytest.mark.parametrize("locale", ["en-US", "zh-CN", "de-DE", "fr-FR"])
def test_owned_prompt_loads_the_selected_language_template(locale: str) -> None:
    rules = load_lookup_templates(locale)
    compute_template = _build_compute_system_prompt()
    assert "[[lookup:stop_condition]]" in compute_template
    rendered = render_lookup_template(compute_template, locale)
    assert "[[lookup:" not in rendered
    for name, text in rules.items():
        if not name.startswith("lead_"):
            assert text in rendered
    assert rules["lead_scope"].strip() in build_lead_system_prompt("ws_lookup", facts=[], output_language=locale)
    for token in ("subagent_return", "exec_summary", "caveats[]", "add_chart.series", "{data}"):
        assert token in "\n".join(rules.values())
    if locale == "en-US":
        assert "上月营业额和客流量情况" not in rules["lookup_workflow"]


@pytest.mark.parametrize(
    ("locale", "query_boundary", "computation_boundary"),
    [
        ("en-US", "available query Tool for the requested source", "authorized computation Tool"),
        ("zh-CN", "指定数据源当前可用的查询工具", "已授权的计算工具"),
        ("de-DE", "verfügbares Abfrage-Tool für die angeforderte Quelle", "autorisiertes Berechnungs-Tool"),
        ("fr-FR", "outil de requête disponible pour la source demandée", "outil de calcul autorisé"),
    ],
)
def test_lookup_workflow_preserves_source_and_authorization_boundaries(locale: str, query_boundary: str, computation_boundary: str) -> None:
    workflow = load_lookup_templates(locale)["lookup_workflow"]
    assert query_boundary in workflow
    assert computation_boundary in workflow
    assert "subagent_return" in workflow


@pytest.mark.parametrize("locale", ["zh-CN", "de-DE", "fr-FR"])
def test_lead_loads_templates_before_adding_user_content(locale: str) -> None:
    original = "User-authored memory: [[lookup:lead_scope]]"
    prompt = build_lead_system_prompt(
        "ws_lookup",
        facts=[],
        output_language=locale,
        memory_pack_block=original,
        peers=[PeerEntry(name="team-data", description="Keep this description in English.", source="team", tool_names=frozenset({"bi_query"}))],
    )
    rules = load_lookup_templates(locale)
    assert rules["lead_scope"].strip() in prompt
    assert rules["lead_example"].format(data="team-data") in prompt
    assert original in prompt, "User memory must not be expanded as an application template."
    assert "Keep this description in English." in prompt
    assert "{data}" not in prompt


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("saved", "pinned", "override", "expected"),
    [("zh-CN", None, None, "zh-CN"), ("zh-CN", "fr-FR", None, "fr-FR"), ("fr-FR", "fr-FR", "en-US", "en-US")],
    ids=["saved-output-not-interface", "turn-pin", "explicit-spawn"],
)
@pytest.mark.parametrize("with_skill_prelude", [False, True], ids=["base", "preassigned-skill"])
async def test_compute_model_receives_effective_language_rules(
    monkeypatch: pytest.MonkeyPatch,
    saved: str,
    pinned: str | None,
    override: str | None,
    expected: str,
    with_skill_prelude: bool,
) -> None:
    from bowhead_core.builtin_subagent_override import apply_to_profile
    from bowhead_core.builtin_subagent_override_store import BuiltinSubagentOverride

    profile = SubagentProfile(
        name="compute",
        llm_agent_type="echo",
        tool_whitelist=(),
        system_prompt=_build_compute_system_prompt(),
        max_iterations=1,
    )
    if with_skill_prelude:
        profile = apply_to_profile(
            profile,
            BuiltinSubagentOverride(
                team_id="tm_lookup",
                name="compute",
                skills=["analyst-framework"],
                prompt_append="Keep this user-authored example: [[lookup:lead_scope]]",
            ),
        )
    monkeypatch.setitem(
        PROFILES,
        "compute",
        profile,
    )
    router, _ = _recording_router(LLMResponse(content="done", tool_calls=[]))
    adapter = router._registry.get("p1")
    invoke = adapter.invoke
    seen: list[str] = []

    async def record(req: Any) -> LLMResponse:
        seen.append(req.system)
        return await invoke(req)

    monkeypatch.setattr(adapter, "invoke", record)
    ctx = _ctx()
    ctx.language_context = LanguageContext(ui_language="en-US", output_language=saved)
    bus = TurnEventBus()
    if pinned:
        pin_turn_output_language(bus, pinned)
    await SubagentRunner(router=router, tool_registry=build_default_registry()).run(
        profile_name="compute",
        task="Read the existing summary",
        parent_turn_id=ctx.turn_id,
        parent_ctx=ctx,
        parent_bus=bus,
        language_override=override,
    )
    assert seen
    assert load_lookup_templates(expected)["stop_condition"] in seen[0]
    if with_skill_prelude:
        assert "Keep this user-authored example: [[lookup:lead_scope]]" in seen[0]
    else:
        assert "[[lookup:" not in seen[0]
    assert "bowhead-lookup-templates:" not in seen[0]
    if expected != "en-US":
        assert load_lookup_templates("en-US")["stop_condition"] not in seen[0]


def test_unknown_locale_keeps_english_rules() -> None:
    assert load_lookup_templates("es-ES") == load_lookup_templates("en-US")


def test_selected_template_is_loaded_without_an_english_source(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import yaml
    from bowhead_core.agents.prompts import simple_lookup_locale

    rules = dict(load_lookup_templates("zh-CN"))
    rules["stop_condition"] = "核实所需指标后立即返回，不继续门店分析。"
    (tmp_path / "zh-CN.yaml").write_text(yaml.safe_dump(rules, allow_unicode=True), encoding="utf-8")
    monkeypatch.setattr(simple_lookup_locale, "_TEMPLATE_DIR", tmp_path)
    load_lookup_templates.cache_clear()
    try:
        assert render_lookup_template("[[lookup:stop_condition]]", "zh-CN") == rules["stop_condition"]
        with pytest.raises(FileNotFoundError, match="fr-FR.yaml"):
            render_lookup_template("[[lookup:stop_condition]]", "fr-FR")
    finally:
        load_lookup_templates.cache_clear()


@pytest.mark.parametrize("locale", ["en-US", "zh-CN", "de-DE", "fr-FR"])
def test_formatted_slots_keep_the_original_rule_hierarchy(locale: str) -> None:
    rules = load_lookup_templates(locale)
    rendered = render_lookup_template(_build_compute_system_prompt(), locale)
    lines = rendered.splitlines()
    assert rules["analysis_depth"] in lines
    assert rules["lookup_workflow"] in lines
    assert rules["requested_metrics"] in lines
    assert rules["requested_artifacts"] in lines
    assert rules["factual_answer"] in lines
    assert rules["artifact_exception"] in lines


def test_locale_change_updates_the_bound_system_revision(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import yaml
    from bowhead_core.agent_execution_binding import _system_revision_projection_base
    from bowhead_core.agents.agent_runner_adapter import manifest_to_profile
    from bowhead_core.agents.prompts import simple_lookup_locale
    from bowhead_core.agents.registry import AgentRegistry
    from bowhead_core.subagent import _system_profile_snapshot

    manifest = AgentRegistry.bootstrap().get("compute")
    original = manifest_to_profile(manifest)
    original_revision = _system_revision_projection_base(
        team_id="tm_lookup",
        profile_name="compute",
        definition=_system_profile_snapshot(original),
    )[3]
    for locale in ("en-US", "zh-CN", "de-DE", "fr-FR"):
        rules = dict(load_lookup_templates(locale))
        if locale == "fr-FR":
            rules["stop_condition"] += " Retourner les résultats vérifiés immédiatement."
        (tmp_path / f"{locale}.yaml").write_text(yaml.safe_dump(rules, allow_unicode=True), encoding="utf-8")
    monkeypatch.setattr(simple_lookup_locale, "_TEMPLATE_DIR", tmp_path)
    load_lookup_templates.cache_clear()
    try:
        changed = manifest_to_profile(manifest)
        changed_revision = _system_revision_projection_base(
            team_id="tm_lookup",
            profile_name="compute",
            definition=_system_profile_snapshot(changed),
        )[3]
        assert original_revision != changed_revision, "Locale rules are part of the immutable system Agent definition."
        assert changed.system_prompt.count("bowhead-lookup-templates:") == 1
        with pytest.raises(ValueError, match="templates changed"):
            render_lookup_template(original.system_prompt, "zh-CN")
        assert "bowhead-lookup-templates:" not in render_lookup_template(changed.system_prompt, "fr-FR")
    finally:
        load_lookup_templates.cache_clear()


@pytest.mark.asyncio
async def test_live_lead_delegation_uses_turn_language(monkeypatch: pytest.MonkeyPatch) -> None:
    from bowhead_core.agents.lead_orchestrator import _lead_loop
    from bowhead_core.agents.registry import AgentRegistry

    monkeypatch.setenv("BOWHEAD_PLANNING_MODE", "0")
    runner = _CapturingRunner()
    runtime = _mk_runtime(runner)
    runtime.multi_agent_registry = AgentRegistry.bootstrap()
    runtime.parent_ctx.language_context = LanguageContext(ui_language="en-US", output_language="zh-CN")
    pin_turn_output_language(runtime.parent_bus, "fr-FR")
    await _lead_loop(_mk_sctx("t_lookup_locale"), "last month's revenue", runtime)
    assert runner.captured
    assert load_lookup_templates("fr-FR")["lead_scope"].strip() in (runner.captured[0] or "")
    assert load_lookup_templates("en-US")["lead_scope"].strip() not in (runner.captured[0] or "")
