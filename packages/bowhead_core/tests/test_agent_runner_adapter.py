"""Sprint multi-agent-v1.5 PR-#15 — agent_runner_adapter 单测.

覆盖:
- manifest_to_profile 翻译正确性
- register_manifest_as_profile 幂等
- register_all_system_manifests 启动批量注册
- make_legacy_agent_runner_fn closure 行为 (success / failure / payload-less)
- user-aware prompt loader workspace fallback
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest
from bowhead_core.agent_role_candidates import ExactAgentRoleChoice
from bowhead_core.agents import AgentRegistry
from bowhead_core.agents.agent_runner_adapter import (
    default_prompt_loader,
    make_legacy_agent_runner_fn,
    manifest_to_profile,
    register_all_system_manifests,
    register_manifest_as_profile,
)
from bowhead_core.agents.manifest import ToolsSpec
from bowhead_core.agents.task_spec import BusRef, TaskSpec
from bowhead_core.agents.turn_context import (
    RuntimeTurnContext,
    SerializableTurnContext,
)
from bowhead_core.subagent import PROFILES, SubagentResult

# Closing review (2026-08-25): **no markers** here ⇒ `-m unit` never collected these 20 tests,
# including batch 3-b chapter blocks, their P1 regression gate, and G4 "handwritten chapters supply `## Chapter title`".
# Batch 3/review-fix commits cited `-m unit` pass counts as gates; **that citation was wrong**
# (tests independently ran/passed each time, but cited gate did not cover them).
# Only dependencies are registry reading packaged manifest/prompts, no Redis/BI mock ⇒ unit.
pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _reset_profiles() -> None:
    """每 test 前 yield, no cleanup (manifest 同名覆盖 builtin OK 测试隔离)."""
    yield


# -- Manifest → Profile translation --


def test_manifest_to_profile_basic() -> None:
    """compute manifest → SubagentProfile 字段映射."""
    registry = AgentRegistry.bootstrap()
    manifest = registry.get("compute")
    profile = manifest_to_profile(manifest)
    assert profile.name == "compute"
    assert profile.llm_agent_type == "analyst"  # model alias
    assert "bi_query" in profile.tool_whitelist
    assert "subagent_return" in profile.tool_whitelist
    assert profile.max_iterations == 100
    assert profile.max_output_tokens is None
    assert profile.primary_capability_ref == "cap_data_query@1"
    # System prompt = compute.md body + manifest `system.append`; pin one **rule** anchor per part,
    # never subheadings; batch 3 layering renamed them (`## 起手指引` → `## ② 起手…`), so title anchors
    # fail for layout changes rather than proving both body and append connected.
    assert "最后一步必须" in profile.system_prompt  # Body: terminal condition.
    assert "TaskAgent" in profile.system_prompt  # append content.


def test_manifest_to_profile_uses_custom_loader() -> None:
    """prompt_loader 注入测试."""
    registry = AgentRegistry.bootstrap()
    manifest = registry.get("compute")

    def _stub(_m: Any) -> str:
        return "STUB PROMPT BODY"

    profile = manifest_to_profile(manifest, prompt_loader=_stub)
    assert profile.system_prompt.startswith("STUB PROMPT BODY")
    # append still included.
    assert "TaskAgent" in profile.system_prompt


def test_manifest_to_profile_materializes_python_sandbox_scope() -> None:
    """Workspace manifests compile the authoring pseudo ref like DB profiles."""
    registry = AgentRegistry.bootstrap()
    manifest = registry.get("compute").model_copy(
        update={
            "tools": ToolsSpec(
                default_enabled=False,
                allow=["python_sandbox"],
            ),
        },
    )

    profile = manifest_to_profile(manifest)

    assert profile.tool_whitelist == ("python_exec", "skill_invoke")
    assert profile.skill_invoke_allowlist == ("python_sandbox",)
    assert profile.system_prompt.startswith("## Python sandbox runtime contract")
    assert 'input.purpose="deliverable"' in profile.system_prompt


# -- Register --


def test_register_manifest_idempotent() -> None:
    """同名 manifest 多次 register 不冲突."""
    registry = AgentRegistry.bootstrap()
    manifest = registry.get("compute")
    register_manifest_as_profile(manifest)
    profile_1 = PROFILES.get("compute")
    register_manifest_as_profile(manifest)
    profile_2 = PROFILES.get("compute")
    # Same-manifest translations equivalent (system_prompt + tool_whitelist + max_*).
    assert profile_1.name == profile_2.name
    assert profile_1.tool_whitelist == profile_2.tool_whitelist


def test_register_all_system_manifests() -> None:
    """Register the system peers, including settings and Coach specialists.

    Ten loadable system manifests include the default-off settings specialist.
    register_all_system_manifests registers all that have a loadable prompt
    (lead with system: ~ registers via default_prompt_loader returning "").
    """
    registry = AgentRegistry.bootstrap()
    count = register_all_system_manifests(registry)
    # Lead with system: ~ still registers (default_prompt_loader returns "")
    assert count == 10
    assert "configuration-agent" in PROFILES
    assert "agent-coach" in PROFILES
    assert "compute" in PROFILES
    assert "financial-analyst" in PROFILES
    assert "business-analyst" in PROFILES
    assert "compliance-analyst" in PROFILES
    assert "kb-analyst" in PROFILES  # Phase 8b-4
    assert "lead" in PROFILES  # Phase 8b-4 (system: ~)
    assert "consultant" in PROFILES  # subagent-unification capstone


# -- make_legacy_agent_runner_fn --


@dataclass
class _StubBus:
    workspace_id: str = "ws_test"
    emitted: list[Any] = None

    def __post_init__(self) -> None:
        self.emitted = self.emitted or []

    async def emit(self, ev: Any) -> None:
        self.emitted.append(ev)


@dataclass
class _StubCtx:
    user_id: str = "u_1"
    workspace_id: str = "ws_test"
    role: str = "analyst"


@dataclass
class _StubRunner:
    """SubagentRunner stub — 控 SubagentResult 返回."""

    next_result: SubagentResult = None
    captured: dict = None

    def __post_init__(self) -> None:
        self.captured = {}

    async def run(
        self,
        *,
        profile_name: str,
        task: str,
        parent_turn_id: str,
        parent_bus: Any,
        parent_ctx: Any,
        extra_instructions: str | None = None,
        pre_minted_subagent_id: str | None = None,  # Batch 4: matches real signature.
        exact_binding: Any = None,
    ) -> SubagentResult:
        self.captured["profile_name"] = profile_name
        self.captured["pre_minted_subagent_id"] = pre_minted_subagent_id
        self.captured["task"] = task
        self.captured["extra"] = extra_instructions
        self.captured["exact_binding"] = exact_binding
        self.captured["parent_ctx"] = parent_ctx
        return self.next_result


def _exact_image_choice() -> ExactAgentRoleChoice:
    return ExactAgentRoleChoice(
        role_id="image",
        candidate_id="lrc_" + "a" * 32,
        reason_code="task_match",
        agent_id="system:marketing-analyst",
        runtime_profile_name="marketing-analyst",
        assignment_id=None,
        revision_ref="arv_marketing_image",
        release_id=None,
        behavior_fingerprint="sha256:" + "b" * 64,
    )


def _mk_runtime_ctx() -> RuntimeTurnContext:
    sctx = SerializableTurnContext(
        user_id="u_1",
        workspace_id="ws_test",
        session_id="s_a",
        turn_id="t_b",
        role="analyst",
        bus_ref=BusRef(session_id="s_a", turn_id="t_b"),
    )
    return RuntimeTurnContext(serializable=sctx)


@pytest.mark.asyncio
async def test_runner_fn_success_with_payload() -> None:
    """SubagentResult.payload → AgentRunnerFn 返 (payload, tokens_in, tokens_out)."""
    registry = AgentRegistry.bootstrap()
    register_all_system_manifests(registry)
    manifest = registry.get("compute")

    stub_runner = _StubRunner(
        next_result=SubagentResult(
            profile="compute",
            subagent_id="sub_x",
            ok=True,
            final_answer="",
            payload={"title": "test", "insights": []},
            tokens_input=100,
            tokens_output=200,
        ),
    )
    runner_fn = make_legacy_agent_runner_fn(
        subagent_runner=stub_runner,  # type: ignore[arg-type]
        parent_turn_id="t_test",
        parent_bus=_StubBus(),  # type: ignore[arg-type]
        parent_ctx=_StubCtx(),  # type: ignore[arg-type]
        team_sourced_names=frozenset(),  # No team agent here; required parameter is security guard.
    )
    spec = TaskSpec(agent_name="compute", user_question="近 30 天销量")
    payload, tokens_in, tokens_out = await runner_fn(manifest, spec, _mk_runtime_ctx())
    assert payload == {"title": "test", "insights": []}
    assert tokens_in == 100
    assert tokens_out == 200
    assert stub_runner.captured["profile_name"] == "compute"
    assert stub_runner.captured["task"] == "近 30 天销量"


@pytest.mark.asyncio
@pytest.mark.parametrize("document_profile", ["legacy", "bowra-v1"])
async def test_plan_consultant_step_gets_new_deck_authority_on_its_own_context(tmp_path, monkeypatch, document_profile) -> None:
    """Plan execution prepares the same new-deck authority as subagent_spawn, on a per-step copy."""
    from bowhead_core.artifact_store import ArtifactStore, LocalBackend
    from bowhead_core.deck_render_tool import DeckRenderBinding, deck_target

    from bowhead_core import document_runtime

    monkeypatch.setattr(document_runtime, "read_document_profile", lambda: document_profile)
    from bowhead_core.session_store import SessionStore
    from bowhead_core.tool_registry import ToolExecutionContext

    sessions = SessionStore(tmp_path / "artifacts.db")
    await sessions.initialize()
    await sessions.ensure_session(session_id="s_a", workspace_id="ws_test", user_id="u_1")
    await sessions.record_turn(turn_id="t_b", session_id="s_a", user_text="Create a new landscape deck.")
    store = ArtifactStore(backend=LocalBackend(root_dir=tmp_path / "files"), db_path=tmp_path / "artifacts.db")
    await store.initialize()
    parent_ctx = ToolExecutionContext(
        workspace_id="ws_test",
        member_uid="m_test",
        user_id="u_1",
        team_id="team_test",
        session_id="s_a",
        turn_id="t_b",
        role="analyst",
        datarc=None,
        artifact_store=store,
    )
    registry = AgentRegistry.bootstrap()
    register_all_system_manifests(registry)
    stub_runner = _StubRunner(
        next_result=SubagentResult(profile="consultant", subagent_id="sub_x", ok=True, final_answer="", payload={"title": "deck"})
    )
    runner_fn = make_legacy_agent_runner_fn(
        subagent_runner=stub_runner,  # type: ignore[arg-type]
        parent_turn_id="t_b",
        parent_bus=_StubBus(),  # type: ignore[arg-type]
        parent_ctx=parent_ctx,
        team_sourced_names=frozenset(),
    )
    # Exercise explicit native-renderer authority in either edition composition.
    consultant = registry.get("consultant")
    consultant = consultant.model_copy(update={"tools": ToolsSpec(allow=list(dict.fromkeys([*consultant.tools.allow, "render_deck"])))})
    binding = None
    try:
        await runner_fn(consultant, TaskSpec(agent_name="consultant", user_question="Create a new landscape deck."), _mk_runtime_ctx())
        step_ctx = stub_runner.captured["parent_ctx"]
        binding = step_ctx.deck_render_binding
        assert step_ctx is not parent_ctx and parent_ctx.deck_render_binding is None
        if document_profile == "legacy":
            assert isinstance(binding, DeckRenderBinding) and binding.target == deck_target(parent_ctx) and binding.required is True
            assert "render_deck" in stub_runner.captured["extra"]
        else:
            assert binding is None
            assert "bowra-pptx" in stub_runner.captured["extra"]

        await runner_fn(registry.get("compute"), TaskSpec(agent_name="compute", user_question="Query sales."), _mk_runtime_ctx())
        assert getattr(stub_runner.captured["parent_ctx"], "deck_render_binding", None) is None
    finally:
        import shutil
        from pathlib import Path

        if binding is not None:
            shutil.rmtree(Path(binding.template_path).parent, ignore_errors=True)


@pytest.mark.asyncio
async def test_runner_fn_admits_and_runs_exact_plan_choice(monkeypatch: pytest.MonkeyPatch) -> None:
    from bowhead_core import agent_execution_binding as binding_module

    monkeypatch.setenv("FEATURE_AGENT_BEHAVIOR_M3", "1")
    registry = AgentRegistry.bootstrap()
    register_all_system_manifests(registry)
    manifest = registry.get("marketing-analyst")
    exact_binding = SimpleNamespace(execution_id="aex_plan", runtime_profile_name=manifest.name)
    lifecycle: list[tuple[str, str, str | None]] = []

    class _ExactRunner(_StubRunner):
        team_subagent_store = SimpleNamespace(db_path="/tmp/unused-plan-binding.db")

        async def admit_execution_binding(self, **kwargs: Any) -> Any:
            self.captured["admission"] = kwargs
            return exact_binding

    class _LifecycleStore:
        def __init__(self, _path: str) -> None:
            pass

        async def mark_running(self, execution_id: str) -> None:
            lifecycle.append((execution_id, "running", None))

        async def mark_terminal(self, execution_id: str, *, status: str, failure_code: str | None = None) -> None:
            lifecycle.append((execution_id, status, failure_code))

    monkeypatch.setattr(binding_module, "AgentExecutionBindingStore", _LifecycleStore)
    runner = _ExactRunner(
        next_result=SubagentResult(
            profile=manifest.name,
            subagent_id="sub_exact",
            ok=True,
            final_answer="",
            payload={"title": "主视觉"},
        )
    )
    fn = make_legacy_agent_runner_fn(
        subagent_runner=runner,  # type: ignore[arg-type]
        parent_turn_id="t_plan",
        parent_bus=_StubBus(),  # type: ignore[arg-type]
        parent_ctx=_StubCtx(),  # type: ignore[arg-type]
        team_sourced_names=frozenset(),
    )

    await fn(
        manifest,
        TaskSpec(
            agent_name=manifest.name,
            user_question="生成主视觉",
            task_id="ps_image",
            agent_choice=_exact_image_choice(),
        ),
        _mk_runtime_ctx(),
    )

    admission = runner.captured["admission"]
    assert admission["dispatch_source"] == "plan"
    assert admission["dispatch_key"] == "step:ps_image:attempt:1"
    assert admission["agent_id"] == "system:marketing-analyst"
    assert admission["expected"].revision_ref == "arv_marketing_image"
    assert runner.captured["exact_binding"] is exact_binding
    assert lifecycle == [("aex_plan", "running", None), ("aex_plan", "completed", None)]


@pytest.mark.asyncio
async def test_runner_fn_marks_exact_resolution_error_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    from bowhead_core.agent_execution_binding import AgentExecutionBindingError
    from bowhead_core.agents.task_spec import PlanStepAgentBindingError

    from bowhead_core import agent_execution_binding as binding_module

    monkeypatch.setenv("FEATURE_AGENT_BEHAVIOR_M3", "1")
    registry = AgentRegistry.bootstrap()
    register_all_system_manifests(registry)
    manifest = registry.get("marketing-analyst")
    exact_binding = SimpleNamespace(execution_id="aex_blocked", runtime_profile_name=manifest.name)
    lifecycle: list[tuple[str, str, str | None]] = []

    class _ExactRunner(_StubRunner):
        team_subagent_store = SimpleNamespace(db_path="/tmp/unused-plan-binding.db")

        async def admit_execution_binding(self, **_kwargs: Any) -> Any:
            return exact_binding

        async def run(self, **_kwargs: Any) -> SubagentResult:
            raise AgentExecutionBindingError("agent_candidate_stale")

    class _LifecycleStore:
        def __init__(self, _path: str) -> None:
            pass

        async def mark_running(self, execution_id: str) -> None:
            lifecycle.append((execution_id, "running", None))

        async def mark_terminal(self, execution_id: str, *, status: str, failure_code: str | None = None) -> None:
            lifecycle.append((execution_id, status, failure_code))

    monkeypatch.setattr(binding_module, "AgentExecutionBindingStore", _LifecycleStore)
    runner = _ExactRunner()
    fn = make_legacy_agent_runner_fn(
        subagent_runner=runner,  # type: ignore[arg-type]
        parent_turn_id="t_plan",
        parent_bus=_StubBus(),  # type: ignore[arg-type]
        parent_ctx=_StubCtx(),  # type: ignore[arg-type]
        team_sourced_names=frozenset(),
    )

    with pytest.raises(PlanStepAgentBindingError) as exc:
        await fn(
            manifest,
            TaskSpec(
                agent_name=manifest.name,
                user_question="生成主视觉",
                task_id="ps_image",
                agent_choice=_exact_image_choice(),
            ),
            _mk_runtime_ctx(),
        )

    assert exc.value.error_kind == "agent_candidate_stale"
    assert lifecycle == [
        ("aex_blocked", "running", None),
        ("aex_blocked", "blocked", "agent_candidate_stale"),
    ]


@pytest.mark.asyncio
async def test_runner_fn_stale_exact_choice_never_enters_peer_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    from bowhead_core.agent_execution_binding import AgentExecutionBindingError

    monkeypatch.setenv("FEATURE_AGENT_BEHAVIOR_M3", "1")
    from bowhead_core.agents.task_spec import PlanStepAgentBindingError

    registry = AgentRegistry.bootstrap()
    manifest = registry.get("marketing-analyst")

    class _StaleRunner(_StubRunner):
        async def admit_execution_binding(self, **_kwargs: Any) -> Any:
            raise AgentExecutionBindingError("agent_candidate_stale")

    runner = _StaleRunner(
        next_result=SubagentResult(
            profile=manifest.name,
            subagent_id="sub_never",
            ok=True,
            final_answer="",
            payload={"unexpected": True},
        )
    )
    fn = make_legacy_agent_runner_fn(
        subagent_runner=runner,  # type: ignore[arg-type]
        parent_turn_id="t_plan",
        parent_bus=_StubBus(),  # type: ignore[arg-type]
        parent_ctx=_StubCtx(),  # type: ignore[arg-type]
        team_sourced_names=frozenset(),
    )

    with pytest.raises(PlanStepAgentBindingError) as exc:
        await fn(
            manifest,
            TaskSpec(
                agent_name=manifest.name,
                user_question="生成主视觉",
                task_id="ps_image",
                agent_choice=_exact_image_choice(),
            ),
            _mk_runtime_ctx(),
        )
    assert exc.value.error_kind == "agent_candidate_stale"
    assert "profile_name" not in runner.captured


@pytest.mark.asyncio
async def test_runner_fn_does_not_downgrade_exact_choice_when_behavior_reader_is_off() -> None:
    from bowhead_core.agents.task_spec import PlanStepAgentBindingError

    registry = AgentRegistry.bootstrap()
    manifest = registry.get("marketing-analyst")
    runner = _StubRunner(
        next_result=SubagentResult(
            profile=manifest.name,
            subagent_id="sub_never",
            ok=True,
            final_answer="",
            payload={"unexpected": True},
        )
    )
    fn = make_legacy_agent_runner_fn(
        subagent_runner=runner,  # type: ignore[arg-type]
        parent_turn_id="t_plan",
        parent_bus=_StubBus(),  # type: ignore[arg-type]
        parent_ctx=_StubCtx(),  # type: ignore[arg-type]
        team_sourced_names=frozenset(),
    )

    with pytest.raises(PlanStepAgentBindingError) as exc:
        await fn(
            manifest,
            TaskSpec(
                agent_name=manifest.name,
                user_question="生成主视觉",
                task_id="ps_image",
                agent_choice=_exact_image_choice(),
            ),
            _mk_runtime_ctx(),
        )
    assert exc.value.error_kind == "agent_binding_unavailable"
    assert "profile_name" not in runner.captured


@pytest.mark.asyncio
async def test_runner_fn_failure_raises() -> None:
    """SubagentResult.ok=False → AgentRunnerFn raise (LocalTaskRunner wrap 成 TaskFailureError)."""
    registry = AgentRegistry.bootstrap()
    register_all_system_manifests(registry)
    manifest = registry.get("compute")

    stub_runner = _StubRunner(
        next_result=SubagentResult(
            profile="compute",
            subagent_id="sub_x",
            ok=False,
            final_answer="",
            error="simulated subagent failure",
        ),
    )
    runner_fn = make_legacy_agent_runner_fn(
        subagent_runner=stub_runner,  # type: ignore[arg-type]
        parent_turn_id="t_test",
        parent_bus=_StubBus(),  # type: ignore[arg-type]
        parent_ctx=_StubCtx(),  # type: ignore[arg-type]
        team_sourced_names=frozenset(),  # No team agent here; required parameter is security guard.
    )
    spec = TaskSpec(agent_name="compute", user_question="q")
    with pytest.raises(RuntimeError, match="simulated subagent failure"):
        await runner_fn(manifest, spec, _mk_runtime_ctx())


@pytest.mark.asyncio
async def test_runner_fn_no_payload_uses_fallback_markdown() -> None:
    """SubagentResult 没 payload 但有 final_answer → wrap 成 fallback_markdown."""
    registry = AgentRegistry.bootstrap()
    register_all_system_manifests(registry)
    manifest = registry.get("compute")

    stub_runner = _StubRunner(
        next_result=SubagentResult(
            profile="compute",
            subagent_id="sub_x",
            ok=True,
            final_answer="一段 markdown 答案",
            payload=None,
            tokens_input=10,
            tokens_output=20,
        ),
    )
    runner_fn = make_legacy_agent_runner_fn(
        subagent_runner=stub_runner,  # type: ignore[arg-type]
        parent_turn_id="t_test",
        parent_bus=_StubBus(),  # type: ignore[arg-type]
        parent_ctx=_StubCtx(),  # type: ignore[arg-type]
        team_sourced_names=frozenset(),  # No team agent here; required parameter is security guard.
    )
    spec = TaskSpec(agent_name="compute", user_question="q")
    payload, _, _ = await runner_fn(manifest, spec, _mk_runtime_ctx())
    assert "fallback_markdown" in payload
    assert "一段 markdown 答案" in payload["fallback_markdown"]


@pytest.mark.asyncio
async def test_runner_fn_passes_upstream_payloads_as_extra() -> None:
    """upstream_payloads → extra_instructions JSON 序列化注入."""
    registry = AgentRegistry.bootstrap()
    register_all_system_manifests(registry)
    manifest = registry.get("compute")

    stub_runner = _StubRunner(
        next_result=SubagentResult(
            profile="compute",
            subagent_id="sub_x",
            ok=True,
            final_answer="",
            payload={"k": "v"},
        ),
    )
    runner_fn = make_legacy_agent_runner_fn(
        subagent_runner=stub_runner,  # type: ignore[arg-type]
        parent_turn_id="t_test",
        parent_bus=_StubBus(),  # type: ignore[arg-type]
        parent_ctx=_StubCtx(),  # type: ignore[arg-type]
        team_sourced_names=frozenset(),  # No team agent here; required parameter is security guard.
    )
    spec = TaskSpec(
        agent_name="compute",
        user_question="q",
        upstream_payloads=({"agent": "financial", "payload": {"net_profit": 100}},),
        routing_reason="财务+业务复合",
    )
    await runner_fn(manifest, spec, _mk_runtime_ctx())
    extra = stub_runner.captured["extra"]
    assert extra is not None
    assert "上游 task 已产出" in extra
    assert "financial" in extra
    assert "100" in extra
    assert "财务+业务复合" in extra


@pytest.mark.asyncio
async def test_runner_fn_auto_registers_unknown_manifest() -> None:
    """Manifest 未在 PROFILES 时, runner_fn 自动 register 一次再调."""
    # Remove compute from PROFILES (simulate unregistered).
    PROFILES.pop("compute", None)

    registry = AgentRegistry.bootstrap()
    manifest = registry.get("compute")

    stub_runner = _StubRunner(
        next_result=SubagentResult(
            profile="compute",
            subagent_id="sub_x",
            ok=True,
            final_answer="",
            payload={"ok": True},
        ),
    )
    runner_fn = make_legacy_agent_runner_fn(
        subagent_runner=stub_runner,  # type: ignore[arg-type]
        parent_turn_id="t_test",
        parent_bus=_StubBus(),  # type: ignore[arg-type]
        parent_ctx=_StubCtx(),  # type: ignore[arg-type]
        team_sourced_names=frozenset(),  # No team agent here; required parameter is security guard.
    )
    spec = TaskSpec(agent_name="compute", user_question="q")
    await runner_fn(manifest, spec, _mk_runtime_ctx())
    # PROFILES must contain compute afterward.
    assert "compute" in PROFILES


def test_default_prompt_loader_compute() -> None:
    """默认 loader 能读 compute.md."""
    registry = AgentRegistry.bootstrap()
    manifest = registry.get("compute")
    body = default_prompt_loader(manifest)
    assert len(body) > 100  # Nonempty.
    assert "BOOT 铁律" in body or "compute" in body.lower()


# ── P2-d (dev 1.0.77 dogfood): language follows user ─────────────


def test_every_system_peer_gets_the_language_rule(monkeypatch: pytest.MonkeyPatch) -> None:
    """中文提问 → 报告标题/KPI 标签/正文全英文。全仓此前**没有任何一条**语言约束，
    模型跟着证据走（BI 列名天然是英文）就滑到英文去了。

    规则落在 `manifest_to_profile`（所有 peer 系统提示词的唯一汇合点），而不是抄进
    8 份 `prompts/system/*.md` —— 抄 8 遍的话第 9 个 peer 加进来必漏。
    ⚠ 这条同时是**防漂移闸**：新增 peer 自动被覆盖，摘掉汇合点这里必红。
    """
    # This is the manifest_to_profile rule; with language decision v2 (default on) the rule moves to the
    # runtime fallback in SubagentRunner, so pin the switch-off path this convergence test covers.
    monkeypatch.setenv("FEATURE_LANGUAGE_DECISION_V2", "0")
    registry = AgentRegistry.bootstrap()
    names = registry.list_all()  # list[str]
    assert len(names) >= 8, "枚举到的 peer 太少，list_all 可能没真拿到清单"
    checked = 0
    for name in names:
        manifest = registry.get(name)
        if manifest.system is None:  # Lead uses in-memory prompt, bypasses this path.
            continue
        profile = manifest_to_profile(manifest)
        assert "语言跟随用户" in profile.system_prompt, manifest.name
        # Explicitly state "English BI columns are no reason", direct cause of English drift.
        assert "列名" in profile.system_prompt, manifest.name
        checked += 1
    assert checked >= 7, f"只覆盖到 {checked} 个 peer，汇合点可能被绕开了"


def test_language_rule_not_appended_to_empty_prompt() -> None:
    """manifest.system 为 None（Lead）时 body 是空串——不能凭空造出一份只有语言规则的提示词。"""

    class _NoSystem:
        name = "lead"
        system = None
        model = "analyst"
        max_iterations = 3
        max_output_tokens = 100
        description = "d"
        produces = ()  # capability-routing 2a: deliverable declaration (irrelevant here; empty completes stub shape).
        plugins = []
        skill_grants = None

        class tools:  # noqa: N801
            allow = ()

    profile = manifest_to_profile(_NoSystem())  # type: ignore[arg-type]
    assert "语言跟随用户" not in profile.system_prompt


@pytest.mark.asyncio
async def test_runner_fn_injects_language_from_ctx() -> None:
    """spawn-language：计划执行路不走 subagent_spawn handler——语言兜底必须在这条路
    也生效（注入只写一条路、另一条裸奔的教训）。"""
    from bowhead_core.language_context import LanguageContext

    registry = AgentRegistry.bootstrap()
    register_all_system_manifests(registry)
    manifest = registry.get("compute")

    stub_runner = _StubRunner(
        next_result=SubagentResult(profile="compute", subagent_id="sub_x", ok=True, final_answer="", payload={"k": "v"}),
    )
    ctx = _StubCtx()
    ctx.language_context = LanguageContext(ui_language="en-US", output_language="en-US")
    runner_fn = make_legacy_agent_runner_fn(
        subagent_runner=stub_runner,  # type: ignore[arg-type]
        parent_turn_id="t_test",
        parent_bus=_StubBus(),  # type: ignore[arg-type]
        parent_ctx=ctx,  # type: ignore[arg-type]
        team_sourced_names=frozenset(),
    )
    await runner_fn(manifest, TaskSpec(agent_name="compute", user_question="q"), _mk_runtime_ctx())
    extra = stub_runner.captured["extra"]
    assert extra is not None
    assert "**language**: en-US" in extra


@pytest.mark.asyncio
async def test_runner_fn_no_language_ctx_injects_nothing() -> None:
    """positive control 的反面：ctx 无 language_context → extra 不出现语言行（现状不变）。"""
    registry = AgentRegistry.bootstrap()
    register_all_system_manifests(registry)
    manifest = registry.get("compute")

    stub_runner = _StubRunner(
        next_result=SubagentResult(profile="compute", subagent_id="sub_x", ok=True, final_answer="", payload={"k": "v"}),
    )
    runner_fn = make_legacy_agent_runner_fn(
        subagent_runner=stub_runner,  # type: ignore[arg-type]
        parent_turn_id="t_test",
        parent_bus=_StubBus(),  # type: ignore[arg-type]
        parent_ctx=_StubCtx(),  # type: ignore[arg-type]
        team_sourced_names=frozenset(),
    )
    await runner_fn(manifest, TaskSpec(agent_name="compute", user_question="q"), _mk_runtime_ctx())
    assert "**language**" not in str(stub_runner.captured["extra"] or "")


@pytest.mark.asyncio
async def test_runner_fn_hands_the_agent_its_acceptance_criteria() -> None:
    """★判据必须跨过派活这条缝——执行者得看见自己要被按什么打分。

    2026-08-18 116 dogfood 根因：`success_criteria` 全仓只有三个用处(写库/渲染到
    计划卡/给核验官判)，`step_to_task` 从没带上它。enforce 上线后实测「Top N 名称」
    类判据 15/44 不达标，note 形状高度一致(产出点名 TOP1/TOP2/末位就是不点第 3 名)
    ——判官判得对，考卷没发给考生。

    断言穿到 `subagent_runner.run` 真正收到的 extra_instructions，不是 grep 源码。
    """
    registry = AgentRegistry.bootstrap()
    register_all_system_manifests(registry)
    manifest = registry.get("compute")
    stub_runner = _StubRunner(
        next_result=SubagentResult(profile="compute", subagent_id="sub_x", ok=True, final_answer="", payload={"k": "v"}),
    )
    runner_fn = make_legacy_agent_runner_fn(
        subagent_runner=stub_runner,  # type: ignore[arg-type]
        parent_turn_id="t_test",
        parent_bus=_StubBus(),  # type: ignore[arg-type]
        parent_ctx=_StubCtx(),  # type: ignore[arg-type]
        team_sourced_names=frozenset(),
    )
    spec = TaskSpec(
        agent_name="compute",
        user_question="按门店维度查询营业额 TOP10",
        success_criteria=("含营业额最高的 3 个门店名称及其营业额数值", "标注统计时间区间"),
    )
    await runner_fn(manifest, spec, _mk_runtime_ctx())
    extra = stub_runner.captured["extra"]
    assert extra is not None
    assert "含营业额最高的 3 个门店名称及其营业额数值" in extra
    assert "标注统计时间区间" in extra
    # Criteria alone insufficient: state verifier reads prose only, or "calculated but only in table" remains.
    assert "只读你产出的文字部分" in extra
    # ★ Anti-fabrication gate: absent it, change incentivizes pleasing scorer, worse than original bug.
    assert "绝不允许" in extra and "编造" in extra


@pytest.mark.asyncio
async def test_runner_fn_without_criteria_is_unchanged() -> None:
    """负对照: 没有判据(普通 chat 派发)时 extra 不得凭空长出验收标准块。"""
    registry = AgentRegistry.bootstrap()
    register_all_system_manifests(registry)
    manifest = registry.get("compute")
    stub_runner = _StubRunner(
        next_result=SubagentResult(profile="compute", subagent_id="sub_x", ok=True, final_answer="", payload={"k": "v"}),
    )
    runner_fn = make_legacy_agent_runner_fn(
        subagent_runner=stub_runner,  # type: ignore[arg-type]
        parent_turn_id="t_test",
        parent_bus=_StubBus(),  # type: ignore[arg-type]
        parent_ctx=_StubCtx(),  # type: ignore[arg-type]
        team_sourced_names=frozenset(),
    )
    await runner_fn(manifest, TaskSpec(agent_name="compute", user_question="q"), _mk_runtime_ctx())
    assert "本步验收标准" not in (stub_runner.captured["extra"] or "")


@pytest.mark.asyncio
async def test_runner_fn_tells_the_agent_it_is_writing_one_chapter() -> None:
    """★plan-report-assembly 批 3-b：腿得知道自己写的是一份文档里的**一章**。

    批 0/1/2 已把装订修好(8 份报告 → 1 份 8 章, 116 真机 t_c9853b490565 验过),
    但装订能降级**结构**、降级不了**措辞**: 腿仍按「整篇报告」的骨架写 —— 自指
    「本报告」、重铺一遍全局背景、末尾再来一段全局结论。这是同一条缝的第三例
    (第一例 success_criteria 见 task_spec.py:57, 第二例 deliverable 见 3-a)。

    断言穿到 `subagent_runner.run` 真正收到的 extra_instructions, 不是 grep 源码。
    ⚠ 决议 #4: 只给章名、**不给位次** —— 腿看不到前文, 「第 N/M 章」会诱导它写
    「如前文所述」去指一段它根本没读过的内容。
    """
    registry = AgentRegistry.bootstrap()
    register_all_system_manifests(registry)
    manifest = registry.get("compute")
    stub_runner = _StubRunner(
        next_result=SubagentResult(profile="compute", subagent_id="sub_x", ok=True, final_answer="", payload={"k": "v"}),
    )
    runner_fn = make_legacy_agent_runner_fn(
        subagent_runner=stub_runner,  # type: ignore[arg-type]
        parent_turn_id="t_test",
        parent_bus=_StubBus(),  # type: ignore[arg-type]
        parent_ctx=_StubCtx(),  # type: ignore[arg-type]
        team_sourced_names=frozenset(),
    )
    spec = TaskSpec(
        agent_name="compute",
        user_question="将我方经营指标与行业基准逐项对标",
        deliverable="交叉验证对标结论",
    )
    await runner_fn(manifest, spec, _mk_runtime_ctx())
    extra = stub_runner.captured["extra"]
    assert extra is not None
    # Output shape still supplied (never as chapter title; see below).
    assert "交叉验证对标结论" in extra
    # Constraint: only this chapter; no full-report intro / global conclusion.
    assert "一章" in extra
    assert "导语" in extra
    assert "全局结论" in extra
    # ★ Decision #4 negative guard: never supply ordinal position.
    assert "第 3 章" not in extra
    assert "共 8 章" not in extra
    # ★ Batch 3 self-review P1 regression: document-level recommendation/limitation pool **uses** chapter payloads.
    # 2026-09-07 (tech-design-document-minimalism §2.2): system **no longer** renders payload recommendations
    # as a canvas section, so cannot promise "render once at report end"; that makes each leg
    # omit recommendations and system omit rendering ⇒ report **no recommendations**, worse than prior per-leg repetition.
    # Current contract: still fill both payload fields (Lead chat replies), explicitly no separate section rendering.
    assert "recommendations" in extra
    assert "caveats" in extra
    assert "照常照数填全" not in extra, "旧承诺删干净了才不会让腿停写建议"
    assert "有就填" in extra and "不会" in extra
    # Negative: never say "not your responsibility", which stops legs filling fields.
    assert "不归你" not in extra
    # ★ G4 (actual 116): handwritten chapter (write_report) **lacks chapter title**; after leg calls
    # write_report, ψ-E eager compose suppresses that leg entirely (avoid duplicate body),
    # so payload title never renders h1. Require body-leading `## Chapter title`,
    # or reader sees continuation of prior chapter (Chinese step3 "交叉验证对标结论" did so).
    # 2026-09-25 user decision: writer titles by chapter content; deliverable describes output shape only.
    assert f"## {spec.deliverable}" not in extra and "## <本章标题>" in extra
    assert "无标题" in extra


@pytest.mark.asyncio
async def test_runner_fn_without_deliverable_grows_no_chapter_block() -> None:
    """负对照: planner 没填章名时 extra 不得凭空长出章节块 —— 钉的是「闸真的存在」。

    普通 multi-agent 派发的 TaskSpec 没有 deliverable, 必须与今天逐字一致。
    """
    registry = AgentRegistry.bootstrap()
    register_all_system_manifests(registry)
    manifest = registry.get("compute")
    stub_runner = _StubRunner(
        next_result=SubagentResult(profile="compute", subagent_id="sub_x", ok=True, final_answer="", payload={"k": "v"}),
    )
    runner_fn = make_legacy_agent_runner_fn(
        subagent_runner=stub_runner,  # type: ignore[arg-type]
        parent_turn_id="t_test",
        parent_bus=_StubBus(),  # type: ignore[arg-type]
        parent_ctx=_StubCtx(),  # type: ignore[arg-type]
        team_sourced_names=frozenset(),
    )
    await runner_fn(manifest, TaskSpec(agent_name="compute", user_question="q"), _mk_runtime_ctx())
    assert "本步交付的是整份文档里的一章" not in (stub_runner.captured["extra"] or "")


@pytest.mark.asyncio
async def test_chapter_block_never_uses_the_deliverable_as_the_chapter_title() -> None:
    """planner 约定里步级 `deliverable` 是「期望产出形态 (insight/表/章节)」——它不是章名。

    此前按词表猜哪些是形态词、其余当章名下发：116 先后出现 `## insight`（t_40869341f322）、
    `## 表+图+insight`（1.3.47）、`## 章节(表+图+关键指标)`（1.3.51 `s_bb8f798ebe34`），planner 换个写法就漏。
    2026-09-25 用户拍板：章名由写手按本章内容起；deliverable 一律只当形态下发。
    """
    registry = AgentRegistry.bootstrap()
    register_all_system_manifests(registry)
    manifest = registry.get("compute")

    async def _extra_for(deliverable: str) -> str:
        stub = _StubRunner(
            next_result=SubagentResult(profile="compute", subagent_id="sub_x", ok=True, final_answer="", payload={"k": "v"}),
        )
        fn = make_legacy_agent_runner_fn(
            subagent_runner=stub,  # type: ignore[arg-type]
            parent_turn_id="t_test",
            parent_bus=_StubBus(),  # type: ignore[arg-type]
            parent_ctx=_StubCtx(),  # type: ignore[arg-type]
            team_sourced_names=frozenset(),
        )
        await fn(manifest, TaskSpec(agent_name="compute", user_question="q", deliverable=deliverable), _mk_runtime_ctx())
        return stub.captured["extra"] or ""

    for deliverable in ("insight", "表", "  表  ", "表+图+insight", "章节(表+图+关键指标)", "退款与合规风险敞口评估"):
        extra = await _extra_for(deliverable)
        assert f"## {deliverable.strip()}" not in extra, f"{deliverable!r} 被当成章标题下发了"
        assert "章名：" not in extra
        # Retain shape information.
        assert f"产出形态**是「{deliverable.strip()}」" in extra, deliverable
        # Title from own content (G4: untitled handwritten chapter reads as prior chapter continuation).
        assert "按本章实际内容" in extra and "## <本章标题>" in extra


@pytest.mark.asyncio
@pytest.mark.parametrize("deliverable", ["", "table and insight"])
async def test_answer_plan_passes_chat_delivery_without_report_chapter_instructions(deliverable) -> None:
    registry = AgentRegistry.bootstrap()
    register_all_system_manifests(registry)
    runner = _StubRunner(next_result=SubagentResult(profile="compute", subagent_id="sub_answer", ok=True, final_answer="The answer is ready."))
    bus = _StubBus()
    bus._d6_plan_deliverable_kind = "answer"
    run = make_legacy_agent_runner_fn(
        subagent_runner=runner,
        parent_turn_id="t_answer",
        parent_bus=bus,
        parent_ctx=_StubCtx(),
        team_sourced_names=frozenset(),
    )
    await run(
        registry.get("compute"), TaskSpec(agent_name="compute", user_question="Calculate net revenue.", deliverable=deliverable), _mk_runtime_ctx()
    )
    instructions = runner.captured["extra"]
    assert "This Plan delivers a chat answer" in instructions
    assert "subagent_return" in instructions
    assert "## <本章标题>" not in instructions
    if deliverable:
        assert deliverable in instructions
