"""Google Workspace read tools: attended-only overlay, per-member identity, explicit truncation."""

from __future__ import annotations

from types import SimpleNamespace
from urllib.parse import parse_qs

import httpx2
import pytest
from bowhead_core.builtin_tool_providers import active_by_provider
from bowhead_core.google_docs_markdown import document_to_markdown, presentation_to_markdown
from bowhead_core.subagent import EXPECTED_OUTCOME_ERROR_KINDS
from bowhead_core.types import ToolInvocation
from bowhead_gateway.routes import google_workspace as routes
from test_google_workspace_routes import _DOMAIN, _authorize, _configure, _tokens, env, google  # noqa: F401 - fixtures

from bowhead_core import builtin_google_tool as gtool

pytestmark = pytest.mark.anyio

_DOC_ID = "1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"


def _para(text: str, *, style: str = "NORMAL_TEXT", bullet: dict | None = None, bold: bool = False, link: str | None = None) -> dict:
    text_style: dict = {}
    if bold:
        text_style["bold"] = True
    if link:
        text_style["link"] = {"url": link}
    paragraph: dict = {"elements": [{"textRun": {"content": text + "\n", "textStyle": text_style}}], "paragraphStyle": {"namedStyleType": style}}
    if bullet is not None:
        paragraph["bullet"] = bullet
    return {"paragraph": paragraph}


def _cell(text: str) -> dict:
    return {"content": [_para(text)]}


def test_document_markdown_keeps_meaning_bearing_structure() -> None:
    document = {
        "title": "Q3",
        "lists": {"l1": {"listProperties": {"nestingLevels": [{"glyphType": "DECIMAL"}]}}, "l2": {"listProperties": {"nestingLevels": [{}]}}},
        "body": {
            "content": [
                {"sectionBreak": {}},
                _para("季度回顾", style="HEADING_1"),
                _para("收入增长", bold=True),
                _para("第一点", bullet={"listId": "l1"}),
                _para("第二点", bullet={"listId": "l1"}),
                _para("子项", bullet={"listId": "l2", "nestingLevel": 1}),
                {"table": {"tableRows": [{"tableCells": [_cell("地区"), _cell("收入")]}, {"tableCells": [_cell("华东"), _cell("1|2")]}]}},
                _para("详见报告", link="https://example.com/r"),
                {
                    "paragraph": {
                        "elements": [{"textRun": {"content": "脚注处"}}, {"footnoteReference": {"footnoteId": "f1"}}, {"textRun": {"content": "\n"}}],
                        "paragraphStyle": {},
                    }
                },
            ]
        },
        "footnotes": {"f1": {"content": [_para("来源：财务部")]}},
    }

    assert document_to_markdown(document) == (
        "# 季度回顾\n\n**收入增长**\n\n1. 第一点\n1. 第二点\n  - 子项\n\n"
        "| 地区 | 收入 |\n| --- | --- |\n| 华东 | 1\\|2 |\n\n[详见报告](https://example.com/r)\n\n脚注处[^1]\n\n[^1]: 来源：财务部"
    )


def test_presentation_markdown_lists_every_slide_and_notes() -> None:
    def shape(text: str) -> dict:
        return {"shape": {"text": {"textElements": [{"textRun": {"content": text}}]}}}

    presentation = {
        "slides": [
            {"pageElements": [shape("标题\n要点一\n")], "slideProperties": {"notesPage": {"pageElements": [shape("讲者备注")]}}},
            {"pageElements": []},
        ]
    }
    assert presentation_to_markdown(presentation) == "## Slide 1\n\n标题\n要点一\n\nSpeaker notes: 讲者备注\n\n## Slide 2\n\n(no text on this slide)"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (f"https://docs.google.com/document/d/{_DOC_ID}/edit#heading=h.1", ("document", _DOC_ID)),
        (f"https://docs.google.com/spreadsheets/u/0/d/{_DOC_ID}/edit", ("spreadsheet", _DOC_ID)),
        (f"https://docs.google.com/presentation/d/{_DOC_ID}", ("presentation", _DOC_ID)),
        (f"https://drive.google.com/file/d/{_DOC_ID}/view", (None, _DOC_ID)),
        (f"https://drive.google.com/open?id={_DOC_ID}", (None, _DOC_ID)),
        (_DOC_ID, (None, _DOC_ID)),
        (f"https://evil.example/document/d/{_DOC_ID}", None),
        (f"http://docs.google.com/document/d/{_DOC_ID}", None),
    ],
)
def test_file_reference_accepts_only_google_hosts(value: str, expected: tuple | None) -> None:
    assert gtool.parse_file_reference(value) == expected


def test_google_connection_outcomes_are_expected_not_failures() -> None:
    assert gtool.EXPECTED_ERROR_KINDS <= EXPECTED_OUTCOME_ERROR_KINDS


def _inv(name: str, result: dict) -> ToolInvocation:
    return ToolInvocation(tool_name=name, arguments={}, result={"provider": "google_workspace", **result})


def test_connection_notice_is_owed_only_when_no_later_google_call_succeeded() -> None:
    ref = "teammcp.ibt_1.gworkspace_read"
    failed = _inv(ref, {"ok": False, "error_kind": "google_reauth_required"})
    spawn = ToolInvocation(tool_name="subagent_spawn", arguments={}, result={}, nested=[failed])
    assert "重新连接 Google" in gtool.google_connection_notice([spawn], "zh-CN")
    assert "Reconnect Google" in gtool.google_connection_notice([spawn], "en-US")

    recovered = ToolInvocation(tool_name="subagent_spawn", arguments={}, result={}, nested=[failed, _inv(ref, {"ok": True})])
    assert gtool.google_connection_notice([recovered], "zh-CN") == ""
    assert gtool.google_connection_notice([_inv(ref, {"ok": False, "error_kind": "google_file_not_found"})], "zh-CN") == ""
    assert gtool.google_connection_notice([_inv("teammcp.ibt_2.datarc_query", {"ok": False, "error_kind": "google_not_connected"})]) == ""
    # A third-party MCP tool with the same name and error_kind is not a Google result.
    impostor = ToolInvocation(tool_name=ref, arguments={}, result={"ok": False, "error_kind": "google_reauth_required"})
    assert gtool.google_connection_notice([impostor], "zh-CN") == ""


class _Api:
    """Fake Google APIs on top of the token endpoint fake."""

    def __init__(self, token_fake) -> None:
        self.google = token_fake
        self.routes: dict[str, httpx2.Response] = {}
        self.seen: list[httpx2.Request] = []
        self._token = token_fake.handler

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        if request.url.host == "oauth2.googleapis.com":
            return self._token(request)
        self.seen.append(request)
        for prefix, response in self.routes.items():
            if str(request.url).startswith(prefix):
                return response
        return httpx2.Response(404, json={"error": {"errors": [{"reason": "notFound"}]}})


@pytest.fixture
def api(google, monkeypatch: pytest.MonkeyPatch):  # noqa: F811 - fixture reuse
    from bowhead_core import google_workspace

    fake = _Api(google)
    google_workspace.set_http_client_factory(lambda: httpx2.AsyncClient(transport=httpx2.MockTransport(fake.handler)))
    monkeypatch.setattr(gtool.anyio, "sleep", _no_sleep)
    return fake


async def _no_sleep(_seconds: float) -> None:
    return None


async def _enabled_connection(env, **bundles) -> object:  # noqa: F811
    from bowhead_core.google_workspace import scopes_for

    connection = await _configure(env, bundles=bundles)
    wanted = {b["id"] for b in connection["bundles"] if b["enabled"]}
    await _authorize(env, env.admin, token_body=_tokens(f"admin@{_DOMAIN}", scopes=scopes_for(wanted)))
    await routes.update_status(routes.StatusBody(enabled=True), env.request, env.admin)
    return await env.store.get_team_builtin_tool(env.team.id, "google_workspace")


def _ctx(env, user, *, attended: bool = True) -> SimpleNamespace:  # noqa: F811
    return SimpleNamespace(team_id=env.team.id, user_id=user.id, config_lite_supported=attended)


async def test_overlay_only_for_attended_runs_and_enabled_bundles(env, api) -> None:  # noqa: F811
    tool = await _enabled_connection(env)
    rows = await env.store.list_team_builtin_tools(env.team.id)

    attended = active_by_provider(rows, _ctx(env, env.admin))
    assert [(provider.name, [t.id for t in tools]) for provider, tools in attended] == [("google_workspace", [tool.id])]
    specs = attended[0][0].build_specs(team_id=env.team.id, tools=attended[0][1], store=env.store)
    assert [spec.schema.name for spec in specs] == [f"teammcp.{tool.id}.gworkspace_read"]  # calendar off by default
    assert all(spec.allowed_for_roles == frozenset({"analyst"}) and not spec.needs_approval for spec in specs)

    assert active_by_provider(rows, _ctx(env, env.admin, attended=False)) == []

    await _configure(env, bundles={"calendar": True})
    rows = await env.store.list_team_builtin_tools(env.team.id)
    provider, tools = active_by_provider(rows, _ctx(env, env.admin))[0]
    assert [s.schema.name.rsplit(".", 1)[1] for s in provider.build_specs(team_id=env.team.id, tools=tools, store=env.store)] == [
        "gworkspace_read",
        "gcalendar_events",
    ]


async def _read_spec(env, tool):  # noqa: F811
    return gtool.build_builtin_google_specs(team_id=env.team.id, tools=[tool], store=env.store)[0]


async def test_reads_a_document_as_the_calling_member_with_explicit_truncation(env, api, monkeypatch) -> None:  # noqa: F811
    tool = await _enabled_connection(env)
    monkeypatch.setattr(gtool, "_TEXT_LIMIT", 10)
    api.google.token_responses.append((200, {"access_token": "admin-access", "expires_in": 3600}))
    api.routes[f"https://docs.googleapis.com/v1/documents/{_DOC_ID}"] = httpx2.Response(
        200, json={"title": "Q3", "body": {"content": [_para("0123456789abcdefghij")]}}
    )
    spec = await _read_spec(env, tool)

    first = await spec.handler({"file": f"https://docs.google.com/document/d/{_DOC_ID}/edit"}, _ctx(env, env.admin))
    assert first["ok"] and first["content"] == "0123456789" and first["truncated"] is True and first["next_offset"] == 10
    assert api.seen[-1].headers["Authorization"] == "Bearer admin-access"
    # A bare ID resolves its type through Drive metadata first.
    api.routes[f"https://www.googleapis.com/drive/v3/files/{_DOC_ID}"] = httpx2.Response(
        200, json={"name": "Q3", "mimeType": "application/vnd.google-apps.document"}
    )
    rest = await spec.handler({"file": _DOC_ID, "offset": 10}, _ctx(env, env.admin))
    assert rest["content"] == "abcdefghij" and rest["truncated"] is False

    # The member has no grant: never the admin's token, and the way out is attached.
    member = await spec.handler({"file": _DOC_ID}, _ctx(env, env.member))
    assert member["error_kind"] == "google_not_connected" and "My connections" in member["guidance"]
    # Unattended run (e.g. scheduled job) never uses anyone's grant.
    unattended = await spec.handler({"file": _DOC_ID}, _ctx(env, env.admin, attended=False))
    assert unattended["error_kind"] == "google_connection_unavailable"


async def test_unpicked_file_guidance_depends_on_read_by_link(env, api) -> None:  # noqa: F811
    tool = await _enabled_connection(env, read_by_link=False)
    api.google.token_responses.append((200, {"access_token": "a", "expires_in": 3600}))
    spec = await _read_spec(env, tool)
    denied = await spec.handler({"file": f"https://docs.google.com/document/d/{_DOC_ID}"}, _ctx(env, env.admin))
    assert denied["error_kind"] == "google_file_not_granted" and "Select from Google Drive" in denied["guidance"]

    tool = await _enabled_connection(env, read_by_link=True)
    api.google.token_responses.append((200, {"access_token": "b", "expires_in": 3600}))
    spec = await _read_spec(env, tool)
    missing = await spec.handler({"file": f"https://docs.google.com/document/d/{_DOC_ID}"}, _ctx(env, env.admin))
    assert missing["error_kind"] == "google_file_not_found"


async def test_sheet_lists_tabs_first_then_reads_a_bounded_range(env, api, monkeypatch) -> None:  # noqa: F811
    tool = await _enabled_connection(env)
    monkeypatch.setattr(gtool, "_SHEET_ROW_LIMIT", 2)
    api.google.token_responses.append((200, {"access_token": "a", "expires_in": 3600}))
    base = f"https://sheets.googleapis.com/v4/spreadsheets/{_DOC_ID}"
    api.routes[f"{base}/values/"] = httpx2.Response(200, json={"range": "销售!A1:B3", "values": [["地区", "收入"], [False, 0], ["2026-10-15", 0.35]]})
    api.routes[base] = httpx2.Response(
        200,
        json={"properties": {"title": "销售"}, "sheets": [{"properties": {"title": "销售", "gridProperties": {"rowCount": 3, "columnCount": 2}}}]},
    )
    spec = await _read_spec(env, tool)
    link = f"https://docs.google.com/spreadsheets/d/{_DOC_ID}/edit"

    tabs = await spec.handler({"file": link}, _ctx(env, env.admin))
    assert tabs["sheets"] == [{"title": "销售", "rows": 3, "columns": 2}] and "range" in tabs["note"]
    values = await spec.handler({"file": link, "range": "销售!A1:B3"}, _ctx(env, env.admin))
    assert values["rows"] == [["地区", "收入"], [False, 0]] and values["total_rows"] == 3 and values["truncated"] is True
    assert api.seen[-1].url.params["valueRenderOption"] == "UNFORMATTED_VALUE"
    assert api.seen[-1].url.params["dateTimeRenderOption"] == "FORMATTED_STRING"
    assert values["value_representation"] == "unformatted_numbers_with_source_temporal_labels"
    assert "%E9%94%80%E5%94%AE!A1:B3" in str(api.seen[-1].url)


async def test_calendar_reads_own_events_within_bounds(env, api) -> None:  # noqa: F811
    tool = await _enabled_connection(env, calendar=True)
    api.google.token_responses.append((200, {"access_token": "a", "expires_in": 3600}))
    api.routes["https://www.googleapis.com/calendar/v3/calendars/primary/events"] = httpx2.Response(
        200,
        json={
            "items": [
                {
                    "summary": "周会",
                    "start": {"dateTime": "2026-09-01T10:00:00+08:00"},
                    "end": {"dateTime": "2026-09-01T11:00:00+08:00"},
                    "attendees": [{"email": "a@x"}],
                },
                {"summary": "取消", "status": "cancelled", "start": {}, "end": {}},
            ]
        },
    )
    calendar = gtool.build_builtin_google_specs(team_id=env.team.id, tools=[tool], store=env.store)[1]

    ok = await calendar.handler({"time_min": "2026-09-01T00:00:00+08:00", "time_max": "2026-09-08T00:00:00+08:00"}, _ctx(env, env.admin))
    assert ok["events"] == [
        {"title": "周会", "start": "2026-09-01T10:00:00+08:00", "end": "2026-09-01T11:00:00+08:00", "location": "", "attendees": ["a@x"]}
    ]
    assert parse_qs(api.seen[-1].url.query.decode())["singleEvents"] == ["true"]
    too_long = await calendar.handler({"time_min": "2026-01-01T00:00:00Z", "time_max": "2026-09-01T00:00:00Z"}, _ctx(env, env.admin))
    assert too_long["error_kind"] == "google_invalid_range"
