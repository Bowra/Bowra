"""A chart delivered in chat is read and edited in place in a later Turn.

Reproduces the November 2026 dogfood shape: a python_sandbox chart with a
Datarc workspace context, hidden for chat delivery at the end of its Turn,
whose session source ref carries only the tool-call identity.
"""

import copy
from types import SimpleNamespace

import jwt
import pytest
from bowhead_core.canvas_store import CanvasStore
from bowhead_core.chart.authoring_tools import edit_chart, read_chart
from bowhead_core.results_report_relations import project_report_relations
from bowhead_core.session_store import SessionStore
from bowhead_core.turn_log_store import TurnLogStore
from bowhead_core.user_store import UserStore
from bowhead_gateway.chart_authoring_projection import project_chart_for_authoring
from bowhead_gateway.report_read_access import ReportReadAccess
from bowhead_gateway.source_access_projection import project_event_payloads_for_viewer, project_live_event_payload_for_viewer

pytestmark = pytest.mark.integration

SECRET = "synthetic-chat-chart-in-place-secret"
DATA = "art_85365dc88fa4"


class Artifacts:
    def __init__(self, owner: str, workspace_id: str) -> None:
        self.owner = owner
        self.workspace_id = workspace_id

    async def get_many(self, ids, *, include_deleted=False):
        return [
            SimpleNamespace(
                id=item, status="ready", created_by=self.owner, workspace_id=self.workspace_id, source="python_sandbox", derived_from=None
            )
            for item in ids
            if item == DATA
        ]


class Bus:
    def __init__(self) -> None:
        self.events = []

    async def emit(self, event):
        self.events.append(event)
        return True


def chart_payload() -> dict:
    return {
        "spec": {
            "kind": "bar",
            "title": "Primes per hundred",
            "data_artifact_id": DATA,
            "row_count": 2,
            "columns": [
                {"key": "bucket", "header": "Bucket", "type": "categorical"},
                {"key": "primes", "header": "Primes", "type": "numeric"},
            ],
            "rows": [{"bucket": "1-100", "primes": 25}, {"bucket": "101-200", "primes": 21}],
            "show_data_labels": True,
            "source_meta": {"id": DATA, "tool": "python_sandbox", "query": "primes per hundred", "artifact_id": DATA, "turn_id": "t_draw"},
        },
        "data_source": {"tool": "datarc", "context_id": "1", "context_name": "Retail"},
    }


@pytest.fixture
async def chat_chart(tmp_path, initialize_migrated_core_database, monkeypatch):
    from bowhead_core.agents import _write_guard
    from bowhead_gateway.routes import canvas as canvas_routes

    from bowhead_gateway import source_access_projection

    monkeypatch.setenv("BOWHEAD_JWT_SECRET", SECRET)
    monkeypatch.setattr(_write_guard, "check_write_trust_class", lambda: None)
    monkeypatch.setattr(canvas_routes, "kb_file_access_for_request", lambda request: None)
    monkeypatch.setattr(source_access_projection, "kb_file_access_for_request", lambda request: None)
    path = tmp_path / "bowhead.db"
    initialize_migrated_core_database(path)
    users, sessions, canvases, log = UserStore(path), SessionStore(path), CanvasStore(path), TurnLogStore(path)
    for store in (users, sessions, canvases, log):
        await store.initialize()
    team = await users.create_team(name="Charts", max_seats=5)
    user = await users.create_user(team_id=team.id, email="charts@example.com")
    space = await users.create_local_workspace(team_id=team.id, name="Charts", creator_user_id=user.id)
    session = await sessions.ensure_session(session_id="s_chat_chart", workspace_id=space, user_id=user.id)
    await sessions.record_turn(turn_id="t_draw", session_id=session.id, user_text="Count primes per hundred as a bar chart")
    await sessions.finalize_turn(session_id=session.id, current_turn_id="t_draw", next_turn_id=None)
    await sessions.record_turn(turn_id="t_recolor", session_id=session.id, user_text="Make the bars green")
    # Like the observed session, the sandbox source ref carries the tool-call
    # identity and no artifact ID, so it cannot fill an authorization gap.
    await log.record(turn_id="t_draw", event_type="tool_call", payload={"tool_name": "python_sandbox", "call_id": "call_primes", "arguments": {}})
    await log.record(turn_id="t_draw", event_type="tool_result", payload={"tool_name": "python_sandbox", "call_id": "call_primes", "result_meta": {}})
    canvas = await canvases.ensure_canvas(session_id=session.id, workspace_id=space, user_id=user.id)
    section = await canvases.append_section(canvas_id=canvas.id, kind="chart", payload=chart_payload(), by_turn_id="t_draw")
    assert await canvases.mark_turn_hidden_for_chat_mode("t_draw", rationale="declared: chat (agent/user decision)") == [section.id]
    token = jwt.encode({"sub": user.id}, SECRET, algorithm="HS256")
    artifacts = Artifacts(user.id, space)
    request = SimpleNamespace(
        headers={"Authorization": "Bearer " + token},
        cookies={},
        app=SimpleNamespace(
            state=SimpleNamespace(
                user_store=users,
                session_store=sessions,
                canvas_store=canvases,
                artifact_store=artifacts,
                artifact_fetch_secret="synthetic-fetch-secret",
                turn_log_store=log,
                access_control_store=None,
            )
        ),
    )

    async def chat_route_projection(target):
        # The closure the chat route installs on TurnRequest.
        return await project_chart_for_authoring(request, session.id, target)

    ctx = SimpleNamespace(
        canvas_store=canvases,
        session_store=sessions,
        artifact_store=artifacts,
        session_id=session.id,
        workspace_id=space,
        team_id=team.id,
        user_id=user.id,
        turn_id="t_recolor",
        parent_bus=Bus(),
        chart_authoring_projection=chat_route_projection,
        report_read_access=None,
    )
    return SimpleNamespace(request=request, ctx=ctx, canvas=canvas, section=section, artifacts=artifacts, store=canvases, team=team)


async def test_chat_delivered_python_chart_is_read_and_edited_in_place_in_a_later_turn(chat_chart):
    ctx, store, section = chat_chart.ctx, chat_chart.store, chat_chart.section
    read = await read_chart({"section_id": section.id}, ctx)
    assert "error" not in read, read
    assert read["rev"] == 1 and read["views"][0]["sample"][0] == {"bucket": "1-100", "primes": 25}
    changed = await edit_chart({"section_id": section.id, "expected_rev": 1, "changes": {"title": "Primes per hundred (green)"}}, ctx)
    assert "error" not in changed, changed
    assert changed["rev"] == 2 and changed["chart_guarded"] is False
    [event] = ctx.parent_bus.events
    assert event.event_type == "canvas.section_patched" and event.payload["hidden_for_chat_mode"] is True
    stored = next(s for s in await store.list_sections(chat_chart.canvas.id, include_hidden=True) if s.id == section.id)
    assert stored.rev == 2 and stored.hidden_for_chat_mode
    assert stored.payload["spec"]["title"] == "Primes per hundred (green)"
    assert stored.payload["spec"]["rows"] == section.payload["spec"]["rows"]
    assert stored.payload["spec"]["source_meta"] == section.payload["spec"]["source_meta"]
    # The chart stays a chat figure: Canvas and the report closure still exclude it.
    assert section.id not in {s.id for s in await store.list_sections(chat_chart.canvas.id)}
    assert DATA not in project_report_relations([stored]).closure_ids

    # The actor-bound report authority used by resumed and terminal Turns agrees.
    scoped = copy.copy(ctx)
    scoped.chart_authoring_projection = None
    scoped.report_read_access = ReportReadAccess(chat_chart.request, ctx.session_id, ctx.workspace_id, ctx.team_id, ctx.user_id)
    assert (await read_chart({"section_id": section.id}, scoped))["rev"] == 2


async def test_chat_delivered_chart_still_denies_an_unauthorized_source(chat_chart):
    ctx, store, section = chat_chart.ctx, chat_chart.store, chat_chart.section
    chat_chart.artifacts.owner = "usr_someone_else"
    denied = await read_chart({"section_id": section.id}, ctx)
    assert denied["error"] == "chart_source_projection_unavailable" and denied["reason"] == "source_access_denied"
    refused = await edit_chart({"section_id": section.id, "expected_rev": 1, "changes": {"title": "Denied"}}, ctx)
    assert refused["reason"] == "source_access_denied"
    stored = next(s for s in await store.list_sections(chat_chart.canvas.id, include_hidden=True) if s.id == section.id)
    assert stored.rev == 1 and stored.payload == section.payload
    assert not ctx.parent_bus.events


async def test_earlier_chat_answer_shows_the_edited_chart_live_and_on_replay(chat_chart):
    ctx, store, section = chat_chart.ctx, chat_chart.store, chat_chart.section
    delivered = {"kind": "chart", "id": section.id, "spec": copy.deepcopy(section.payload["spec"])}
    final = {"inline_artifacts": [delivered]}
    await edit_chart({"section_id": section.id, "expected_rev": 1, "changes": {"title": "Primes per hundred (green)"}}, ctx)

    [replayed] = await project_event_payloads_for_viewer(chat_chart.request, "t_draw", [("lead.final.payload", final)])
    shown = replayed["inline_artifacts"][0]
    assert shown["id"] == section.id and shown["rev"] == 2
    assert shown["spec"]["title"] == "Primes per hundred (green)"
    assert shown["spec"]["rows"][0] == {"bucket": "1-100", "primes": 25}
    # The substituted spec still passes the viewer-safe source projection.
    assert shown["spec"]["source_meta"]["id"] != DATA
    live = await project_live_event_payload_for_viewer(chat_chart.request, "t_draw", "lead.final.payload", final)
    assert live["inline_artifacts"] == replayed["inline_artifacts"]
    assert final["inline_artifacts"][0]["spec"]["title"] == "Primes per hundred"

    # The later Turn's live update keeps its chat-only marker through projection.
    [event] = ctx.parent_bus.events
    patched = await project_live_event_payload_for_viewer(chat_chart.request, "t_recolor", event.event_type, event.payload)
    assert patched["hidden_for_chat_mode"] is True and patched["rev"] == 2
    assert patched["patch"]["spec"]["title"] == "Primes per hundred (green)"

    # A superseded chart is no longer current: the answer keeps what it delivered.
    await store.mark_sections_superseded(section_ids=[section.id], superseded_by_turn="t_recolor")
    [kept] = await project_event_payloads_for_viewer(chat_chart.request, "t_draw", [("lead.final.payload", final)])
    assert kept["inline_artifacts"][0]["spec"]["title"] == "Primes per hundred" and "rev" not in kept["inline_artifacts"][0]


async def test_exact_hex_survives_authorized_read_live_patch_and_saved_chat_replay(chat_chart):
    ctx, section = chat_chart.ctx, chat_chart.section
    final = {"inline_artifacts": [{"kind": "chart", "id": section.id, "spec": copy.deepcopy(section.payload["spec"])}]}
    changed = await edit_chart({"section_id": section.id, "expected_rev": 1, "changes": {"series_colors": {"primes": "#00a651"}}}, ctx)
    assert changed["series_colors"] == {"primes": "#00A651"}
    inspected = await read_chart({"section_id": section.id}, ctx)
    assert inspected["views"][0]["editable_color_series"][0]["color_hex"] == "#00A651"
    [event] = ctx.parent_bus.events
    live = await project_live_event_payload_for_viewer(chat_chart.request, "t_recolor", event.event_type, event.payload)
    [replayed] = await project_event_payloads_for_viewer(chat_chart.request, "t_draw", [("lead.final.payload", final)])
    assert live["patch"]["spec"]["columns"][1]["color_hex"] == "#00A651"
    assert replayed["inline_artifacts"][0]["spec"]["columns"][1]["color_hex"] == "#00A651"
    assert replayed["inline_artifacts"][0]["spec"]["rows"] == section.payload["spec"]["rows"]
    assert "color_hex" not in final["inline_artifacts"][0]["spec"]["columns"][1]
