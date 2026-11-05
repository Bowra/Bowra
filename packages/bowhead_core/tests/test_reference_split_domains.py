"""Run the original reference HTTP flows against an actually migrated layout."""

import json
import shutil
import sqlite3
from contextlib import closing
from types import SimpleNamespace
from uuid import uuid4

import anyio
import pytest
import respx
import test_reference_http_start as http_flows
import test_reference_temporary_methods as method_flows
import test_skill_read_receiver_integration as receiver_flows
from bowhead_cli.migrate import upgrade
from bowhead_core._db import close_pooled_readers
from bowhead_core.agent_execution_binding import AgentExecutionBindingStore
from bowhead_core.session_store import SessionStore
from bowhead_core.skill_method_policy import task_method_reads_ready
from bowhead_core.skill_reference_context import ReferenceContextUnavailable, reference_turn_requires_context, refresh_reference_skill_context
from bowhead_core.sqlite_domain_runtime import connect_sync
from bowhead_core.sqlite_domains import DOMAIN_TARGET_FILES, TABLE_DOMAINS, domain_database_path
from cryptography.fernet import Fernet
from test_reference_http_start import http_start as http_start
from test_reference_http_start import prohibit_unmocked_network as prohibit_unmocked_network
from test_reference_http_start import start_case as start_case
from test_skill_extension_runtime import tool_response

legacy_receiver_case = receiver_flows.receiver_case
pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
    pytest.mark.parametrize("start_case", ["team"], indirect=True),
    pytest.mark.parametrize("legacy_receiver_case", ["admission"], indirect=True),
]


@pytest.fixture
async def receiver_case(legacy_receiver_case, monkeypatch):
    fixture = legacy_receiver_case
    path = fixture.case.fixture.db_path
    await close_pooled_readers()
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", Fernet.generate_key().decode())
    await anyio.to_thread.run_sync(upgrade, f"sqlite:///{path}")
    # Reconstruct stores at startup, after the real coordinator moves their tables.
    fixture.case.store = SessionStore(path)
    fixture.case.bindings = AgentExecutionBindingStore(path)

    def rows(case, table):
        with closing(sqlite3.connect(domain_database_path(case.path, TABLE_DOMAINS[table]))) as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute(f'SELECT * FROM "{table}"')]

    def activate(case):
        with closing(connect_sync(case.case.fixture.db_path)) as db:
            db.execute("UPDATE team_subagent_schema_meta SET release_semantics_activated_at='2026-09-14T00:00:00+00:00' WHERE singleton=1")
            db.commit()

    monkeypatch.setattr(http_flows, "rows", rows)
    monkeypatch.setattr(method_flows, "rows", rows)
    monkeypatch.setattr(http_flows, "activate", activate)
    return fixture


async def test_split_method_readiness_uses_the_guard_owner(receiver_case):
    path = receiver_case.case.fixture.db_path
    with closing(sqlite3.connect(path)) as core:
        assert core.execute("SELECT 1 FROM sqlite_master WHERE name='trg_reference_method_composition'").fetchone() is None
    assert await task_method_reads_ready(path)


async def test_split_method_readiness_does_not_trust_a_guard_in_another_domain(receiver_case):
    path = receiver_case.case.fixture.db_path
    with closing(sqlite3.connect(domain_database_path(path, "execution"))) as execution:
        execution.execute("DROP TRIGGER trg_reference_method_composition")
    with closing(sqlite3.connect(path)) as core:
        core.execute("CREATE TRIGGER trg_reference_method_composition BEFORE INSERT ON teams BEGIN SELECT 1; END")
    assert not await task_method_reads_ready(path)


async def test_split_unconfigured_method_catalog_send_and_exact_receiver(http_start):
    await method_flows.test_unconfigured_standalone_method_reaches_the_exact_agent_without_assignment(http_start, "team")


async def test_split_automatic_method_receiver_keeps_original_eligibility(http_start):
    await method_flows.test_automatic_selection_uses_the_same_production_descriptor_eligibility(http_start)


async def test_split_assigned_method_question_resumes_its_original_execution(http_start, monkeypatch):
    await http_flows.test_lead_delegation_http_question_resumes_the_original_execution(
        http_start, monkeypatch, decision="answered", skills=True, source_kind="team"
    )


async def test_split_assigned_method_reads_and_delivers_once_without_a_question(http_start, monkeypatch):
    case = http_start
    http_flows.enable_lead_writer(case, monkeypatch)
    calls = []

    def respond(request):
        body = json.loads(request.content)
        calls.append(body)
        if len(calls) == 1:
            return tool_response(
                "subagent_spawn",
                {"profile": case.fixture.case.args["reference_start"].profile_name, "task": "Read the selected method", "required_source_urls": []},
                stream=body.get("stream", False),
            )
        if len(calls) == 2:
            return tool_response("skill_read", {"name": "method"}, stream=body.get("stream", False))
        return http_flows.answer_response(request)

    with respx.mock() as mock:
        mock.post(url__regex=r"https://api\..*").mock(side_effect=respond)
        accepted = await case.client.post("/api/v1/chat/send", json=http_flows.payload(case))
        assert accepted.status_code == 200, accepted.text
        turn_id = accepted.json()["turn_id"]
        await http_flows.settled(turn_id)
    turn = await case.fixture.case.store.get_turn(turn_id)
    assert turn.status == "completed", turn.error_kind
    assert any("BODY_method" in json.dumps(call) for call in calls[2:])
    executions = [row for row in http_flows.rows(case, "turn_agent_execution") if row["turn_id"] == turn_id]
    assert len(executions) == 1 and executions[0]["status"] == "completed"
    events = [row for row in http_flows.rows(case, "turn_events") if row["turn_id"] == turn_id]
    assert [json.loads(row["payload_json"])["status"] for row in events if row["event_type"] == "done"] == ["completed"]
    started = [json.loads(row["payload_json"]) for row in events if row["event_type"] == "step_started"]
    assert sum(step.get("tool_name") == "subagent_spawn" for step in started) == 1
    assert sum(step.get("tool_name") == "skill_read" for step in started) == 1
    finished = [json.loads(row["payload_json"]) for row in events if row["event_type"] == "step_done"]
    assert [step["state"] for step in finished if step.get("tool_name") == "skill_read"] == ["success"]
    assert not [row for row in events if row["event_type"] == "clarification_required"]


@pytest.mark.parametrize("change", ["revoked", "stale_version", "missing_method_guard", "missing_epoch_guard", "missing_baseline"])
async def test_split_temporary_method_preserves_admission_fences(http_start, monkeypatch, change):
    case = http_start
    _, body = method_flows.standalone_request(case)
    if change == "revoked":
        monkeypatch.setenv("BOWHEAD_RESOURCE_GRANTS_ENFORCE", "0")
        with closing(connect_sync(case.path)) as db:
            db.execute(
                "INSERT INTO resource_grants(id,team_id,resource_kind,resource_id,principal_type,principal_id,permission,effect,created_at,updated_at) "
                "VALUES (?,?,'skill','temporary_method','user',?,'use','deny','2026-11-05','2026-11-05')",
                ("rg_" + uuid4().hex, case.fixture.scope.team_id, case.fixture.scope.user_id),
            )
            db.commit()
    elif change == "stale_version":
        body["input_document"]["nodes"][-1]["resource"]["version"] = "sha256:" + "f" * 64
    elif change.startswith("missing_") and change.endswith("_guard"):
        trigger = "trg_reference_method_composition" if change == "missing_method_guard" else "trg_reference_extension_epoch_check"
        with closing(sqlite3.connect(domain_database_path(case.path, "execution"))) as db:
            db.execute(f'DROP TRIGGER "{trigger}"')
    else:
        shutil.rmtree(case.fixture.root / "method")
    before = http_flows.acceptance_counts(case)
    previous_turns = {row["id"] for row in http_flows.rows(case, "turns")}
    denied = await case.client.post("/api/v1/chat/send", json=body)
    if change == "missing_baseline":
        assert denied.status_code == 503 and denied.json()["detail"]["error"]["code"] == "turn_dispatch_unavailable"
        # A startup refusal retains its failed admission receipt, without starting the Agent.
        failed = [row for row in http_flows.rows(case, "turns") if row["id"] not in previous_turns]
        assert len(failed) == 1 and failed[0]["status"] == "failed" and failed[0]["error_kind"] == "dispatch_start_failed"
        execution = [row for row in http_flows.rows(case, "turn_agent_execution") if row["turn_id"] == failed[0]["id"]]
        assert len(execution) == 1 and execution[0]["status"] == "failed" and execution[0]["started_at"] is None
        dispatch = [row for row in http_flows.rows(case, "chat_turn_spawn_dispatch") if row["turn_id"] == failed[0]["id"]]
        assert len(dispatch) == 1 and dispatch[0]["state"] == "failed" and dispatch[0]["owner_id"] is None
    else:
        assert denied.status_code in {403, 404, 409}, denied.text
        assert http_flows.acceptance_counts(case) == before
    assert not http_flows.rows(case, "chat_reference_model_invocations")


@pytest.mark.parametrize("association", ["same", "foreign_access", "foreign_source", "unregistered_sibling"])
async def test_split_context_recognizes_only_one_installation(receiver_case, tmp_path, association):
    path = receiver_case.case.fixture.db_path
    store = receiver_case.case.store
    await store.ensure_session(session_id="s_path", workspace_id=receiver_case.scope.workspace_id, user_id=receiver_case.scope.user_id)
    await store.record_turn(turn_id="t_path", session_id="s_path", user_text="Synthetic ordinary task")
    ctx = SimpleNamespace(
        turn_id="t_path",
        reference_source_db_path=str(path),
        session_store=store,
        reference_skill_access=SimpleNamespace(source_db_path=path),
        reference_skill_required=False,
    )
    foreign = tmp_path / "foreign"
    if association in {"foreign_access", "foreign_source"}:
        foreign.mkdir()
        for filename in DOMAIN_TARGET_FILES.values():
            with (
                closing(sqlite3.connect((path.parent / filename).as_uri() + "?mode=ro", uri=True)) as source,
                closing(sqlite3.connect(foreign / filename)) as target,
            ):
                source.backup(target)
        shutil.copyfile(path.parent / "sqlite-layout.json", foreign / "sqlite-layout.json")
        assert await reference_turn_requires_context(domain_database_path(foreign / "bowhead.db", "execution"), turn_id="t_path") is False
    if association == "foreign_access":
        ctx.reference_skill_access.source_db_path = foreign / "bowhead.db"
    elif association == "foreign_source":
        ctx.reference_source_db_path = str(foreign / "bowhead.db")
    elif association == "unregistered_sibling":
        ctx.reference_skill_access.source_db_path = path.with_name("other.db")
    if association == "same":
        await refresh_reference_skill_context(ctx)
        assert ctx.reference_skill_required is False
    else:
        with pytest.raises(ReferenceContextUnavailable):
            await refresh_reference_skill_context(ctx)
        if association == "unregistered_sibling":
            assert not path.with_name("other.db").exists()
