"""Native Lead Prompt dispatch and original-card continuation contracts."""

import asyncio
import base64
import io
import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import respx
from bowhead_core.agents.clarification_tool import register_clarification_tool
from bowhead_core.subagent import PROFILES
from bowhead_gateway.routes import chat, clarifications
from test_reference_http_start import answer_response, enable_lead_writer, lead_has_dispatched, rows, settled
from test_reference_http_start import http_start as http_start
from test_reference_http_start import prohibit_unmocked_network as prohibit_unmocked_network
from test_reference_http_start import receiver_case as receiver_case
from test_reference_http_start import start_case as start_case
from test_reference_prompt_http import _prompt_upload_body
from test_reference_prompt_http import prompt_http as prompt_http
from test_skill_extension_runtime import tool_response

from bowhead_core import events

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
    pytest.mark.parametrize("start_case", ["team"], indirect=True),
    pytest.mark.parametrize("receiver_case", ["admission_unused_sop"], indirect=True),
]


@pytest.fixture
async def lead_prompt(prompt_http, monkeypatch):
    p = prompt_http
    case = p.case
    enable_lead_writer(case, monkeypatch)
    register_clarification_tool(case.app.state.orchestrator.tool_registry)
    monkeypatch.setitem(PROFILES, "lead", replace(PROFILES["lead"], tool_whitelist=("subagent_spawn", "clarification_ask")))
    case.app.include_router(clarifications.router, prefix="/api/v1/clarifications")
    monkeypatch.setattr(events, "_default_registry", case.app.state.orchestrator.event_registry)
    return p


@pytest.mark.parametrize(
    "pause,with_file,read",
    [
        (None, False, True),
        ("lead", False, False),
        ("primary", False, False),
        ("primary", True, True),
        ("primary", "replaced", True),
        ("primary", "pdf", True),
        ("lead", "pdf", False),
        ("primary", "pdf_replaced", True),
        ("primary", "xlsx", True),
        ("lead", "xlsx", False),
        ("primary", "xlsx_replaced", True),
    ],
)
async def test_lead_prompt_retains_material_and_original_execution(lead_prompt, monkeypatch, pause, with_file, read):
    from bowhead_core.chat_reference_projection import read_reference_projection
    from bowhead_core.skill_model_invocation import ReferenceModelInvocationHook

    p = lead_prompt
    case = p.case
    lead_calls, primary_calls = [], []
    file_parses = []
    invocation_facts = []
    begin = ReferenceModelInvocationHook.begin

    async def check_pending(self, spec):
        attempt = await begin(self, spec)
        access = self.access
        facts = await read_reference_projection(
            access.source_db_path,
            scope=access.scope,
            turn_id=access.turn_id,
            visible_resource_identities=frozenset(),
            visibility_complete=True,
        )
        pending = [row for row in facts["model_invocations"] if row["stored_status"] == "pending"]
        assert len(pending) == 1
        assert pending[0]["status"] == "pending" and pending[0]["generation"] == access.generation
        assert facts["orchestration"]["execution_generation"] == access.generation
        assert facts["orchestration"]["turn_status"] == "running"
        assert {row["execution_id"] for row in facts["orchestration"]["agent_executions"] if row["is_primary"]} == {pending[0]["execution_id"]}
        invocation_facts.append(facts)
        return attempt

    monkeypatch.setattr(ReferenceModelInvocationHook, "begin", check_pending)
    if with_file:
        if with_file in {"pdf", "pdf_replaced"}:
            from pypdf import PdfWriter
            from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

            writer = PdfWriter()
            page = writer.add_blank_page(width=612, height=792)
            font = DictionaryObject(
                {
                    NameObject("/Type"): NameObject("/Font"),
                    NameObject("/Subtype"): NameObject("/Type1"),
                    NameObject("/BaseFont"): NameObject("/Helvetica"),
                }
            )
            page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})})
            content = DecodedStreamObject()
            content.set_data(b"BT /F1 12 Tf 20 700 Td (Original PDF profit is 42.) Tj ET")
            page[NameObject("/Contents")] = content
            output = io.BytesIO()
            writer.write(output)
            p.body = await _prompt_upload_body(p, content=output.getvalue(), filename="profit.pdf", mime="application/pdf")
        elif with_file in {"xlsx", "xlsx_replaced"}:
            from openpyxl import Workbook

            # Office document-role deferral must not change spreadsheet preview handling.
            workbook = Workbook()
            workbook.active.title = "Profit"
            workbook.active.append(["period", "profit"])
            workbook.active.append(["FY2026", 42])
            output = io.BytesIO()
            workbook.save(output)
            workbook.close()
            p.body = await _prompt_upload_body(
                p, content=output.getvalue(), filename="profit.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )
        else:
            p.body = await _prompt_upload_body(p)
        if with_file in {"pdf", "pdf_replaced", "xlsx", "xlsx_replaced"}:
            from bowhead_core import attachments_injector

            original_extract = attachments_injector.extract_attachment_bounded

            async def extract(art, data):
                prepared = await original_extract(art, data)
                if art.filename in {"profit.pdf", "profit.xlsx"}:
                    file_parses.append(prepared)
                return prepared

            monkeypatch.setattr(attachments_injector, "extract_attachment_bounded", extract)
        image = await case.app.state.artifact_store.put_bytes(
            content=b"original-prompt-image",
            filename="profit.png",
            mime="image/png",
            created_by=case.fixture.scope.user_id,
            workspace_id=case.fixture.scope.workspace_id,
        )
        with sqlite3.connect(case.path) as db:
            db.execute(
                "INSERT INTO artifact_upload_bindings(artifact_id,workspace_id,user_id,created_at) VALUES (?,?,?,?)",
                (image.id, case.fixture.scope.workspace_id, case.fixture.scope.user_id, datetime.now(UTC).isoformat()),
            )
        p.body["attachments"].append(image.id)
        p.body["attachment_refs"].append(
            {"fileId": image.id, "filename": image.filename, "mime": image.mime, "size": image.size, "referenceKind": "upload"}
        )
    question = {"questions": [{"question": "Which scope?", "type": "free_text", "options": []}]}

    def respond(request):
        body = json.loads(request.content)
        names = {tool["function"]["name"] for tool in body.get("tools", [])}
        if "subagent_spawn" in names:
            lead_calls.append(body)
            if pause == "lead" and len(lead_calls) == 1:
                return tool_response("clarification_ask", question, stream=body.get("stream", False))
            if not primary_calls:
                return tool_response(
                    "subagent_spawn",
                    {"agent_id": p.agent.id, "task": "Analyze profit using the selected Prompt.", "delegation_role": "primary"},
                    stream=body.get("stream", False),
                )
            return answer_response(request)
        if "skill_read" not in names:
            return answer_response(request)
        assert "attachment_read" not in names
        primary_calls.append(body)
        assert "REMOTE_PROFITABILITY_METHOD" in json.dumps(body)
        assert "mcp__" not in " ".join(names)
        if read and len(primary_calls) == 1:
            return tool_response("skill_read", {"name": "unused", "ref": "guide"}, stream=body.get("stream", False))
        if read:
            assert "BASELINE_GUIDE_unused" in json.dumps(body)
        if pause == "primary" and len(primary_calls) == (2 if read else 1):
            return tool_response(
                "subagent_return",
                {"payload": {"error": {"kind": "needs_user_input", "message": "Choose scope.", "clarification_payload": question}}},
                stream=body.get("stream", False),
            )
        return answer_response(request)

    with respx.mock() as mock:
        mock.post(url__regex=r"https://api\..*").mock(side_effect=respond)
        response = await case.client.post("/api/v1/chat/send", json=p.body)
        assert response.status_code == 200, response.text
        turn_id = response.json()["turn_id"]
        await settled(turn_id)
        with sqlite3.connect(case.path) as db:
            plan = json.loads(db.execute("SELECT plan_json FROM chat_reference_plans WHERE turn_id=?", (turn_id,)).fetchone()[0])
            execution = db.execute("SELECT primary_execution_id FROM turn_agent_binding WHERE turn_id=?", (turn_id,)).fetchone()[0]
        assert plan["orchestration_contract"] == "lead-delegation.v1"
        if pause:
            assert (await case.fixture.case.store.get_turn(turn_id)).status == "clarification_pending"
            assert len(p.calls) == (0 if pause == "lead" else 1)
            with sqlite3.connect(case.path) as db:
                card = db.execute("SELECT request_id FROM clarification_requests WHERE turn_id=?", (turn_id,)).fetchone()[0]
            paused_evidence = await case.client.get(f"/api/v1/turns/{turn_id}/reference-evidence")
            assert paused_evidence.status_code == 200, paused_evidence.text
            expected_continuation = {
                "contract": "reference-lead-predispatch.v1" if pause == "lead" else "reference-lead-postdispatch.v1",
                "clarification_id": card,
                "execution_id": execution,
                "status": "pending",
                "resume_generation": None,
                "source_turn_id": turn_id,
                "association_basis": "persisted_binding",
                "model_invocation_ids": [],
                "task_physical_receipt_ids": [],
            }
            assert paused_evidence.json()["continuations"] == [expected_continuation]
            if with_file in {"replaced", "pdf_replaced", "xlsx_replaced"}:
                replaced_files = 0
                extension = {"pdf_replaced": ".pdf", "xlsx_replaced": ".xlsx"}.get(with_file)
                for file in (case.path.parent / "prompt-uploads").rglob("*"):
                    if file.is_file() and (extension is None or file.suffix == extension):
                        original_bytes = file.read_bytes()
                        if with_file == "pdf_replaced":
                            replacement = original_bytes.replace(b"profit is 42.", b"profit is 99.")
                        elif with_file == "xlsx_replaced":
                            # Preserve the file size; the content hash must detect replacement.
                            replacement = original_bytes[:-1] + bytes([original_bytes[-1] ^ 1])
                        else:
                            replacement = b"replacement-file-bytes"
                        assert replacement != original_bytes
                        file.write_bytes(replacement)
                        replaced_files += 1
                if extension:
                    assert replaced_files == 1
            monkeypatch.setenv("FEATURE_CHAT_REFERENCE_LEAD_DELEGATION", "0")
            monkeypatch.setenv("FEATURE_CHAT_REFERENCE_MCP_PROMPT", "0")
            resolved = await case.client.post(
                f"/api/v1/clarifications/{card}/resolve",
                json={"chosen_values": ["All teams"], "answers": [{"question": "Which scope?", "source": "custom", "answer": "All teams"}]},
            )
            assert resolved.status_code == 200, resolved.text
            if with_file in {"replaced", "pdf_replaced", "xlsx_replaced"}:
                with pytest.raises(RuntimeError, match="legacy_chat_durability_recovery_handoff"):
                    await settled(turn_id)

                def expire_worker():
                    with sqlite3.connect(case.path) as db:
                        db.execute(
                            "UPDATE chat_turn_spawn_dispatch SET lease_expires_at=? WHERE turn_id=?",
                            ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), turn_id),
                        )

                await asyncio.to_thread(expire_worker)
                assert await case.fixture.case.store.recover_expired_chat_spawn_dispatches() == [turn_id]
                assert (await case.fixture.case.store.get_turn(turn_id)).status == "failed"
                assert len(primary_calls) == 2 and len(p.calls) == 1
                if with_file in {"pdf_replaced", "xlsx_replaced"}:
                    assert len(file_parses) == 1
                assert not [row for row in rows(case, "chat_reference_lead_postdispatch_attempts") if row["turn_id"] == turn_id]
                failed_evidence = await case.client.get(f"/api/v1/turns/{turn_id}/reference-evidence")
                assert failed_evidence.status_code == 200, failed_evidence.text
                assert failed_evidence.json()["continuations"] == [{**expected_continuation, "status": "resolved"}]
                return
            await settled(turn_id)
            duplicate = await case.client.post(f"/api/v1/clarifications/{card}/resume_turn")
            assert duplicate.status_code == 200, duplicate.text
            assert "All teams" in json.dumps(primary_calls[-1] if pause == "primary" else lead_calls[1])
        assert (await case.fixture.case.store.get_turn(turn_id)).status == "completed"
        count = len(lead_calls) + len(primary_calls)
        replay = await case.client.post("/api/v1/chat/send", json=p.body)
        assert replay.status_code == 200 and replay.json() == response.json()
        assert len(lead_calls) + len(primary_calls) == count
    assert p.calls == [("profitability", {"period": "FY2026"})]
    # A Lead question pause asks then spawns; the selected Team Agent's plain answer (no peer Tool
    # call) is delivered directly without a Lead synthesis call (5006e9348). With a Skill read the
    # peer used a Tool, so ordinary Lead finalization still runs.
    assert len(lead_calls) == (1 if pause == "primary" else 2)
    if with_file:
        expected_text = {
            "pdf": "Original PDF profit is 42.",
            "xlsx": "FY2026\\t42",
        }.get(with_file, "period,profit\\nFY2026,42")
        assert all(expected_text in json.dumps(body) for body in primary_calls)
        if with_file == "xlsx":
            preview_note = file_parses[0].note
            assert preview_note and "30" in preview_note and "python_sandbox" in preview_note
            assert all(json.dumps(preview_note)[1:-1] in json.dumps(body) for body in primary_calls)
        if with_file in {"pdf", "xlsx"} and pause == "primary":
            assert len(file_parses) == 1
        original_image = base64.b64encode(b"original-prompt-image").decode()
        assert all(original_image in json.dumps(body) for body in primary_calls)
    with sqlite3.connect(case.path) as db:
        assert db.execute("SELECT primary_execution_id FROM turn_agent_binding WHERE turn_id=?", (turn_id,)).fetchone() == (execution,)
        records = db.execute(
            "SELECT resolution_seq,idempotency_key,execution_id,created_generation FROM chat_reference_resolutions WHERE turn_id=? ORDER BY resolution_seq",
            (turn_id,),
        ).fetchall()
        assert records == [
            (1, "reference:primary", execution, 1),
            (2, "reference:primary:delegation", execution, 2 if pause == "lead" else 1),
            (3, "reference:primary:mcp-prompt-results", execution, 2 if pause == "lead" else 1),
        ]
    evidence = await case.client.get(f"/api/v1/turns/{turn_id}/reference-evidence")
    assert evidence.status_code == 200
    facts = evidence.json()
    assert next(item for item in facts["references"] if item["resource_kind"] == "mcp_prompt")["outcome"] == "fulfilled"
    assert facts["orchestration"]["turn_status"] == "completed"
    assert facts["orchestration"]["execution_generation"] == (2 if pause else 1)
    assert {row["execution_id"] for row in facts["orchestration"]["agent_executions"]} == {execution}
    assert {row["execution_id"] for row in facts["model_invocations"]} == {execution}
    assert {row["generation"] for row in facts["model_invocations"]} == ({1, 2} if pause == "primary" else {2} if pause == "lead" else {1})
    assert invocation_facts and all(snapshot["selection_identity"] == facts["selection_identity"] for snapshot in invocation_facts)
    resumed_receipts = [row["invocation_id"] for row in facts["model_invocations"] if row["generation"] == 2]
    resumed_physical = [
        row["id"]
        for row in rows(case, "llm_usage_events")
        if row["turn_id"] == turn_id and row["receipt_json"] and json.loads(row["receipt_json"])["generation"] == 2
    ]
    assert facts["continuations"] == (
        [
            {
                **expected_continuation,
                "status": "resolved",
                "resume_generation": 2,
                "model_invocation_ids": resumed_receipts,
                "task_physical_receipt_ids": resumed_physical,
            }
        ]
        if pause
        else []
    )


@pytest.mark.parametrize("lead_queue", [False, True], ids=["legacy", "lead"])
async def test_queued_prompt_executes_accepted_contract_after_switch_change(lead_prompt, monkeypatch, lead_queue):
    f, lead_calls, primary_calls = lead_prompt, 0, 0
    case = f.case
    primary_started, release_primary = asyncio.Event(), asyncio.Event()

    async def respond(request):
        nonlocal lead_calls, primary_calls
        body = json.loads(request.content)
        names = {tool["function"]["name"] for tool in body.get("tools", [])}
        if "subagent_spawn" in names:
            lead_calls += 1
            if not lead_has_dispatched(body):
                return tool_response(
                    "subagent_spawn", {"agent_id": f.agent.id, "task": "Read margin.", "delegation_role": "primary"}, stream=body.get("stream", False)
                )
        elif "skill_read" in names:
            primary_calls += 1
            if primary_calls == 1:
                primary_started.set()
                await asyncio.wait_for(release_primary.wait(), 120)
        return answer_response(request)

    with respx.mock() as mock:
        mock.post(url__regex=r"https://api\..*").mock(side_effect=respond)
        parent = await case.client.post("/api/v1/chat/send", json=f.body)
        assert parent.status_code == 200, parent.text
        parent_id = parent.json()["turn_id"]
        started = asyncio.create_task(primary_started.wait())
        try:
            tasks = [task for task in chat._inflight_tasks if task.get_name().endswith(":" + parent_id)]
            assert tasks
            await asyncio.wait([started, *tasks], timeout=120, return_when=asyncio.FIRST_COMPLETED)
            assert primary_started.is_set(), await case.fixture.case.store.get_turn(parent_id)
            monkeypatch.setenv("FEATURE_CHAT_REFERENCE_LEAD_DELEGATION", "1" if lead_queue else "0")
            body = {
                **f.body,
                "client_submission_id": str(uuid4()),
                "session_id": parent.json()["session_id"],
                "submission_kind": "steer",
                "expected_parent_turn_id": parent_id,
                "expected_execution_generation": 1,
            }
            accepted = await case.client.post("/api/v1/chat/send", json=body)
            assert accepted.status_code == 200 and accepted.json()["status"] == "buffered", accepted.text
            monkeypatch.setenv("FEATURE_CHAT_REFERENCE_LEAD_DELEGATION", "0" if lead_queue else "1")
            monkeypatch.setenv("FEATURE_CHAT_REFERENCE_MCP", "0")
            replay = await case.client.post("/api/v1/chat/send", json=body)
            assert replay.status_code == 200 and replay.json() == accepted.json(), replay.text
        finally:
            release_primary.set()
            started.cancel()
            await asyncio.gather(started, return_exceptions=True)
        await settled(parent_id)
        relation = await case.fixture.case.store.get_chat_follow_up(parent_turn_id=parent_id)
        assert relation is not None
        child_id = relation[0]
        tasks = [task for task in chat._inflight_tasks if task.get_name().endswith(":" + child_id)]
        if tasks:
            await asyncio.wait_for(asyncio.gather(*tasks), 120)
        assert (await case.fixture.case.store.get_turn(child_id)).status == "completed"
        plan = next(row for row in rows(case, "chat_reference_plans") if row["turn_id"] == child_id)
        assert json.loads(plan["plan_json"]).get("orchestration_contract") == ("lead-delegation.v1" if lead_queue else None)
        # Each selected Team Agent's plain answer is delivered directly (5006e9348): one Lead call per Turn.
        assert lead_calls == (2 if lead_queue else 1) and primary_calls == 2
        assert f.calls == [("profitability", {"period": "FY2026"})] * 2
        calls = [
            row
            for row in rows(case, "chat_reference_resolutions")
            if row["turn_id"] == child_id and row["idempotency_key"] == "reference:primary:mcp-prompt-results"
        ]
        assert len(calls) == 1 and calls[0]["resolution_seq"] == (3 if lead_queue else 2)


async def test_prompt_lead_admission_requires_compatible_schema_before_writing(lead_prompt):
    f = lead_prompt
    tables = ("turns", "turn_agent_execution", "chat_reference_plans")
    before = {table: rows(f.case, table) for table in tables}
    with sqlite3.connect(f.case.path) as db:
        db.execute("DROP TRIGGER trg_reference_lead_prompt_plan")
    result = await f.case.client.post("/api/v1/chat/send", json=f.body)
    assert result.status_code == 503, result.text
    assert result.json()["detail"]["error"]["code"] == "turn_store_unavailable"
    assert {table: rows(f.case, table) for table in tables} == before
    assert f.calls == []


@pytest.mark.parametrize(
    "pause,outcome",
    [(pause, outcome) for pause in ("lead", "primary") for outcome in ("dismiss", "revoke", "source_revoked")] + [("primary", "material_purged")],
)
async def test_prompt_pause_rechecks_authority_and_supports_dismissal(lead_prompt, pause, outcome):
    f, model_calls = lead_prompt, []
    case = f.case

    def respond(request):
        body = json.loads(request.content)
        model_calls.append(body)
        lead = any(tool["function"]["name"] == "subagent_spawn" for tool in body.get("tools", []))
        if lead and pause == "primary":
            return tool_response(
                "subagent_spawn", {"agent_id": f.agent.id, "task": "Read margin.", "delegation_role": "primary"}, stream=body.get("stream", False)
            )
        question = {"questions": [{"question": "Which scope?", "type": "free_text", "options": []}]}
        return tool_response(
            "clarification_ask" if lead else "subagent_return",
            question if lead else {"payload": {"error": {"kind": "needs_user_input", "message": "Choose scope.", "clarification_payload": question}}},
            stream=body.get("stream", False),
        )

    with respx.mock() as mock:
        mock.post(url__regex=r"https://api\..*").mock(side_effect=respond)
        accepted = await case.client.post("/api/v1/chat/send", json=f.body)
        assert accepted.status_code == 200, accepted.text
        turn_id = accepted.json()["turn_id"]
        await settled(turn_id)
        assert (await case.fixture.case.store.get_turn(turn_id)).status == "clarification_pending"
        card = next(row for row in rows(case, "clarification_requests") if row["turn_id"] == turn_id)
        count = len(model_calls)
        if outcome == "dismiss":
            result = await case.client.post(f"/api/v1/clarifications/{card['request_id']}/dismiss")
            assert result.status_code == 200, result.text
            assert (await case.fixture.case.store.get_turn(turn_id)).status == "cancelled"
            assert {row["status"] for row in rows(case, "turn_agent_execution") if row["turn_id"] == turn_id} == {"cancelled"}
        else:
            with sqlite3.connect(case.path) as db:
                if outcome == "revoke":
                    db.execute("UPDATE agent_assignment SET personal_state='disabled'")
                elif outcome == "source_revoked":
                    db.execute("UPDATE team_tool_source SET status='disabled' WHERE id='ots_http_prompt'")
                else:
                    db.execute(
                        "UPDATE chat_reference_content_snapshots SET retention_state='purged',ciphertext=NULL WHERE json_extract(descriptor_json,'$.content_kind')='prompt_result'"
                    )
            result = await case.client.post(f"/api/v1/clarifications/{card['request_id']}/resolve", json={"chosen_values": ["All teams"]})
            assert result.status_code == 409, result.text
            assert (await case.fixture.case.store.get_turn(turn_id)).status == "clarification_pending"
        assert len(model_calls) == count and len(f.calls) == (0 if pause == "lead" else 1)
