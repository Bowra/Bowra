"""Regression tests for the 2026-10-01 multi-angle review of the Google Workspace integration."""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx2
import pytest
from bowhead_core.builtin_tool_grants import BuiltinToolGrantStore
from bowhead_core.google_docs_markdown import document_to_markdown
from bowhead_core.google_docs_requests import insert_requests
from bowhead_core.google_workspace import scopes_for, token_broker
from bowhead_core.subagent import _is_non_failure_result
from bowhead_core.types import ToolInvocation
from test_google_delivery_tool import _DOC_ID, _ctx, _doc, ready  # noqa: F401 - fixtures
from test_google_workspace_routes import _DOMAIN, _authorize, _configure, _tokens, env, google  # noqa: F401 - fixtures

from bowhead_core import builtin_google_tool as gtool
from bowhead_core import google_delivery_tool as delivery

pytestmark = pytest.mark.anyio

_DOCS = f"https://docs.googleapis.com/v1/documents/{_DOC_ID}"
_DRIVE = f"https://www.googleapis.com/drive/v3/files/{_DOC_ID}"


# ---------------------------------------------------------------- capability bundles are enforced


async def test_disabled_edit_bundle_blocks_existing_files_unless_picked(ready) -> None:  # noqa: F811
    await _configure(ready.env, bundles={"edit_docs": False})
    ready.api.on("GET", _DOCS, httpx2.Response(200, json=_doc(("NORMAL_TEXT", "正文", 1, 4))))
    ready.api.on("POST", f"{_DOCS}:batchUpdate", httpx2.Response(200, json={"replies": []}))

    refused = await delivery._doc({"op": "append", "file": _DOC_ID, "markdown": "补充"}, _ctx(ready.env))
    assert refused["error"] == "google_capability_disabled"
    assert ready.api.writes() == []

    # A file the member picked (Drive answers for it with drive.file) stays editable.
    ready.api.on("GET", _DRIVE, httpx2.Response(200, json={"id": _DOC_ID}))
    allowed = await delivery._doc({"op": "append", "file": _DOC_ID, "markdown": "补充"}, _ctx(ready.env))
    assert allowed["ok"] is True


async def test_disabled_read_by_link_blocks_unpicked_files(ready) -> None:  # noqa: F811
    tool = await _enabled_tool(ready, read_by_link=False)
    ready.api.on("GET", _DOCS, httpx2.Response(200, json=_doc(("NORMAL_TEXT", "机密", 1, 4))))
    spec = gtool.build_builtin_google_specs(team_id=ready.env.team.id, tools=[tool], store=ready.env.store)[0]

    denied = await spec.handler({"file": f"https://docs.google.com/document/d/{_DOC_ID}"}, _ctx(ready.env))
    assert denied["error_kind"] == "google_file_not_granted" and "content" not in denied
    ready.api.on("GET", _DRIVE, httpx2.Response(200, json={"id": _DOC_ID}))
    allowed = await spec.handler({"file": f"https://docs.google.com/document/d/{_DOC_ID}"}, _ctx(ready.env))
    assert allowed["ok"] and allowed["provider"] == "google_workspace"


async def _enabled_tool(ready, **bundles):  # noqa: F811
    await _configure(ready.env, bundles=bundles)
    return await ready.env.store.get_team_builtin_tool(ready.env.team.id, "google_workspace")


async def test_bare_id_is_typed_through_editor_apis_when_drive_cannot_see_it(ready) -> None:  # noqa: F811
    tool = await _enabled_tool(ready, read_by_link=True)
    ready.api.on("GET", _DOCS, httpx2.Response(200, json=_doc(("NORMAL_TEXT", "按链接", 1, 4))))
    spec = gtool.build_builtin_google_specs(team_id=ready.env.team.id, tools=[tool], store=ready.env.store)[0]
    result = await spec.handler({"file": _DOC_ID}, _ctx(ready.env))  # Drive metadata answers 404
    assert result["ok"] and result["type"] == "document" and "按链接" in result["content"]


# ---------------------------------------------------------------- Docs edits


async def test_replace_section_under_the_last_heading_inserts_inside_the_document(ready) -> None:  # noqa: F811
    document = _doc(("NORMAL_TEXT", "正文", 1, 4), ("HEADING_1", "结论", 4, 7))  # heading is the last paragraph; body ends at 7
    ready.api.on("GET", _DOCS, httpx2.Response(200, json=document))
    ready.api.on("POST", f"{_DOCS}:batchUpdate", httpx2.Response(200, json={"replies": []}))

    result = await delivery._doc({"op": "replace_section", "file": _DOC_ID, "heading": "结论", "markdown": "收入增长"}, _ctx(ready.env))
    assert result["ok"]
    requests = json.loads(ready.api.writes()[-1].content)["requests"]
    assert not any("deleteContentRange" in r for r in requests)
    assert requests[0] == {"insertText": {"location": {"index": 6}, "text": "\n收入增长"}}  # before the heading's newline


def test_four_space_lists_nest_one_level_per_indent() -> None:
    requests, _ = insert_requests("- one\n    - two\n        - three", 1, leading_newline=False)
    assert requests[0]["insertText"]["text"] == "one\n\ttwo\n\t\tthree\n"


async def test_write_errors_distinguish_view_only_scope_and_revision(ready) -> None:  # noqa: F811
    ready.api.on("GET", _DOCS, httpx2.Response(200, json=_doc(("NORMAL_TEXT", "正文", 1, 4))))
    cases = [
        (httpx2.Response(403, json={"error": {"status": "PERMISSION_DENIED"}}), "google_no_edit_access"),
        (
            httpx2.Response(403, json={"error": {"status": "PERMISSION_DENIED", "details": [{"reason": "ACCESS_TOKEN_SCOPE_INSUFFICIENT"}]}}),
            "google_scope_missing",
        ),
        (
            httpx2.Response(400, json={"error": {"status": "INVALID_ARGUMENT", "message": "The required revision ID does not match"}}),
            "google_revision_conflict",
        ),
    ]
    for response, expected in cases:
        ready.api.routes = [r for r in ready.api.routes if not r[1].endswith(":batchUpdate")]
        ready.api.on("POST", f"{_DOCS}:batchUpdate", response)
        result = await delivery._doc({"op": "append", "file": _DOC_ID, "markdown": "补充"}, _ctx(ready.env))
        assert result["error"] == expected, (expected, result)


async def test_partially_created_presentation_reports_where_it_is(ready) -> None:  # noqa: F811
    ready.api.on("POST", "https://slides.googleapis.com/v1/presentations/p9:batchUpdate", httpx2.Response(503))
    ready.api.on("POST", "https://slides.googleapis.com/v1/presentations", httpx2.Response(200, json={"presentationId": "p9", "slides": []}))
    result = await delivery._slides({"op": "create", "title": "汇报", "slides": [{"title": "结论"}]}, _ctx(ready.env))
    assert result["error"] == "google_outcome_unknown"
    assert result["created_file_id"] == "p9" and result["created_url"] == "https://docs.google.com/presentation/d/p9"


async def test_existing_sheet_defaults_to_its_first_tab(ready) -> None:  # noqa: F811
    base = f"https://sheets.googleapis.com/v4/spreadsheets/{_DOC_ID}"
    ready.api.on("GET", f"{base}/values/", httpx2.Response(200, json={}))
    ready.api.on("GET", base, httpx2.Response(200, json={"properties": {"title": "销售"}, "sheets": [{"properties": {"title": "工作表1"}}]}))
    ready.api.on("PUT", f"{base}/values/", httpx2.Response(200, json={}))
    result = await delivery._sheet({"file": _DOC_ID, "rows": [["a"]]}, _ctx(ready.env))
    assert result["ok"] and result["range"] == "'工作表1'!A1:A1"


# ---------------------------------------------------------------- notices and expected outcomes


def test_undelivered_notice_tracks_each_file_and_defers_to_the_connection_notice() -> None:
    def call(file: str, result: dict) -> ToolInvocation:
        return ToolInvocation(tool_name=delivery.DOC_TOOL, arguments={"op": "append", "file": file}, result=result)

    failed_a = call("https://docs.google.com/document/d/AAAAAAAAAAAAAAAAAAAA", {"ok": False, "error": "google_revision_conflict"})
    ok_b = call("https://docs.google.com/document/d/BBBBBBBBBBBBBBBBBBBB", {"ok": True})
    assert "Google 文档" in delivery.undelivered_google_notice([failed_a, ok_b], "zh-CN")

    reauth = call("https://docs.google.com/document/d/AAAAAAAAAAAAAAAAAAAA", {"ok": False, "error": "google_reauth_required"})
    assert "我的连接" in delivery.undelivered_google_notice([reauth], "zh-CN")
    assert "我的连接" not in delivery.undelivered_google_notice([reauth], "zh-CN", connection_noticed=True)


def test_google_expected_outcomes_need_the_google_provider_marker() -> None:
    assert _is_non_failure_result({"ok": False, "error_kind": "google_not_connected", "provider": "google_workspace"}) is True
    assert _is_non_failure_result({"ok": False, "error_kind": "google_not_connected"}) is False


# ---------------------------------------------------------------- grants, broker and callbacks


async def test_stale_refresh_never_marks_a_renewed_grant(env) -> None:  # noqa: F811
    connection = await _configure(env)
    await _authorize(env, env.admin, token_body=_tokens(f"admin@{_DOMAIN}", refresh="refresh-old"))
    grants = BuiltinToolGrantStore(env.db_path)
    grant = await grants.get(connection["id"], env.admin.id)
    _token, old_cipher = await grants.refresh_token_versioned(grant)

    await _authorize(env, env.admin, token_body=_tokens(f"admin@{_DOMAIN}", refresh="refresh-new"))
    await grants.mark_needs_reauth(connection["id"], env.admin.id, expected_cipher=old_cipher)
    assert (await grants.get(connection["id"], env.admin.id)).status == "active"


async def test_refresh_started_before_forget_is_not_cached(env) -> None:  # noqa: F811
    await _configure(env)
    await _authorize(env, env.admin, token_body=_tokens(f"admin@{_DOMAIN}"))
    tool = await env.store.get_team_builtin_tool(env.team.id, "google_workspace")
    auth = await env.store.get_team_builtin_tool_instance_auth_decrypted(env.team.id, tool.id)
    broker = token_broker(env.db_path)
    grant = await broker.grants.get(tool.id, env.admin.id)

    original = broker.grants.refresh_token_versioned

    async def reconnect_meanwhile(g):
        value = await original(g)
        broker.forget(g.id)  # the member reconnects while this refresh is in flight
        return value

    broker.grants.refresh_token_versioned = reconnect_meanwhile  # type: ignore[method-assign]
    env.google.token_responses.append((200, {"access_token": "stale", "expires_in": 3600}))
    await broker.access_token(tool=tool, auth=auth, user_id=env.admin.id, required_bundles=frozenset({"base"}))
    assert grant.id not in broker._cache


async def test_reconnecting_the_same_account_keeps_the_old_token_unrevoked(env) -> None:  # noqa: F811
    await _configure(env)
    await _authorize(env, env.admin, token_body=_tokens(f"admin@{_DOMAIN}", refresh="refresh-1"))
    await _authorize(env, env.admin, token_body=_tokens(f"admin@{_DOMAIN}", refresh="refresh-2"))
    assert "refresh-1" not in env.google.revoked


async def test_changing_the_domain_revokes_every_grant(env) -> None:  # noqa: F811
    connection = await _configure(env)
    await _authorize(env, env.admin, token_body=_tokens(f"admin@{_DOMAIN}", refresh="refresh-d"))
    updated = await _configure(env, domain="other.example")
    assert updated["authorized_members"] == 0 and "refresh-d" in env.google.revoked
    assert await BuiltinToolGrantStore(env.db_path).get(connection["id"], env.admin.id) is None


async def test_read_401_drops_the_cached_token_without_marking_reauth(ready) -> None:  # noqa: F811
    tool = await _enabled_tool(ready, read_by_link=True)
    ready.api.on("GET", _DOCS, httpx2.Response(401, json={"error": {"status": "UNAUTHENTICATED"}}))
    spec = gtool.build_builtin_google_specs(team_id=ready.env.team.id, tools=[tool], store=ready.env.store)[0]
    result = await spec.handler({"file": f"https://docs.google.com/document/d/{_DOC_ID}"}, _ctx(ready.env))
    assert result["error_kind"] == "google_reauth_required"
    grant = await BuiltinToolGrantStore(ready.env.db_path).get(tool.id, ready.env.admin.id)
    assert grant.status == "active" and grant.id not in token_broker(ready.env.db_path)._cache


async def test_resumed_scheduled_clarification_stays_unattended(monkeypatch) -> None:
    from bowhead_gateway.routes import clarifications

    from bowhead_gateway import terminal_pending_execution as recovery

    async def scheduled(_app, _turn_id):
        return SimpleNamespace(job_id="sj_1")

    monkeypatch.setattr(clarifications, "scheduled_run_for_turn", scheduled)
    assert await recovery._resume_is_attended(SimpleNamespace(), "t_1") is False

    async def interactive(_app, _turn_id):
        return None

    monkeypatch.setattr(clarifications, "scheduled_run_for_turn", interactive)
    assert await recovery._resume_is_attended(SimpleNamespace(), "t_1") is True


# ---------------------------------------------------------------- canvas card on edits


async def test_edit_card_snapshot_shows_only_what_was_written(ready, tmp_path) -> None:  # noqa: F811
    from bowhead_core.canvas_store import CanvasStore

    canvas = CanvasStore(db_path=str(tmp_path / "canvas.db"))
    await canvas.initialize()
    ready.api.on("GET", _DOCS, httpx2.Response(200, json=_doc(("NORMAL_TEXT", "别人的机密正文", 1, 9))))
    ready.api.on("POST", f"{_DOCS}:batchUpdate", httpx2.Response(200, json={"replies": []}))
    ctx = _ctx(ready.env)
    ctx.canvas_store, ctx.parent_bus, ctx.workspace_id = canvas, None, "ws_1"

    await delivery._doc({"op": "append", "file": _DOC_ID, "markdown": "本次补充"}, ctx)
    target = await canvas.ensure_canvas(session_id="s_1", workspace_id="ws_1", user_id="u")
    [card] = [s for s in await canvas.list_sections(target.id, include_hidden=True) if (s.payload or {}).get("provider") == "google_workspace"]
    assert card.payload["snapshot"]["content"] == "本次补充" and card.payload["snapshot"]["part"] == "written"
    assert "机密" not in json.dumps(card.payload, ensure_ascii=False)


def _tabbed_document() -> dict:
    def tab(tab_id: str, title: str, text: str) -> dict:
        body = _doc(("HEADING_1", "Summary", 1, 9), ("NORMAL_TEXT", text, 9, 30))
        return {"tabProperties": {"tabId": tab_id, "title": title}, "documentTab": {"body": body["body"]}}

    first = tab("t.first", "Overview", "First tab content")
    second = tab("t.second", "Results", "Second tab content")
    second["childTabs"] = [tab("t.child", "Appendix", "Nested tab content")]
    return {"title": "Multi-tab report", "revisionId": "revision-tabs", "tabs": [first, second]}


async def test_document_read_includes_nested_tabs_and_respects_the_link_target(ready) -> None:  # noqa: F811
    tool = await _enabled_tool(ready, read_by_link=True)
    ready.api.on("GET", _DOCS, httpx2.Response(200, json=_tabbed_document()))
    spec = gtool.build_builtin_google_specs(team_id=ready.env.team.id, tools=[tool], store=ready.env.store)[0]
    link = f"https://docs.google.com/document/d/{_DOC_ID}/edit"

    complete = await spec.handler({"file": link}, _ctx(ready.env))
    assert complete["ok"] and not complete["truncated"]
    assert all(text in complete["content"] for text in ("First tab content", "Second tab content", "Nested tab content"))
    assert [tab["id"] for tab in complete["tabs"]] == ["t.first", "t.second", "t.child"]
    assert ready.api.calls[-1].url.params["includeTabsContent"] == "true"

    selected = await spec.handler({"file": f"{link}?tab=t.child"}, _ctx(ready.env))
    assert selected["ok"] and "Nested tab content" in selected["content"]
    assert selected["url"].endswith("?tab=t.child")
    assert "First tab content" not in selected["content"] and "Second tab content" not in selected["content"]

    missing = await spec.handler({"file": f"{link}?tab=t.missing"}, _ctx(ready.env))
    assert missing["error_kind"] == "google_invalid_reference" and "content" not in missing


def test_document_tabs_keep_their_footnote_references_distinct() -> None:
    document = _tabbed_document()
    for tab in document["tabs"]:
        content = tab["documentTab"]
        content["body"]["content"][1]["paragraph"]["elements"].append({"footnoteReference": {"footnoteId": "source"}})
        content["footnotes"] = {
            "source": {"content": [{"paragraph": {"elements": [{"textRun": {"content": f"{tab['tabProperties']['title']} source"}}]}}]}
        }
    markdown = document_to_markdown(document)
    assert markdown.count("[^tab1-1]") == 2 and "[^tab1-1]: Overview source" in markdown
    assert markdown.count("[^tab2-1]") == 2 and "[^tab2-1]: Results source" in markdown
    assert "[^1]" not in markdown


@pytest.mark.parametrize("op", ["append", "replace_section", "replace_text"])
@pytest.mark.parametrize("single_tab", [False, True])
async def test_document_edits_are_scoped_to_the_selected_tab(ready, op: str, single_tab: bool) -> None:  # noqa: F811
    document = _tabbed_document()
    if single_tab:
        document["tabs"] = document["tabs"][:1]
    selected_tab = "t.first" if single_tab else "t.child"
    ready.api.on("GET", _DOCS, httpx2.Response(200, json=document))
    ready.api.on("POST", f"{_DOCS}:batchUpdate", httpx2.Response(200, json={"replies": []}))
    args = {
        "op": op,
        "file": f"https://docs.google.com/document/d/{_DOC_ID}/edit" + ("" if single_tab else "?tab=t.child"),
        "markdown": "Replacement",
        "heading": "Summary",
        "find": "old",
        "replace": "new",
    }

    result = await delivery._doc(args, _ctx(ready.env))
    assert result["ok"] and result["url"].endswith(f"?tab={selected_tab}")
    body = json.loads(ready.api.writes()[-1].content)
    assert body["writeControl"] == {"requiredRevisionId": "revision-tabs"}
    for request in body["requests"]:
        operation = next(iter(request.values()))
        if "replaceAllText" in request:
            assert operation["tabsCriteria"] == {"tabIds": [selected_tab]}
        else:
            assert (operation.get("range") or operation.get("location"))["tabId"] == selected_tab


@pytest.mark.parametrize("tab", [None, "t.missing"])
async def test_document_edits_refuse_ambiguous_or_missing_tabs_before_dispatch(ready, tab: str | None) -> None:  # noqa: F811
    ready.api.on("GET", _DOCS, httpx2.Response(200, json=_tabbed_document()))
    link = f"https://docs.google.com/document/d/{_DOC_ID}/edit" + (f"?tab={tab}" if tab is not None else "")
    result = await delivery._doc({"op": "replace_text", "file": link, "find": "old", "replace": "new"}, _ctx(ready.env))
    assert result["error"] == ("google_tab_required" if tab is None else "google_invalid_reference")
    assert [tab["id"] for tab in result["tabs"]] == ["t.first", "t.second", "t.child"]
    assert ready.api.writes() == []


@pytest.mark.parametrize("existing", ['=""', " ", 0, False])
async def test_sheet_preserves_formulas_and_other_nonempty_input_until_overwrite_is_allowed(ready, existing: object) -> None:  # noqa: F811
    base = f"https://sheets.googleapis.com/v4/spreadsheets/{_DOC_ID}"
    ready.api.on("GET", base, httpx2.Response(200, json={"properties": {"title": "Report"}}))

    original = ready.api.handler

    def formula_response(request: httpx2.Request) -> httpx2.Response:
        if request.url.path.startswith(f"/v4/spreadsheets/{_DOC_ID}/values/"):
            ready.api.calls.append(request)
            # The formula's displayed result is empty; its actual input must remain protected.
            return httpx2.Response(200, json={"values": [[existing if request.url.params.get("valueRenderOption") == "FORMULA" else ""]]})
        return original(request)

    from bowhead_core import google_workspace

    google_workspace.set_http_client_factory(lambda: httpx2.AsyncClient(transport=httpx2.MockTransport(formula_response)))
    result = await delivery._sheet({"file": _DOC_ID, "rows": [["Replacement"]]}, _ctx(ready.env))
    assert result["error"] == "google_range_not_empty" and result["non_empty_cells"] == 1
    assert ready.api.writes() == []


async def test_sheet_blanks_and_ragged_rows_match_the_written_canvas_snapshot(ready, tmp_path) -> None:  # noqa: F811
    from bowhead_core.canvas_store import CanvasStore

    canvas = CanvasStore(db_path=str(tmp_path / "canvas.db"))
    await canvas.initialize()
    base = f"https://sheets.googleapis.com/v4/spreadsheets/{_DOC_ID}"
    ready.api.on("GET", f"{base}/values/", httpx2.Response(200, json={"values": [["Old", "Old"], ["Old", "Old"]]}))
    ready.api.on("GET", base, httpx2.Response(200, json={"properties": {"title": "Report"}}))
    ready.api.on("PUT", f"{base}/values/", httpx2.Response(200, json={"updatedRows": 2, "updatedCells": 4}))
    ctx = _ctx(ready.env)
    events = []

    async def emit(event) -> None:
        events.append(event)

    ctx.canvas_store, ctx.parent_bus, ctx.workspace_id = canvas, SimpleNamespace(emit=emit), "ws_1"

    result = await delivery._sheet({"file": _DOC_ID, "rows": [[None, "New"], []], "overwrite": True}, ctx)
    assert result["ok"] and result["overwrote_cells"] == 4
    values = json.loads(ready.api.writes()[-1].content)["values"]
    assert values == [["", "New"], ["", ""]]
    target = await canvas.ensure_canvas(session_id="s_1", workspace_id="ws_1", user_id="u")
    [card] = [section for section in await canvas.list_sections(target.id) if (section.payload or {}).get("provider") == "google_workspace"]
    assert card.payload["snapshot"]["rows"] == values
    assert events[0].event_type == "canvas.section_added"
    assert events[0].payload["section"]["snapshot"]["rows"] == values


async def test_calendar_401_refreshes_access_on_the_next_call_without_reauthorizing(ready) -> None:  # noqa: F811
    tool = await _enabled_tool(ready, calendar=True)
    grants = BuiltinToolGrantStore(ready.env.db_path)
    grant = await grants.get(tool.id, ready.env.admin.id)
    await grants.save(
        tool_id=tool.id,
        team_id=tool.team_id,
        user_id=grant.user_id,
        provider_subject=grant.provider_subject,
        provider_email=grant.provider_email,
        granted_scopes=scopes_for({"base", "calendar"}),
        refresh_token=None,
    )
    url = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
    ready.api.on("GET", url, httpx2.Response(401))
    specs = gtool.build_builtin_google_specs(team_id=ready.env.team.id, tools=[tool], store=ready.env.store)
    calendar = next(spec for spec in specs if spec.schema.name.endswith("gcalendar_events"))
    args = {"time_min": "2026-10-01T00:00:00Z", "time_max": "2026-10-02T00:00:00Z"}

    failed = await calendar.handler(args, _ctx(ready.env))
    assert failed["error_kind"] == "google_reauth_required"
    assert grant.id not in token_broker(ready.env.db_path)._cache
    assert (await grants.get(tool.id, grant.user_id)).status == "active"

    ready.api.routes = [route for route in ready.api.routes if route[1] != url]
    ready.api.on("GET", url, httpx2.Response(200, json={"items": []}))
    ready.env.google.token_responses.append((200, {"access_token": "fresh-access", "expires_in": 3600}))
    retried = await calendar.handler(args, _ctx(ready.env))
    assert retried["ok"] and ready.api.calls[-1].headers["Authorization"] == "Bearer fresh-access"
