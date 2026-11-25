"""A delivered file is revised as a file over the real chat path (the original regression shape).

Turn 1 delivered an HTML file. Turn 2 asks to fix charts in it: Lead sees the file in
<delivered_files>, dispatches compute with revise_files (even naming report delivery), and
the helper runs without Canvas-writing tools. The Turn ends in chat and adds nothing to Canvas.
"""

import json
from types import SimpleNamespace

import pytest
import respx
from bowhead_core.artifact_store import ArtifactStore, LocalBackend
from bowhead_core.canvas_store import CanvasStore
from test_ordinary_lead_question_http import call, response
from test_reference_http_start import enable_lead_writer, rows, settled
from test_reference_http_start import http_start as http_start
from test_reference_http_start import receiver_case as receiver_case
from test_reference_http_start import start_case as start_case

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
    pytest.mark.parametrize("start_case", ["team"], indirect=True),
    pytest.mark.parametrize("receiver_case", ["admission"], indirect=True),
]


async def _send(case, text: str, respond, *, session_id: str | None = None) -> SimpleNamespace:
    payload = {"workspace_id": case.fixture.scope.workspace_id, "message": text, **({"session_id": session_id} if session_id else {})}
    with respx.mock() as mock:
        mock.post(url__regex=r"https://api\..*").mock(side_effect=respond)
        accepted = await case.client.post("/api/v1/chat/send", json=payload, headers={"Content-Type": "application/json"})
        assert accepted.status_code == 200, accepted.text
        turn_id = accepted.json()["turn_id"]
        await settled(turn_id)
    return SimpleNamespace(turn_id=turn_id, turn=await case.fixture.case.store.get_turn(turn_id))


def _tool_names(body: dict) -> set[str]:
    return {item["function"]["name"] for item in body.get("tools", [])}


async def test_delivered_file_is_revised_without_touching_the_canvas(http_start: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    case = http_start
    monkeypatch.setenv("FEATURE_REVISION_TARGETS", "1")
    enable_lead_writer(case, monkeypatch)
    orchestrator = case.app.state.orchestrator
    artifacts = ArtifactStore(str(case.path), LocalBackend(case.path.parent / "revision-target-files"))
    await artifacts.initialize()
    case.app.state.artifact_store = orchestrator.artifact_store = artifacts

    def first_turn(request):
        body = json.loads(request.content)
        return response(body, {"role": "assistant", "content": "The RBU HTML report is ready."}, "stop")

    first = await _send(case, "Build an HTML report for RBU.", first_turn)
    assert first.turn.status == "completed", first.turn.error
    session_id = next(row["session_id"] for row in rows(case, "turns") if row["id"] == first.turn_id)
    delivered = await artifacts.put_bytes(
        content=b"<html><body><div id='kpi'></div></body></html>",
        filename="rbu_operating_report_2026_detailed.html",
        mime="text/html",
        created_by=case.fixture.scope.user_id,
        source="python_sandbox",
        kind="deliverable",
        created_by_turn=first.turn_id,
        workspace_id=case.fixture.scope.workspace_id,
    )

    lead_calls: list[dict] = []
    helper_calls: list[dict] = []

    def second_turn(request):
        body = json.loads(request.content)
        if not _tool_names(body):
            return response(body, {"role": "assistant", "content": "Chart fix"}, "stop")
        if "subagent_spawn" in _tool_names(body):
            lead_calls.append(body)
            if len(lead_calls) == 1:
                arguments = {
                    "profile": "compute",
                    "task": "Fix the later charts in the delivered HTML report.",
                    "required_source_urls": [],
                    "revise_files": [delivered.id],
                    "revise_scope": ["later charts"],
                    "delivery": "report",
                }
                return response(body, call("revise-html", "subagent_spawn", arguments), "tool_calls")
            return response(body, {"role": "assistant", "content": "The HTML report was updated."}, "stop")
        helper_calls.append(body)
        return response(body, {"role": "assistant", "content": "Revised the later charts and saved a new version."}, "stop")

    second = await _send(case, "The later charts did not render; fix them.", second_turn, session_id=session_id)
    assert second.turn.status == "completed", second.turn.error
    first_request = json.dumps(lead_calls[0], ensure_ascii=False)
    assert "<delivered_files>" in first_request and delivered.id in first_request
    assert len(helper_calls) == 1
    helper_request = json.dumps(helper_calls[0], ensure_ascii=False)
    assert "Revision target: delivered files" in helper_request and "later charts" in helper_request
    assert not _tool_names(helper_calls[0]) & {"write_report", "add_chart", "edit_chart", "remove_chart"}, _tool_names(helper_calls[0])
    events = [row for row in rows(case, "turn_events") if row["turn_id"] == second.turn_id]
    summaries = [json.loads(row["payload_json"]) for row in events if row["event_type"] == "lead.subagent_summary"]
    assert summaries and summaries[0]["revise_files"] == [delivered.id] and summaries[0]["delivery"] == "chat"
    done = [json.loads(row["payload_json"]) for row in events if row["event_type"] == "done"]
    assert len(done) == 1 and done[0]["status"] == "completed"
    assert done[0].get("response_kind") != "canvas", done[0]


async def test_a_file_revision_answer_does_not_point_to_the_unchanged_canvas(http_start: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression check M2 (revision-target design §9): the revised HTML file is the report.

    The helper records version 2 in this Turn and Lead's answer names it, then adds "完整报告见画布" although the
    Canvas did not change. The answer keeps the version sentence, drops the pointer, and live, persisted and replayed
    answers agree. The fixture has a Canvas store, so the terminal read can state that this Turn changed no report
    section; without that evidence the §9 branch keeps the pointer. Its other inputs (a ledger that counts this
    Turn's code_block sections, as on 116) are covered by test_m2_canvas_pointer_reconcile.
    """
    case = http_start
    monkeypatch.setenv("FEATURE_REVISION_TARGETS", "1")
    enable_lead_writer(case, monkeypatch)
    orchestrator = case.app.state.orchestrator
    artifacts = ArtifactStore(str(case.path), LocalBackend(case.path.parent / "revision-target-pointer-files"))
    await artifacts.initialize()
    case.app.state.artifact_store = orchestrator.artifact_store = artifacts
    canvas_store = CanvasStore(str(case.path))
    await canvas_store.initialize()
    monkeypatch.setattr(orchestrator, "canvas_store", canvas_store)
    case.app.state.canvas_store = canvas_store

    def first_turn(request):
        return response(json.loads(request.content), {"role": "assistant", "content": "The RBU HTML report is ready."}, "stop")

    first = await _send(case, "Build an HTML report for RBU.", first_turn)
    session_id = next(row["session_id"] for row in rows(case, "turns") if row["id"] == first.turn_id)
    fields = {"mime": "text/html", "created_by": case.fixture.scope.user_id, "source": "python_sandbox", "kind": "deliverable"}
    fields |= {"workspace_id": case.fixture.scope.workspace_id, "filename": "rbu_operating_report_2026_detailed.html"}
    delivered = await artifacts.put_bytes(content=b"<html><body>v1</body></html>", created_by_turn=first.turn_id, **fields)
    answer = "第 2 版 HTML 报告的后半部分图表已修复：rbu_operating_report_2026_detailed.html。完整报告见画布。"
    lead_calls: list[dict] = []

    async def write_version() -> None:
        current = next(row["id"] for row in rows(case, "turns") if row["session_id"] == session_id and row["id"] != first.turn_id)
        await artifacts.put_bytes(content=b"<html><body>v2</body></html>", created_by_turn=current, version_of_target=delivered.id, **fields)

    async def second_turn(request):
        body = json.loads(request.content)
        if not _tool_names(body):
            return response(body, {"role": "assistant", "content": "Chart fix"}, "stop")
        if "subagent_spawn" in _tool_names(body):
            lead_calls.append(body)
            if len(lead_calls) == 1:
                arguments = {
                    "profile": "compute",
                    "task": "Fix the later charts in the delivered HTML report.",
                    "required_source_urls": [],
                    "revise_files": [delivered.id],
                    "canvas_mode": "revise",
                }
                return response(body, call("revise-html", "subagent_spawn", arguments), "tool_calls")
            return response(body, {"role": "assistant", "content": answer}, "stop")
        await write_version()
        return response(body, {"role": "assistant", "content": "Saved version 2 with the later charts fixed."}, "stop")

    second = await _send(case, "The later charts did not render; fix them.", second_turn, session_id=session_id)
    assert second.turn.status == "completed", second.turn.error
    persisted = second.turn.final_answer or ""
    assert await artifacts.turn_has_deliverable(second.turn_id), "The Turn delivered version 2, so it keeps the report surface"
    # The test helper declares no outputs, so other delivery checks may append their own notes after the answer.
    assert persisted.startswith("第 2 版 HTML 报告的后半部分图表已修复：rbu_operating_report_2026_detailed.html。"), persisted
    assert "见画布" not in persisted, "The persisted answer drops the pointer to the unchanged Canvas"
    events = [row for row in rows(case, "turn_events") if row["turn_id"] == second.turn_id]
    finals = [json.loads(row["payload_json"]) for row in events if row["event_type"] == "final_answer"]
    assert finals and finals[-1]["text"] == persisted, "Live and replayed answers end on the persisted one"
    # The pointer is retracted by a corrective final_answer that replaces Lead's streamed one (M2/2a).
    assert finals[-1].get("reason_code") == "canvas_pointer_reconciled", finals[-1]
    done = [json.loads(row["payload_json"]) for row in events if row["event_type"] == "done"]
    assert len(done) == 1 and "见画布" not in json.dumps(done[0], ensure_ascii=False)
