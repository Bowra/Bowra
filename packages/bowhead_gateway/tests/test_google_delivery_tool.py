"""Lead delivery to Google: web-only, number-checked, no confirmation dialog, dispatched once."""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx2
import pytest
from bowhead_core.google_docs_requests import find_section, insert_requests, utf16_len
from bowhead_core.types import ToolInvocation
from bowhead_gateway.routes import google_workspace as routes
from test_google_workspace_routes import _DOMAIN, _authorize, _configure, _tokens, env, google  # noqa: F401 - fixtures

from bowhead_core import google_delivery_tool as delivery

pytestmark = pytest.mark.anyio

_DOC_ID = "1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"


class _Api:
    def __init__(self, token_fake) -> None:
        self.token_fake = token_fake
        self.routes: list[tuple[str, str, httpx2.Response]] = []
        self.calls: list[httpx2.Request] = []

    def on(self, method: str, prefix: str, response: httpx2.Response) -> None:
        self.routes.append((method, prefix, response))

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        if request.url.host == "oauth2.googleapis.com":
            return self.token_fake.handler(request)
        self.calls.append(request)
        for method, prefix, response in self.routes:
            if request.method == method and str(request.url).startswith(prefix):
                return response
        return httpx2.Response(404, json={"error": {"status": "NOT_FOUND"}})

    def writes(self) -> list[httpx2.Request]:
        return [call for call in self.calls if call.method in {"POST", "PUT"}]


@pytest.fixture
async def ready(env, google, monkeypatch):  # noqa: F811
    from bowhead_core.google_workspace import scopes_for

    from bowhead_core import google_workspace

    api = _Api(google)
    google_workspace.set_http_client_factory(lambda: httpx2.AsyncClient(transport=httpx2.MockTransport(api.handler)))
    await _configure(env)
    await _authorize(env, env.admin, token_body=_tokens(f"admin@{_DOMAIN}", scopes=scopes_for({"base", "read_by_link", "edit_docs", "edit_sheets"})))
    await routes.update_status(routes.StatusBody(enabled=True), env.request, env.admin)
    google.token_responses.append((200, {"access_token": "access-admin", "expires_in": 3600}))
    delivery.set_store_provider(lambda: env.store)
    yield SimpleNamespace(env=env, api=api)
    delivery.set_store_provider(None)


def _ctx(env, *, attended: bool = True, user_text: str = "") -> SimpleNamespace:  # noqa: F811
    return SimpleNamespace(
        team_id=env.team.id, user_id=env.admin.id, config_lite_supported=attended, turn_id="t_1", session_id="s_1", user_text=user_text
    )


def _doc(*paragraphs: tuple[str, str, int, int]) -> dict:
    content = [{"sectionBreak": {}, "startIndex": 0, "endIndex": 1}]
    for style, text, start, end in paragraphs:
        content.append(
            {
                "startIndex": start,
                "endIndex": end,
                "paragraph": {"paragraphStyle": {"namedStyleType": style}, "elements": [{"textRun": {"content": text + "\n"}}]},
            }
        )
    return {"title": "季度报告", "revisionId": "rev-7", "body": {"content": content}}


# ---------------------------------------------------------------- pure helpers


def test_insert_requests_measure_utf16_and_style_after_insertion() -> None:
    requests, length = insert_requests("# 标题 😀\n\n- **要点**\n- 次\n  - 子项", 10, leading_newline=True)
    text = requests[0]["insertText"]["text"]
    assert text == "\n标题 😀\n要点\n次\n\t子项" and length == utf16_len(text)
    heading = next(r for r in requests if r.get("updateParagraphStyle", {}).get("paragraphStyle", {}).get("namedStyleType") == "HEADING_1")
    assert heading["updateParagraphStyle"]["range"] == {"startIndex": 11, "endIndex": 11 + utf16_len("标题 😀")}
    bold = next(r for r in requests if r.get("updateTextStyle", {}).get("fields") == "bold")
    start = 11 + utf16_len("标题 😀\n")
    assert bold["updateTextStyle"]["range"] == {"startIndex": start, "endIndex": start + 2}
    assert requests[-1]["createParagraphBullets"]["bulletPreset"] == "BULLET_DISC_CIRCLE_SQUARE"
    # Reset first so appended text never inherits a heading/bullet from the paragraph it lands in.
    # Bullets go first because removing them leaves the list indent behind as paragraph indent.
    assert [next(iter(r)) for r in requests[:4]] == ["insertText", "deleteParagraphBullets", "updateParagraphStyle", "updateTextStyle"]
    assert requests[2]["updateParagraphStyle"]["fields"] == "namedStyleType,indentStart,indentFirstLine"
    assert requests[3]["updateTextStyle"]["fields"] == "bold,italic,link,underline,foregroundColor"


def test_find_section_refuses_missing_or_duplicate_headings() -> None:
    document = _doc(
        ("HEADING_1", "概览", 1, 4),
        ("NORMAL_TEXT", "旧内容", 4, 8),
        ("HEADING_2", "细节", 8, 11),
        ("NORMAL_TEXT", "x", 11, 13),
        ("HEADING_1", "附录", 13, 16),
        ("NORMAL_TEXT", "y", 16, 18),
    )
    section, headings = find_section(document, "概览")
    assert section is not None and (section.content_start, section.content_end) == (4, 13)  # runs past the H2 up to the next H1
    assert headings == ["概览", "细节", "附录"]
    assert find_section(document, "不存在")[0] is None
    duplicated = _doc(("HEADING_1", "概览", 1, 4), ("HEADING_1", "概览", 4, 7))
    assert find_section(duplicated, "概览")[0] is None


# ---------------------------------------------------------------- gates


async def test_unattended_or_disabled_connection_never_writes(ready, monkeypatch) -> None:
    args = {"op": "create", "title": "报告", "markdown": "# 结论"}
    unattended = await delivery._doc(args, _ctx(ready.env, attended=False))
    assert unattended["error"] == "google_web_only"
    monkeypatch.setenv("FEATURE_GOOGLE_WORKSPACE", "0")
    disabled = await delivery._doc(args, _ctx(ready.env))
    assert disabled["error"] == "google_connection_unavailable"
    assert ready.api.writes() == []


async def test_advertising_follows_connection_and_presence(ready) -> None:
    from bowhead_core.turn_runner import _resolve_unconfigured_capability_tools

    orch = SimpleNamespace(user_store=ready.env.store)
    assert await _resolve_unconfigured_capability_tools(orch, SimpleNamespace(team_id=ready.env.team.id, config_lite_supported=True)) == frozenset()
    hidden = await _resolve_unconfigured_capability_tools(orch, SimpleNamespace(team_id=ready.env.team.id, config_lite_supported=False))
    assert hidden == frozenset(delivery.TOOL_NAMES)
    await routes.update_status(routes.StatusBody(enabled=False), ready.env.request, ready.env.admin)
    assert await _resolve_unconfigured_capability_tools(orch, SimpleNamespace(team_id=ready.env.team.id, config_lite_supported=True)) == frozenset(
        delivery.TOOL_NAMES
    )


async def test_numbers_without_a_source_are_refused_before_writing(ready) -> None:
    result = await delivery._doc({"op": "create", "title": "报告", "markdown": "收入 4,321 万"}, _ctx(ready.env))
    assert result["error"] == "google_unsourced_numbers" and result["unverified_numbers"]
    assert ready.api.writes() == []
    sourced = await delivery._doc({"op": "create", "title": "报告", "markdown": "收入 4,321 万"}, _ctx(ready.env, user_text="收入是 4321 万"))
    assert sourced["error"] != "google_unsourced_numbers"


# ---------------------------------------------------------------- Docs


async def test_create_doc_uploads_markdown_for_conversion_without_a_dialog(ready) -> None:
    ready.api.on(
        "POST",
        "https://www.googleapis.com/upload/drive/v3/files",
        httpx2.Response(200, json={"id": "new-doc", "name": "报告", "webViewLink": "https://docs.google.com/document/d/new-doc/edit"}),
    )
    result = await delivery._doc({"op": "create", "title": "报告", "markdown": "# 结论\n\n- 增长"}, _ctx(ready.env))

    assert result == {
        "ok": True,
        "op": "create",
        "file_id": "new-doc",
        "title": "报告",
        "url": "https://docs.google.com/document/d/new-doc/edit",
        "shown_in_canvas": False,  # this context has no canvas
    }
    upload = ready.api.writes()[0]
    assert upload.headers["Authorization"] == "Bearer access-admin"
    assert b'"mimeType": "application/vnd.google-apps.document"' in upload.content and b"text/markdown" in upload.content


async def test_replace_section_pins_revision_and_replaces_only_that_section(ready) -> None:
    document = _doc(("HEADING_1", "概览", 1, 4), ("NORMAL_TEXT", "旧内容", 4, 8), ("HEADING_1", "附录", 8, 11))
    ready.api.on("GET", f"https://docs.googleapis.com/v1/documents/{_DOC_ID}", httpx2.Response(200, json=document))
    ready.api.on("POST", f"https://docs.googleapis.com/v1/documents/{_DOC_ID}:batchUpdate", httpx2.Response(200, json={"replies": []}))

    missing = await delivery._doc({"op": "replace_section", "file": _DOC_ID, "heading": "结论", "markdown": "新"}, _ctx(ready.env))
    assert missing["error"] == "google_heading_not_found" and missing["headings"] == ["概览", "附录"]
    assert "澄清" in missing["message"] and ready.api.writes() == []  # asks the user, never guesses a section

    result = await delivery._doc(
        {"op": "replace_section", "file": f"https://docs.google.com/document/d/{_DOC_ID}/edit", "heading": "概览", "markdown": "新的概览"},
        _ctx(ready.env),
    )
    assert result["ok"] and result["title"] == "季度报告"
    body = json.loads(ready.api.writes()[-1].content)
    assert body["writeControl"] == {"requiredRevisionId": "rev-7"}
    assert body["requests"][0] == {"deleteContentRange": {"range": {"startIndex": 4, "endIndex": 8}}}
    assert body["requests"][1] == {"insertText": {"location": {"index": 4}, "text": "新的概览\n"}}


async def test_revision_conflict_and_unknown_outcome_are_never_retried(ready) -> None:
    ready.api.on("GET", f"https://docs.googleapis.com/v1/documents/{_DOC_ID}", httpx2.Response(200, json=_doc(("NORMAL_TEXT", "正文", 1, 4))))
    ready.api.routes.append(
        (
            "POST",
            f"https://docs.googleapis.com/v1/documents/{_DOC_ID}:batchUpdate",
            httpx2.Response(400, json={"error": {"status": "FAILED_PRECONDITION"}}),
        )
    )
    conflict = await delivery._doc({"op": "append", "file": _DOC_ID, "markdown": "补充"}, _ctx(ready.env))
    assert conflict["error"] == "google_revision_conflict"

    ready.api.routes[-1] = ("POST", f"https://docs.googleapis.com/v1/documents/{_DOC_ID}:batchUpdate", httpx2.Response(503))
    before = len(ready.api.writes())
    unknown = await delivery._doc({"op": "append", "file": _DOC_ID, "markdown": "补充"}, _ctx(ready.env))
    assert unknown["error"] == "google_outcome_unknown" and len(ready.api.writes()) == before + 1


# ---------------------------------------------------------------- Slides / Sheets


async def test_create_slides_builds_pages_and_removes_the_blank_first_slide(ready) -> None:
    ready.api.on("POST", "https://slides.googleapis.com/v1/presentations/p1:batchUpdate", httpx2.Response(200, json={}))
    ready.api.on(
        "POST",
        "https://slides.googleapis.com/v1/presentations",
        httpx2.Response(200, json={"presentationId": "p1", "slides": [{"objectId": "blank"}]}),
    )
    result = await delivery._slides({"op": "create", "title": "汇报", "slides": [{"title": "结论", "bullets": ["增长", "风险"]}]}, _ctx(ready.env))

    assert result["ok"] and result["url"] == "https://docs.google.com/presentation/d/p1" and result["slide_count"] == 1
    requests = json.loads(ready.api.writes()[-1].content)["requests"]
    assert requests[0]["createSlide"]["slideLayoutReference"] == {"predefinedLayout": "TITLE_AND_BODY"}
    assert {"deleteObject": {"objectId": "blank"}} in requests


async def test_sheet_write_refuses_non_empty_range_until_overwrite_is_explicit(ready) -> None:
    base = f"https://sheets.googleapis.com/v4/spreadsheets/{_DOC_ID}"
    ready.api.on("GET", f"{base}/values/", httpx2.Response(200, json={"values": [["旧"], ["", "x"]]}))
    ready.api.on("GET", base, httpx2.Response(200, json={"properties": {"title": "销售"}}))
    ready.api.on("PUT", f"{base}/values/", httpx2.Response(200, json={}))
    args = {
        "file": f"https://docs.google.com/spreadsheets/d/{_DOC_ID}",
        "sheet": "汇总",
        "start_cell": "B2",
        "rows": [["地区", "收入"], ["华东", "=1+1"]],
    }

    refused = await delivery._sheet(args, _ctx(ready.env))
    assert refused["error"] == "google_range_not_empty" and refused["non_empty_cells"] == 2 and refused["range"] == "'汇总'!B2:C3"
    assert "澄清" in refused["message"]  # the Lead asks the user before overwriting
    assert not [call for call in ready.api.writes() if call.method == "PUT"]

    written = await delivery._sheet({**args, "overwrite": True}, _ctx(ready.env))
    assert written["ok"] and written["overwrote_cells"] == 2
    put = ready.api.writes()[-1]
    assert put.url.params["valueInputOption"] == "RAW"  # never parse model text as formulas
    assert json.loads(put.content)["values"] == [["地区", "收入"], ["华东", "=1+1"]]


# ---------------------------------------------------------------- notice


def test_undelivered_notice_reports_the_last_call_per_tool() -> None:
    failed = ToolInvocation(tool_name="google_doc_write", arguments={}, result={"ok": False, "error": "google_outcome_unknown"})
    ok_sheet = ToolInvocation(tool_name="google_sheet_write", arguments={}, result={"ok": True})
    notice = delivery.undelivered_google_notice([failed, ok_sheet], "zh-CN")
    assert notice.startswith("**Google 未写入**") and "Google 文档" in notice and "不会自动重试" in notice and "表格" not in notice
    retried = ToolInvocation(tool_name="google_doc_write", arguments={}, result={"ok": True})
    assert delivery.undelivered_google_notice([failed, retried], "zh-CN") == ""
    assert "Not written to Google" in delivery.undelivered_google_notice([failed], "en-US")


# ---------------------------------------------------------------- canvas card


class _Bus:
    def __init__(self) -> None:
        self.events: list = []

    async def emit(self, event) -> None:
        self.events.append(event)


@pytest.fixture
async def canvas(tmp_path):
    from bowhead_core.canvas_store import CanvasStore

    store = CanvasStore(db_path=str(tmp_path / "canvas.db"))
    await store.initialize()
    return store


def _canvas_ctx(env, canvas, bus, *, turn_id: str = "t_1"):  # noqa: F811
    ctx = _ctx(env)
    ctx.turn_id = turn_id
    ctx.canvas_store = canvas
    ctx.parent_bus = bus
    ctx.workspace_id = "ws_1"
    return ctx


async def _cards(canvas, session_id: str = "s_1") -> list:
    target = await canvas.ensure_canvas(session_id=session_id, workspace_id="ws_1", user_id="u")
    return [s for s in await canvas.list_sections(target.id, include_hidden=True) if (s.payload or {}).get("provider") == "google_workspace"]


async def test_written_doc_appears_in_canvas_with_snapshot_and_lazy_live_preview(ready, canvas) -> None:
    bus = _Bus()
    ready.api.on("POST", "https://www.googleapis.com/upload/drive/v3/files", httpx2.Response(200, json={"id": "new-doc", "name": "报告"}))
    ready.api.on("GET", "https://docs.googleapis.com/v1/documents/new-doc", httpx2.Response(200, json=_doc(("HEADING_1", "结论", 1, 4))))

    result = await delivery._doc({"op": "create", "title": "报告", "markdown": "# 结论"}, _canvas_ctx(ready.env, canvas, bus))

    assert result["ok"] and result["shown_in_canvas"] is True
    [card] = await _cards(canvas)
    assert card.kind == "external_iframe"
    payload = card.payload
    assert payload["file_kind"] == "document" and payload["open_url"] == "https://docs.google.com/document/d/new-doc"
    assert payload["url"] == f"https://docs.google.com/document/d/new-doc/preview?authuser=admin%40{_DOMAIN}"
    assert payload["snapshot"] == {"format": "markdown", "content": "# 结论", "truncated": False}
    assert [event.event_type for event in bus.events] == ["canvas.section_added"]
    assert bus.events[0].payload["section"]["snapshot"]["content"] == "# 结论"  # live payload == persisted payload


async def test_second_write_to_the_same_file_in_a_turn_updates_the_card(ready, canvas) -> None:
    bus = _Bus()
    document = _doc(("NORMAL_TEXT", "正文", 1, 4))
    ready.api.on("GET", f"https://docs.googleapis.com/v1/documents/{_DOC_ID}", httpx2.Response(200, json=document))
    ready.api.on("POST", f"https://docs.googleapis.com/v1/documents/{_DOC_ID}:batchUpdate", httpx2.Response(200, json={"replies": []}))

    await delivery._doc({"op": "append", "file": _DOC_ID, "markdown": "补充"}, _canvas_ctx(ready.env, canvas, bus))
    await delivery._doc({"op": "append", "file": _DOC_ID, "markdown": "再补充"}, _canvas_ctx(ready.env, canvas, bus))
    assert len(await _cards(canvas)) == 1
    assert [event.event_type for event in bus.events] == ["canvas.section_added", "canvas.section_patched"]

    await delivery._doc({"op": "append", "file": _DOC_ID, "markdown": "下一轮"}, _canvas_ctx(ready.env, canvas, bus, turn_id="t_2"))
    assert len(await _cards(canvas)) == 2  # a later turn gets its own card


async def test_sheet_card_snapshots_the_rows_written_and_survives_failed_read_back(ready, canvas) -> None:
    bus = _Bus()
    ready.api.on("POST", "https://sheets.googleapis.com/v4/spreadsheets", httpx2.Response(200, json={"spreadsheetId": "sheet-1"}))
    ready.api.on("PUT", "https://sheets.googleapis.com/v4/spreadsheets/sheet-1/values/", httpx2.Response(200, json={}))
    await delivery._sheet({"title": "汇总", "rows": [["地区", "收入"], ["华东", 1]]}, _canvas_ctx(ready.env, canvas, bus))
    [sheet_card] = await _cards(canvas)
    assert sheet_card.payload["snapshot"] == {
        "format": "table",
        "rows": [["地区", "收入"], ["华东", "1"]],
        "range": "'Sheet1'!A1:B2",
        "truncated": False,
    }
    assert sheet_card.payload["url"].startswith("https://docs.google.com/spreadsheets/d/sheet-1/htmlview?authuser=")

    # Slides read-back fails (404 from the fake API): the card still carries the link and live preview.
    ready.api.on("POST", "https://slides.googleapis.com/v1/presentations/p1:batchUpdate", httpx2.Response(200, json={}))
    ready.api.on("POST", "https://slides.googleapis.com/v1/presentations", httpx2.Response(200, json={"presentationId": "p1", "slides": []}))
    result = await delivery._slides({"op": "create", "title": "汇报", "slides": [{"title": "结论"}]}, _canvas_ctx(ready.env, canvas, bus))
    assert result["ok"] and result["shown_in_canvas"] is True
    slides_card = next(card for card in await _cards(canvas) if card.payload["file_kind"] == "presentation")
    assert slides_card.payload["snapshot"] is None and slides_card.payload["open_url"] == "https://docs.google.com/presentation/d/p1"
