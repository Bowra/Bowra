from __future__ import annotations

import ast
import json
from collections import Counter
from pathlib import Path

import pytest
from bowhead_core.terminal_ingress_guard import scan_terminal_ingress

_ROOT = Path(__file__).resolve().parents[3]
_MANIFEST = _ROOT / "config/terminal_protocol_write_ingress.json"
_LEGACY_EXECUTION_ADAPTER = "admit_legacy_chat_execution"
_LEGACY_EXECUTION_ADAPTER_SOURCE = "packages/bowhead_gateway/src/bowhead_gateway/turn_service.py"
_LEGACY_EXECUTION_ADAPTER_OWNER = f"<module>.{_LEGACY_EXECUTION_ADAPTER}"
_LEGACY_EXECUTION_FINDING_KINDS = frozenset(
    {
        "legacy_event_bus_constructor",
        "legacy_event_bus_producer",
        "legacy_direct_run_turn_call",
        "legacy_execution_adapter_call",
        "legacy_execution_adapter_definition",
    }
)
_ALLOWED_LEGACY_BUS_CONSTRUCTORS = frozenset(
    {
        "legacy_event_bus_constructor|packages/bowhead_core/src/bowhead_core/events.py|<module>.EventBusRegistry.emit_out_of_band|TurnEventBus",
        "legacy_event_bus_constructor|packages/bowhead_core/src/bowhead_core/events.py|<module>.EventBusRegistry.get_or_create|TurnEventBus",
        "legacy_event_bus_constructor|packages/bowhead_core/src/bowhead_core/terminal_runtime.py|<module>.TerminalProtocolBusAdapter.__init__|TurnEventBus",
        "legacy_event_bus_constructor|packages/bowhead_core/src/bowhead_core/agent_eval_subagent_executor.py|<module>.ExactSubagentEvalExecutor.execute|TurnEventBus",
    }
)
_ALLOWED_LEGACY_DIRECT_RUN_TURN_CALLS = frozenset(
    {
        "legacy_direct_run_turn_call|packages/bowhead_gateway/src/bowhead_gateway/routes/chat.py|<module>._run_turn_and_cleanup|run_turn",
    }
)
# The conservative scanner also sees this typed domain method. It runs inside
# the already claimed root worker and never invokes Orchestrator.run_turn.
_DOMAIN_RUN_TURN_CALLS = frozenset(
    {"legacy_direct_run_turn_call|packages/bowhead_core/src/bowhead_core/documents/tool.py|<module>.register_document_tool.submit|run_turn"}
)


def _manifest() -> dict[str, object]:
    value = json.loads(_MANIFEST.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _sources() -> dict[str, str]:
    roots = _manifest().get("scan_roots")
    assert isinstance(roots, list) and roots
    paths = (path for root in roots if isinstance(root, str) for path in (_ROOT / root).rglob("*.py"))
    return {str(path.relative_to(_ROOT)): path.read_text(encoding="utf-8") for path in paths}


def test_guard_detects_alias_nonleading_status_helper_and_opaque_emit() -> None:
    findings = scan_terminal_ingress(
        {
            "fixture.py": """
async def bad(store, bus, event, db, orch):
    fail = store.mark_failed
    await fail(turn_id="t", error="x", error_kind="internal")
    await db.execute("UPDATE turns SET ended_at=?, status='failed' WHERE id=?")
    await bus.emit(event)
    await bus.emit(orch._event("t", "done", {}))
"""
        }
    )
    identities = {(finding.kind, finding.detail) for finding in findings}
    assert ("legacy_mutator", "mark_failed") in identities
    assert ("direct_sql", "turn_status") in identities
    assert ("root_terminal_emit", "unresolved") in identities
    assert ("root_terminal_emit", "done") in identities


@pytest.mark.unit
def test_guard_counts_a_second_write_of_the_same_kind_in_one_function() -> None:
    """同一函数里第二条同类写入 / 发射 / legacy 调用不能被第一条的登记顺带放行——按次数计。"""
    findings = scan_terminal_ingress(
        {
            "fixture.py": """
async def run(db, store, bus, event):
    await db.execute("UPDATE clarification_requests SET resume_context_json=? WHERE request_id=?")
    await db.execute("UPDATE clarification_requests SET status='pending' WHERE request_id=?")
    await store.mark_failed(turn_id="t")
    await store.mark_failed(turn_id="t")
    await bus.emit(event)
    await bus.emit(event)
"""
        }
    )
    counts = Counter(finding.identity for finding in findings)
    assert counts["direct_sql|fixture.py|<module>.run|clarification_decision_source"] == 2
    assert counts["legacy_mutator|fixture.py|<module>.run|mark_failed"] == 2
    assert counts["root_terminal_emit|fixture.py|<module>.run|unresolved"] == 2


@pytest.mark.unit
@pytest.mark.parametrize(
    ("factory_import", "arguments"),
    [
        ("from bowhead_core.canvas_composer import _event as make_event", '"t", "done", {}'),
        ("from bowhead_core.events import OrchestratorEvent as make_event", 'event_type="done", turn_id="t", payload={}'),
        ("make_event = canvas._event", '"t", "done", {}'),
        ("make_event = getattr(events, 'OrchestratorEvent')", 'event_type="done", turn_id="t", payload={}'),
    ],
)
@pytest.mark.parametrize("alias", ["make_event", "copied", "typed"])
def test_guard_identifies_terminal_events_through_factory_aliases(factory_import: str, arguments: str, alias: str) -> None:
    # Non-terminal calls at a reviewed forwarding edge are also recorded as
    # unresolved. A new done must acquire its own identity, not hide in that edge.
    source = f"""{factory_import}
async def forward(bus):
    copied = make_event
    typed: object = copied
    event = {alias}({arguments})
    await bus.emit(event)
"""
    findings = scan_terminal_ingress({"fixture.py": source})
    assert {finding.identity for finding in findings} == {"root_terminal_emit|fixture.py|<module>.forward|done"}


@pytest.mark.unit
def test_guard_event_factory_aliases_respect_local_scope_and_reassignment() -> None:
    findings = scan_terminal_ingress(
        {
            "fixture.py": """
async def known(bus):
    from bowhead_core.canvas_composer import _event as make_event
    await bus.emit(make_event("t", "error", {}))
    make_event = other_factory
    await bus.emit(make_event("t", "done", {}))

async def unknown(bus):
    await bus.emit(make_event("t", "done", {}))
""",
        }
    )
    assert {finding.identity for finding in findings} == {
        "root_terminal_emit|fixture.py|<module>.known|error",
        "root_terminal_emit|fixture.py|<module>.known|unresolved",
        "root_terminal_emit|fixture.py|<module>.unknown|unresolved",
    }


def test_guard_detects_multiline_sql_fstring_and_inline_root_emit() -> None:
    findings = scan_terminal_ingress(
        {
            "fixture.py": '''
async def bad(store, bus, turn_id):
    await store.execute("""
      UPDATE sessions
      SET deleted_at = ?
      WHERE id = ?
    """)
    await store.execute(f"UPDATE turns SET status = '{turn_id}'")
    await bus.emit(OrchestratorEvent('done', turn_id, {}))
'''
        }
    )
    assert {finding.kind for finding in findings} == {
        "direct_sql",
        "root_terminal_emit",
    }


def test_guard_detects_account_delete_and_swallowed_fail_stop() -> None:
    findings = scan_terminal_ingress(
        {
            "fixture.py": """
async def bad(bus, db, event):
    await db.execute("DELETE FROM users WHERE id=?", ("u_1",))
    try:
        await bus.emit(event)
    except BaseException:
        pass
"""
        }
    )
    identities = {(finding.kind, finding.detail) for finding in findings}
    assert ("direct_sql", "account_delete") in identities
    assert (
        "durability_abort_swallowed",
        "emit_try_line_4",
    ) in identities


def test_guard_detects_s5_source_control_and_bare_approval_task() -> None:
    findings = scan_terminal_ingress(
        {
            "fixture.py": """
async def bad(store, approval, worker):
    await store.execute("INSERT OR IGNORE INTO chat_turn_user_sources(turn_id) VALUES ('t')")
    await store.execute("UPDATE clarification_requests SET status='resolved'")
    await store.execute("UPDATE turn_control_decisions SET state='generation_opened'")
    legacy_resume = store.spawn_resume_turn
    await legacy_resume(worker)
    asyncio.create_task(approval.execute())
"""
        }
    )
    identities = {(finding.kind, finding.detail) for finding in findings}
    assert ("direct_sql", "chat_turn_user_source") in identities
    assert ("direct_sql", "clarification_decision_source") in identities
    assert ("direct_sql", "terminal_decision") in identities
    assert ("s5_control_mutator", "spawn_resume_turn") in identities
    assert ("approval_bare_task", "execute") in identities


def test_guard_detects_chat_task_effect_runtime_and_retention_ingress() -> None:
    findings = scan_terminal_ingress(
        {
            "packages/bowhead_core/src/bowhead_core/turn_runner.py": """
async def run(store, work):
    asyncio.create_task(work())
    create_detached_auxiliary_task(work(), reason="session_title")
    await store.record_tool_effect_settlement(work)

class Store:
    async def aggregate_effect_retention(self):
        await self.db.execute("DELETE FROM turn_effect_attempts WHERE turn_id=?")
""",
        }
    )
    identities = {(finding.kind, finding.detail) for finding in findings}
    assert identities == {
        ("chat_task_spawn", "create_task:work"),
        (
            "chat_task_spawn",
            "create_detached_auxiliary_task:session_title",
        ),
        ("effect_runtime_writer", "record_tool_effect_settlement"),
        ("effect_retention_gate_definition", "aggregate_effect_retention"),
        ("effect_detail_delete", "turn_effect_attempts"),
    }


def test_guard_counts_duplicate_reviewed_ingress_and_resolves_aliases() -> None:
    findings = scan_terminal_ingress(
        {
            "packages/bowhead_core/src/bowhead_core/agents/task_runner.py": """
from asyncio import create_task as spawn, ensure_future as later, shield as protect
from somewhere import record_manual_effect_settlement as settle_control
from bowhead_core.terminal_llm_invocation import detached_auxiliary_scope as detach

class Runner:
    async def run(self, store, work):
        alias = store.record_tool_effect_settlement
        typed_alias: object = getattr(store, "record_tool_effect_settlement")
        dynamic_spawn: object = getattr(asyncio, "create_task")
        spawn(work())
        spawn(work())
        later(work())
        dynamic_spawn(work())
        getattr(asyncio, "create_task")(work())
        protect(work())
        await alias(work)
        await alias(work)
        await typed_alias(work)
        await getattr(store, "record_tool_effect_settlement")(work)
        await settle_control(work)
        await settle_control(work)
        with detach(reason=\"audit\"):
            pass
        await store.db.execute('DELETE FROM \"turn_effect_settlements\" WHERE attempt_id=?')
        await store.db.execute("DELETE " + "FROM turn_effect_evidence_links WHERE evidence_link_id=?")
""",
        }
    )
    counts = Counter(finding.identity for finding in findings)
    assert counts["chat_task_spawn|packages/bowhead_core/src/bowhead_core/agents/task_runner.py|<module>.Runner.run|create_task:work"] == 4
    assert (
        counts["effect_runtime_writer|packages/bowhead_core/src/bowhead_core/agents/task_runner.py|<module>.Runner.run|record_tool_effect_settlement"]
        == 4
    )
    assert counts["chat_task_spawn|packages/bowhead_core/src/bowhead_core/agents/task_runner.py|<module>.Runner.run|ensure_future:work"] == 1
    assert counts["chat_task_spawn|packages/bowhead_core/src/bowhead_core/agents/task_runner.py|<module>.Runner.run|shield:work"] == 1
    assert (
        counts[
            "effect_control_writer|packages/bowhead_core/src/bowhead_core/agents/task_runner.py|<module>.Runner.run|record_manual_effect_settlement"
        ]
        == 2
    )
    assert (
        counts["effect_detail_delete|packages/bowhead_core/src/bowhead_core/agents/task_runner.py|<module>.Runner.run|turn_effect_settlements"] == 1
    )
    assert (
        counts["effect_detail_delete|packages/bowhead_core/src/bowhead_core/agents/task_runner.py|<module>.Runner.run|turn_effect_evidence_links"]
        == 1
    )


def test_guard_detects_idempotent_emit_alias_and_approval_task_wrappers() -> None:
    findings = scan_terminal_ingress(
        {
            "fixture.py": """
async def run(approval):
    await approval.execute()

async def opaque_run(x):
    await x.execute()

async def bad(bus, event, approval):
    publish = bus.emit_idempotent
    try:
        await publish(event, idempotency_key="k")
    except BaseException:
        pass
    worker = run(approval)
    spawn = asyncio.create_task
    spawn(worker)
    go = approval.execute
    asyncio.create_task(go())
    x = approval
    alias = x.execute
    launch = asyncio.create_task
    launch(alias())
    launch(opaque_run(approval))
"""
        }
    )
    identities = {(finding.kind, finding.detail) for finding in findings}
    assert ("root_terminal_emit", "unresolved") in identities
    assert (
        "durability_abort_swallowed",
        "emit_try_line_10",
    ) in identities
    assert ("approval_bare_task", "execute") in identities


def test_guard_detects_terminal_feature_flag_legacy_fallback() -> None:
    findings = scan_terminal_ingress(
        {
            "fixture.py": """
async def bad(store, terminal_protocol_feature_enabled):
    if terminal_protocol_feature_enabled:
        await store.mark_failed("t")
"""
        }
    )
    identities = {(finding.kind, finding.detail) for finding in findings}
    assert ("legacy_mutator", "mark_failed") in identities
    assert (
        "feature_flag_control_fallback",
        "mark_failed",
    ) in identities


def test_guard_detects_legacy_adapter_and_only_producer_bus_calls() -> None:
    sources = {
        "fixture.py": """
from bowhead_gateway.turn_service import admit_legacy_chat_execution as admit

async def admit_legacy_chat_execution(registry, request):
    create = registry.get_or_create
    await create(request.turn_id)
    await registry.get_or_create(request.turn_id, producer=True)
    await registry.get_or_create(request.turn_id, producer=False)
    await registry.emit_out_of_band(request.turn_id, request.event)

async def caller(request):
    launch = admit
    await launch(request)
    await request.orch.run_turn(request)

async def dynamic_producer(registry, request):
    await getattr(registry, "get_or_create")(request.turn_id)

async def direct_constructor():
    TurnEventBus()
""",
        "packages/bowhead_core/src/bowhead_core/terminal_runtime_factory.py": """
class TerminalRuntimeFactory:
    async def _attach_opened_scope_owned(self, scope):
        await self._generation_buses.get_or_create(scope, claim_epoch=1)
""",
    }
    identities = {finding.identity for finding in scan_terminal_ingress(sources) if finding.kind in _LEGACY_EXECUTION_FINDING_KINDS}
    assert identities == {
        "legacy_execution_adapter_definition|fixture.py|<module>.admit_legacy_chat_execution|admit_legacy_chat_execution",
        "legacy_execution_adapter_call|fixture.py|<module>.caller|admit_legacy_chat_execution",
        "legacy_direct_run_turn_call|fixture.py|<module>.caller|run_turn",
        "legacy_event_bus_producer|fixture.py|<module>.admit_legacy_chat_execution|get_or_create",
        "legacy_event_bus_producer|fixture.py|<module>.dynamic_producer|get_or_create",
        "legacy_event_bus_constructor|fixture.py|<module>.direct_constructor|TurnEventBus",
    }


def test_production_terminal_ingress_exactly_matches_versioned_manifest() -> None:
    manifest = _manifest()
    assert manifest.get("manifest_version") == 8
    entries = manifest.get("entries")
    assert isinstance(entries, dict) and entries
    for metadata in entries.values():
        assert isinstance(metadata, dict)
        assert isinstance(metadata.get("owner"), str) and metadata["owner"].strip()
        assert isinstance(metadata.get("reason"), str) and metadata["reason"].strip()
    for identity, metadata in entries.items():
        if identity.startswith(("effect_control_writer|", "effect_runtime_writer|")):
            for field in (
                "effect_class",
                "identity",
                "provider_adapter_key",
                "release_waiver",
            ):
                assert isinstance(metadata.get(field), str) and metadata[field].strip()
        if identity.startswith(
            (
                "chat_task_spawn|",
                "post_terminal_auxiliary_scope|",
            )
        ):
            for field in (
                "effect_scope",
                "task_class",
                "usage_scope",
            ):
                assert isinstance(metadata.get(field), str) and metadata[field].strip()

    sources = _sources()
    findings = scan_terminal_ingress(sources)
    actual = Counter(finding.identity for finding in findings)
    legacy_execution = {finding.identity for finding in findings if finding.kind in _LEGACY_EXECUTION_FINDING_KINDS}
    adapter_definitions = {identity for identity in legacy_execution if identity.startswith("legacy_execution_adapter_definition|")}
    assert adapter_definitions == {
        "|".join(
            (
                "legacy_execution_adapter_definition",
                _LEGACY_EXECUTION_ADAPTER_SOURCE,
                _LEGACY_EXECUTION_ADAPTER_OWNER,
                _LEGACY_EXECUTION_ADAPTER,
            )
        )
    }
    adapter_callers = {identity for identity in legacy_execution if identity.startswith("legacy_execution_adapter_call|")}
    # Check the reviewed caller identities, not a count tied to an old rollout.
    assert adapter_callers == {identity for identity in entries if identity.startswith("legacy_execution_adapter_call|")}
    producer_owners = {
        (parts[1], parts[2])
        for identity in legacy_execution
        if identity.startswith("legacy_event_bus_producer|")
        for parts in (identity.split("|", 3),)
    }
    assert producer_owners == {
        (
            _LEGACY_EXECUTION_ADAPTER_SOURCE,
            _LEGACY_EXECUTION_ADAPTER_OWNER,
        )
    }
    constructors = {identity for identity in legacy_execution if identity.startswith("legacy_event_bus_constructor|")}
    assert constructors == _ALLOWED_LEGACY_BUS_CONSTRUCTORS
    direct_run_turn = {identity for identity in legacy_execution if identity.startswith("legacy_direct_run_turn_call|")}
    assert direct_run_turn == _ALLOWED_LEGACY_DIRECT_RUN_TURN_CALLS | _DOMAIN_RUN_TURN_CALLS
    expected = Counter({identity: metadata.get("occurrences", 1) for identity, metadata in entries.items()})
    assert all(isinstance(count, int) and not isinstance(count, bool) and count >= 1 for count in expected.values())
    assert actual == expected, "new=" + "\n".join(sorted(actual - expected)) + "\nstale=" + "\n".join(sorted(expected - actual))


def test_d0b_mixed_version_deploy_does_not_override_legacy_turn_cap() -> None:
    """The rolling orphan age fence requires one unchanged cap across versions."""

    deployment_sources = [path for root in (_ROOT / "deploy", _ROOT / "config") for path in root.rglob("*") if path.is_file()]
    offenders = [
        str(path.relative_to(_ROOT))
        for path in deployment_sources
        if "BOWHEAD_LEAD_LOOP_HARD_TIMEOUT_SEC"
        in path.read_text(
            encoding="utf-8",
            errors="ignore",
        )
    ]
    assert offenders == []


def test_auth_mutator_inventory_calls_transaction_local_epoch_helpers() -> None:
    user_tree = ast.parse((_ROOT / "packages/bowhead_core/src/bowhead_core/user_store.py").read_text(encoding="utf-8"))
    access_tree = ast.parse((_ROOT / "packages/bowhead_core/src/bowhead_core/access_control.py").read_text(encoding="utf-8"))
    auth_tree = ast.parse((_ROOT / "packages/bowhead_gateway/src/bowhead_gateway/routes/auth.py").read_text(encoding="utf-8"))

    assert _method_calls(user_tree, "add_member", "_bump_team_membership_epoch_conn")
    assert _method_calls(user_tree, "remove_member", "_bump_team_membership_epoch_conn")
    assert _method_calls(user_tree, "update_member_role", "_bump_team_membership_epoch_conn")
    assert _method_calls(user_tree, "promote_team_role", "_bump_team_membership_epoch_conn")
    assert _method_calls(
        user_tree,
        "accept_invite_for_user_atomic",
        "_bump_team_membership_epoch_conn",
    )
    assert _method_calls(
        user_tree,
        "accept_invite_from_intent_atomic",
        "_bump_team_membership_epoch_conn",
    )
    assert _method_calls(
        user_tree,
        "set_team_creator",
        "_bump_team_membership_epoch_conn",
    )
    assert _method_calls(
        user_tree,
        "transfer_team_owner",
        "_bump_team_membership_epoch_conn",
    )
    assert _method_calls(
        user_tree,
        "admin_soft_delete_member_detailed",
        "_bump_team_membership_epoch_conn",
    )
    account_tree = ast.parse((_ROOT / "packages/bowhead_core/src/bowhead_core/account_deletion.py").read_text(encoding="utf-8"))
    assert _method_calls(account_tree, "retire_account_conn", "_bump_user_account_epoch_conn")
    assert _method_calls(account_tree, "retire_account_conn", "_invalidate_all_chat_auth_session_generations_conn")
    assert _method_calls(
        user_tree,
        "create_credential_recovery_token",
        "_bump_user_account_epoch_conn",
    )
    assert _method_calls(
        user_tree,
        "complete_credential_recovery",
        "_bump_user_account_epoch_conn",
    )
    assert _method_calls(
        user_tree,
        "update_user_password",
        "_bump_user_account_epoch_conn",
    )
    assert _method_calls(
        user_tree,
        "revoke_all_refresh_for_user",
        "_bump_user_account_epoch_conn",
    )
    assert _method_calls(
        user_tree,
        "set_workspace_status",
        "_bump_team_membership_epoch_conn",
    )
    assert _method_calls(
        user_tree,
        "archive_governed_workspace",
        "_bump_team_membership_epoch_conn",
    )
    assert _method_calls(
        user_tree,
        "restore_governed_workspace",
        "_bump_team_membership_epoch_conn",
    )
    assert _method_calls(
        access_tree,
        "_bump_policy_epoch_conn",
        "bump_terminal_auth_epoch",
    )
    assert _method_calls(
        access_tree,
        "add_group_member",
        "_bump_membership_epoch_conn",
    )
    assert _method_calls(
        access_tree,
        "remove_group_member",
        "_bump_membership_epoch_conn",
    )
    assert _method_calls(auth_tree, "reset_password", "update_user_password")


def test_chat_session_and_user_source_helpers_have_exact_callers() -> None:
    session_tree = ast.parse(
        (_ROOT / "packages/bowhead_core/src/bowhead_core/session_store.py").read_text(
            encoding="utf-8",
        )
    )
    terminal_tree = ast.parse(
        (_ROOT / "packages/bowhead_core/src/bowhead_core/terminal_protocol_store.py").read_text(
            encoding="utf-8",
        )
    )
    gateway_main_tree = ast.parse(
        (_ROOT / "packages/bowhead_gateway/src/bowhead_gateway/main.py").read_text(
            encoding="utf-8",
        )
    )
    assert _call_owners(
        session_tree,
        "_ensure_chat_session_in_tx",
    ) == {"ensure_session", "finalize_started_chat_submission"}
    assert _call_owners(
        terminal_tree,
        "_ensure_chat_session_in_tx",
    ) == {"admit_or_steer_v1"}
    assert _call_owners(
        session_tree,
        "_insert_chat_turn_user_source_in_tx",
    ) == {
        "_materialize_blocked_reference_recovery_child_in_tx",
        "finalize_started_chat_submission",
        "materialize_reference_steer_follow_up",
        "materialize_steer_follow_up",
        "recover_expired_chat_spawn_dispatches",
        "record_turn",
    }
    assert _call_owners(
        terminal_tree,
        "_insert_chat_turn_user_source_in_tx",
    ) == {"_insert_admitted_turn"}
    assert (
        sum(
            isinstance(node, ast.Call)
            and (
                (isinstance(node.func, ast.Attribute) and node.func.attr == "cleanup_stale_running_turns")
                or (isinstance(node.func, ast.Name) and node.func.id == "cleanup_stale_running_turns")
            )
            for node in ast.walk(gateway_main_tree)
        )
        == 1
    )
    assert _call_owners(
        gateway_main_tree,
        "cleanup_stale_running_turns",
    ) == {"lifespan", "_chat_submission_maintenance_loop"}


def _method_calls(tree: ast.AST, owner: str, callee: str) -> bool:
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or node.name != owner:
            continue
        return any(
            isinstance(child, ast.Call)
            and (
                (isinstance(child.func, ast.Attribute) and child.func.attr == callee)
                or (isinstance(child.func, ast.Name) and child.func.id == callee)
            )
            for child in ast.walk(node)
        )
    return False


def _call_owners(tree: ast.AST, callee: str) -> set[str]:
    owners: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if any(
            isinstance(child, ast.Call)
            and (
                (isinstance(child.func, ast.Attribute) and child.func.attr == callee)
                or (isinstance(child.func, ast.Name) and child.func.id == callee)
            )
            for child in ast.walk(node)
        ):
            owners.add(node.name)
    return owners
