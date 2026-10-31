from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest
from bowhead_cli.migrate import upgrade
from bowhead_core.migrations.baseline_schema import initial_schema
from bowhead_core.subagent_authoring_catalog import (
    SubagentAuthoringCatalogService,
)
from bowhead_core.subagent_draft_service import (
    REQUIRED_CAPABILITY_UNMET,
    TEXT_ONLY_CONFIRMATION,
    CanvasGenerationResult,
    DraftServiceOutcome,
    DraftServiceResult,
    definition_fingerprint,
)
from bowhead_core.subagent_draft_store import (
    SubagentDraftCreate,
    SubagentDraftStore,
    SubagentDraftVersionConflictError,
)
from bowhead_core.team_subagent_store import TeamSubagentStore
from bowhead_core.user_store import User, UserStore, _now
from bowhead_gateway.conditional_headers import ConditionalHeaderCanonicalizationMiddleware
from bowhead_gateway.routes import team_subagent_drafts as drafts
from bowhead_gateway.routes import team_subagents
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request

from bowhead_gateway import subagent_draft_chat_editor as chat_editor

_BASE = "/api/v1/team-subagents/drafts"


def _run(coro):
    return asyncio.run(coro)


class _ReplayService:
    def __init__(self, draft) -> None:
        self.draft = draft

    async def create_intent_draft(self, **_kwargs) -> DraftServiceResult:
        return DraftServiceResult(
            DraftServiceOutcome.DRAFT_READY,
            draft=self.draft,
        )


def _setup(tmp_path, monkeypatch):
    db_path = tmp_path / "bowhead.db"
    with sqlite3.connect(db_path) as connection:
        connection.executescript(initial_schema("core"))
        connection.commit()
    upgrade(f"sqlite+aiosqlite:///{db_path}", target="core")
    user_store = UserStore(str(db_path))
    _run(user_store.initialize())
    draft_store = SubagentDraftStore(
        str(db_path),
        hmac_key=b"d" * 32,
    )
    _run(draft_store.initialize())
    schema_store = TeamSubagentStore(str(db_path))
    _run(schema_store.initialize())
    owner = User(
        id="user_owner",
        team_id="team_a",
        email="owner@example.com",
    )
    other = User(
        id="user_other",
        team_id="team_a",
        email="other@example.com",
    )
    now = _now()
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO teams(
              id,name,max_seats,status,team_credit_balance,created_at
            ) VALUES (?,?,?,?,?,?)
            """,
            ("team_a", "Team A", 10, "active", 1000, now),
        )
        for user in (owner, other):
            connection.execute(
                """
                INSERT INTO users(
                  id,team_id,email,status,credit_balance,created_at
                ) VALUES (?,?,?,?,?,?)
                """,
                (
                    user.id,
                    user.team_id,
                    user.email,
                    "active",
                    1000,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO team_members(
                  id,user_id,team_id,role,status,created_at
                ) VALUES (?,?,?,?,?,?)
                """,
                (
                    f"tm_{user.id}",
                    user.id,
                    user.team_id,
                    "member",
                    "active",
                    now,
                ),
            )
        connection.commit()
    return user_store, draft_store, schema_store, owner, other


def _create_ready_draft(
    user_store: UserStore,
    draft_store: SubagentDraftStore,
    owner: User,
    *,
    tool_refs: list[str] | None = None,
    warnings: list[dict[str, str]] | None = None,
    validation_issues: list[dict[str, object]] | None = None,
    recommendations: list[dict[str, object]] | None = None,
    builtin_plugin_ids: list[str] | None = None,
):
    catalog = _run(
        SubagentAuthoringCatalogService().build(
            store=user_store,
            team_id=owner.team_id,
            actor_user_id=owner.id,
            actor_active=True,
            mcp_entitled=True,
        ),
    )
    definition = {
        "name": "weekly-sales-review",
        "display_name": "每周销售复盘",
        "description": "分析团队每周销售趋势",
        "system_prompt": "Analyze weekly sales using available evidence.",
        "tool_refs": ["bi_query", "bi_schema"] if tool_refs is None else tool_refs,
        "max_iterations": 10,
        "skills": [],
        "model": "analyst",
        "output_schema": None,
        "visibility": "creator_only",
        **({"builtin_plugin_ids": builtin_plugin_ids} if builtin_plugin_ids else {}),
    }
    draft = _run(
        draft_store.create_draft(
            SubagentDraftCreate(
                id="sad_route_ready",
                team_id=owner.team_id,
                owner_user_id=owner.id,
                source_mode="intent",
                source_turn_id=None,
                status="blocked" if validation_issues else "ready",
                validation_status="invalid" if validation_issues else "valid",
                locale="zh-CN",
                purpose_text="每周分析销售趋势",
                goal_summary="每周销售趋势",
                definition=definition,
                recommendations=recommendations or [],
                assumptions=[],
                warnings=warnings or [],
                clarification=None,
                validation_issues=validation_issues or [],
                catalog_fingerprint=catalog.fingerprint,
                definition_fingerprint=definition_fingerprint(
                    definition,
                ),
                idempotency_key_hash="a" * 64,
                mutation_id="11111111-1111-4111-8111-111111111111",
            ),
        ),
    )
    return draft


def _client(
    user_store: UserStore,
    draft_store: SubagentDraftStore,
    schema_store: TeamSubagentStore,
    user: User,
) -> TestClient:
    app = FastAPI()
    # Production mounts this (main.py): If-Match W/ stripped before route reads it. Otherwise tests pass, but every production write returns 409.
    app.add_middleware(ConditionalHeaderCanonicalizationMiddleware)
    app.include_router(drafts.router, prefix=_BASE)
    app.include_router(
        team_subagents.router,
        prefix="/api/v1/team-subagents",
    )
    app.state.user_store = user_store
    app.state.subagent_draft_store = draft_store
    app.state.team_subagent_schema_store = schema_store
    app.dependency_overrides[drafts._current_user] = lambda: user
    app.dependency_overrides[team_subagents._current_user] = lambda: user
    return TestClient(app)


def test_list_resumable_drafts_is_owner_scoped_and_excludes_discarded(tmp_path, monkeypatch):
    user_store, draft_store, schema_store, owner, other = _setup(tmp_path, monkeypatch)
    draft = _create_ready_draft(user_store, draft_store, owner)
    with _client(user_store, draft_store, schema_store, other) as client:
        response = client.get(_BASE)
        assert response.status_code == 200
        assert response.json()["items"] == []
    with _client(user_store, draft_store, schema_store, owner) as client:
        response = client.get(_BASE)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store, private"
        assert response.json()["items"] == [
            {
                "draft_id": draft.id,
                "draft_status": "ready",
                "source_mode": "intent",
                "title": "每周销售趋势",
                "updated_at": draft.updated_at,
                "expires_at": draft.expires_at,
            }
        ]
    _run(
        draft_store.discard_draft(
            draft.id,
            team_id=owner.team_id,
            owner_user_id=owner.id,
            expected_version=draft.row_version,
        )
    )
    with _client(user_store, draft_store, schema_store, owner) as client:
        assert client.get(_BASE).json()["items"] == []


def _lazy_store_client(user_store: UserStore, user: User) -> TestClient:
    """A client whose app has no started draft store, so routes build it lazily."""
    app = FastAPI()
    app.include_router(drafts.router, prefix=_BASE)
    app.state.user_store = user_store
    app.dependency_overrides[drafts._current_user] = lambda: user
    return TestClient(app)


def test_draft_routes_answer_503_while_the_draft_schema_is_not_migrated(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BOWHEAD_JWT_SECRET", "test-secret-for-draft-hmac")
    db_path = tmp_path / "baseline.db"
    with sqlite3.connect(db_path) as connection:
        connection.executescript(initial_schema("core"))
        connection.commit()
    owner = User(id="user_owner", team_id="team_a", email="owner@example.com")
    with _lazy_store_client(UserStore(str(db_path)), owner) as client:
        listed = client.get(_BASE)
        detail = client.get(f"{_BASE}/sad_missing")
    for response in (listed, detail):
        assert response.status_code == 503, response.text
        assert response.json()["detail"]["error"]["code"] == "subagent_draft_store_unavailable"


def test_draft_routes_answer_503_without_a_jwt_keyring(tmp_path, monkeypatch) -> None:
    user_store, _draft_store, _schema_store, owner, _other = _setup(tmp_path, monkeypatch)
    for name in ("BOWHEAD_JWT_SECRET", "BOWHEAD_JWT_SECRETS"):
        monkeypatch.delenv(name, raising=False)
    with _lazy_store_client(user_store, owner) as client:
        response = client.get(_BASE)
    assert response.status_code == 503, response.text
    assert response.json()["detail"]["error"]["code"] == "subagent_draft_store_unavailable"


def test_draft_list_never_titles_a_draft_with_its_identifier(tmp_path, monkeypatch) -> None:
    """A blank draft's generated identifier ("agent-d9db64") is not a title; the client shows "Untitled draft"."""
    user_store, draft_store, schema_store, owner, _ = _setup(tmp_path, monkeypatch)
    now = _now()
    with sqlite3.connect(user_store.db_path) as connection:
        connection.execute("INSERT INTO workspaces(id,team_id,name,created_at,updated_at) VALUES (?,?,?,?,?)", ("ws_a", owner.team_id, "A", now, now))
        connection.execute("INSERT INTO workspace_members(workspace_id,user_id,role,joined_at) VALUES (?,?,?,?)", ("ws_a", owner.id, "member", now))
    with _client(user_store, draft_store, schema_store, owner) as client:
        created = client.post(
            _BASE,
            headers={"Idempotency-Key": "blank-draft", "X-Space-Id": "ws_a"},
            json={"schema_version": 2, "source_mode": "manual", "purpose": ""},
        )
        assert created.status_code == 201, created.text
        saved = client.patch(
            f"{_BASE}/{created.json()['draft_id']}",
            headers={"X-Space-Id": "ws_a", "If-Match": created.json()["etag"]},
            json={
                "schema_version": 2,
                "purpose": "",
                "definition": {
                    "name": "agent-d9db64",
                    "display_name": "",
                    "description": "",
                    "system_prompt": "",
                    "tool_refs": [],
                    "max_iterations": 10,
                    "model": "analyst",
                    "output_schema": None,
                    "output_schema_text": "",
                    "visibility": "creator_only",
                },
            },
        )
        assert saved.status_code == 200, saved.text
        listed = client.get(f"{_BASE}?schema_version=2", headers={"X-Space-Id": "ws_a"})
    assert listed.status_code == 200, listed.text
    assert listed.json()["items"][0]["title"] == ""


def test_manual_draft_v2_saves_and_resumes_only_in_its_space(tmp_path, monkeypatch) -> None:
    user_store, draft_store, schema_store, owner, other = _setup(tmp_path, monkeypatch)
    monkeypatch.setenv("FEATURE_SUBAGENT_TEXT_ONLY", "1")
    monkeypatch.setenv("FEATURE_SUBAGENT_TEXT_ONLY_PCT", "100")
    now = _now()
    with sqlite3.connect(user_store.db_path) as connection:
        for space_id in ("ws_a", "ws_b"):
            connection.execute(
                "INSERT INTO workspaces(id,team_id,name,created_at,updated_at) VALUES (?,?,?,?,?)",
                (space_id, owner.team_id, space_id, now, now),
            )
            connection.execute(
                "INSERT INTO workspace_members(workspace_id,user_id,role,joined_at) VALUES (?,?,?,?)",
                (space_id, owner.id, "member", now),
            )
        connection.commit()
    with _client(user_store, draft_store, schema_store, owner) as client:
        headers = {"Idempotency-Key": "manual-new-task", "X-Space-Id": "ws_a"}
        created = client.post(
            _BASE,
            headers=headers,
            json={"schema_version": 2, "source_mode": "manual", "purpose": "只总结文本"},
        )
        assert created.status_code == 201, created.text
        draft_id = created.json()["draft_id"]
        assert created.json()["draft_status"] == "editing"
        assert (
            client.post(
                _BASE,
                headers=headers,
                json={"schema_version": 2, "source_mode": "manual", "purpose": "只总结文本"},
            ).json()["draft_id"]
            == draft_id
        )
        assert (
            client.post(
                _BASE,
                headers=headers,
                json={"schema_version": 2, "source_mode": "manual", "purpose": "不同内容"},
            ).status_code
            == 409
        )
        definition = {
            "name": "",
            "display_name": "文字摘要助手",
            "description": "只总结文本",
            "system_prompt": "",
            "tool_refs": [],
            "max_iterations": 10,
            "model": "analyst",
            "output_schema": None,
            "output_schema_text": "{尚未写完",
            "visibility": "creator_only",
        }
        saved = client.patch(
            f"{_BASE}/{draft_id}",
            headers={"X-Space-Id": "ws_a", "If-Match": created.json()["etag"]},
            json={"schema_version": 2, "purpose": "只总结文本（已修改）", "definition": definition, "text_only_confirmed": True},
        )
        assert saved.status_code == 200, saved.text
        assert saved.json()["definition"] == definition
        assert saved.json()["version"] == 2
        # The Canvas toolbar shows the last save time ("Saved 14:25").
        assert saved.json()["updated_at"]
        assert (
            client.post(
                f"{_BASE}/{draft_id}/validate",
                headers={"X-Space-Id": "ws_a", "If-Match": saved.json()["etag"]},
            ).status_code
            == 422
        )
        assert (
            client.post(
                _BASE,
                headers=headers,
                json={"schema_version": 2, "source_mode": "manual", "purpose": "只总结文本"},
            ).json()["draft_id"]
            == draft_id
        )
        assert (
            client.patch(
                f"{_BASE}/{draft_id}",
                headers={"X-Space-Id": "ws_a", "If-Match": created.json()["etag"]},
                json={"schema_version": 2, "purpose": "只总结文本", "definition": definition},
            ).status_code
            == 409
        )
        listed = client.get(f"{_BASE}?schema_version=2", headers={"X-Space-Id": "ws_a"})
        assert listed.status_code == 200, listed.text
        assert [item["draft_id"] for item in listed.json()["items"]] == [draft_id]
        assert listed.json()["items"][0]["title"] == "文字摘要助手"
        assert client.get(f"{_BASE}/{draft_id}?schema_version=2", headers={"X-Space-Id": "ws_a"}).json()["definition"] == definition
        assert client.get(f"{_BASE}/{draft_id}?schema_version=2", headers={"X-Space-Id": "ws_b"}).status_code == 404
        assert client.get(f"{_BASE}?schema_version=2", headers={"X-Space-Id": "ws_b"}).json()["items"] == []
        assert client.get(f"{_BASE}/{draft_id}").status_code == 404
        assert client.post(f"{_BASE}/{draft_id}/create", json={"schema_version": 1, "mutation_id": saved.json()["mutation_id"]}).status_code == 422
        complete = {
            **definition,
            "name": "text-summary",
            "display_name": "文本总结",
            "system_prompt": "只根据用户提供的文本总结，不访问外部资源。",
            "output_schema_text": "",
        }
        updated = client.patch(
            f"{_BASE}/{draft_id}",
            headers={"X-Space-Id": "ws_a", "If-Match": saved.json()["etag"]},
            json={"schema_version": 2, "purpose": "只总结文本", "definition": complete, "text_only_confirmed": True},
        )
        assert updated.status_code == 200, updated.text
        reviewed = client.post(
            f"{_BASE}/{draft_id}/validate",
            headers={"X-Space-Id": "ws_a", "If-Match": updated.json()["etag"]},
        )
        assert reviewed.status_code == 200, reviewed.text
        assert reviewed.json()["draft_status"] == "ready"
        assert reviewed.json()["actions"]["create"] is True
        created_agent = client.post(
            f"{_BASE}/{draft_id}/create",
            headers={"X-Space-Id": "ws_a", "If-Match": reviewed.json()["etag"]},
            json={"schema_version": 2, "mutation_id": reviewed.json()["mutation_id"], "acknowledged_warning_codes": [TEXT_ONLY_CONFIRMATION]},
        )
        assert created_agent.status_code == 201, created_agent.text
        assert created_agent.json()["visibility"] == "creator_only"
        replayed = client.post(
            f"{_BASE}/{draft_id}/create",
            headers={"X-Space-Id": "ws_a"},
            json={"schema_version": 2, "mutation_id": reviewed.json()["mutation_id"]},
        )
        assert replayed.json()["definition_id"] == created_agent.json()["definition_id"]
    with _client(user_store, draft_store, schema_store, other) as client:
        assert client.get(f"{_BASE}/{draft_id}?schema_version=2", headers={"X-Space-Id": "ws_a"}).status_code == 404
    with _client(user_store, draft_store, schema_store, owner) as client:
        recovered = client.get(f"{_BASE}/{draft_id}?schema_version=2", headers={"X-Space-Id": "ws_a"})
        assert recovered.status_code == 200
        assert recovered.json()["actions"]["edit"] is False


def test_ai_draft_converts_to_same_manual_id_and_creates_in_bound_space(tmp_path, monkeypatch) -> None:
    user_store, draft_store, schema_store, owner, other = _setup(tmp_path, monkeypatch)
    draft = _create_ready_draft(user_store, draft_store, owner)
    now = _now()
    with sqlite3.connect(user_store.db_path) as connection:
        for space_id in ("ws_a", "ws_b"):
            connection.execute(
                "INSERT INTO workspaces(id,team_id,name,created_at,updated_at) VALUES (?,?,?,?,?)",
                (space_id, owner.team_id, space_id, now, now),
            )
            connection.execute(
                "INSERT INTO workspace_members(workspace_id,user_id,role,joined_at) VALUES (?,?,?,?)",
                (space_id, owner.id, "member", now),
            )
        connection.commit()
    definition = {
        **{key: value for key, value in draft.definition.items() if key != "skills"},
        "display_name": "我手改的销售复盘",
        "description": "保留手动填写的目标",
        "output_schema_text": "",
    }
    body = {
        "schema_version": 2,
        "purpose": "每周分析销售趋势；重点看新客",
        "definition": definition,
        "text_only_confirmed": False,
    }
    with _client(user_store, draft_store, schema_store, owner) as client:
        headers = {"X-Space-Id": "ws_a", "If-Match": f'W/"{draft.id}:{draft.row_version}"'}
        converted = client.post(f"{_BASE}/{draft.id}/manualize", headers=headers, json=body)
        assert converted.status_code == 200, converted.text
        assert converted.json()["draft_id"] == draft.id
        assert converted.json()["source_mode"] == "intent"
        assert converted.json()["entry_mode"] == "canvas"
        assert converted.json()["definition"] == definition
        assert converted.json()["version"] == draft.row_version + 1
        with pytest.raises(SubagentDraftVersionConflictError):
            _run(
                draft_store.update_review(
                    draft.id,
                    team_id=owner.team_id,
                    owner_user_id=owner.id,
                    expected_version=draft.row_version,
                    definition=draft.definition,
                    recommendations=draft.recommendations,
                    assumptions=[],
                    warnings=[],
                    validation_issues=[],
                    catalog_fingerprint=draft.catalog_fingerprint or "",
                    definition_fingerprint=draft.definition_fingerprint or "",
                    validation_status="valid",
                    status="ready",
                )
            )
        assert client.post(f"{_BASE}/{draft.id}/manualize", headers=headers, json=body).json() == converted.json()
        assert client.post(f"{_BASE}/{draft.id}/manualize", headers=headers, json={**body, "purpose": "other"}).status_code == 409
        assert client.get(f"{_BASE}/{draft.id}").status_code == 404
        assert client.get(_BASE).json()["items"] == []
        assert client.get(f"{_BASE}/{draft.id}?schema_version=2", headers={"X-Space-Id": "ws_b"}).status_code == 404
        assert [item["draft_id"] for item in client.get(f"{_BASE}?schema_version=2", headers={"X-Space-Id": "ws_a"}).json()["items"]] == [draft.id]
        reviewed = client.post(
            f"{_BASE}/{draft.id}/validate",
            headers={"X-Space-Id": "ws_a", "If-Match": converted.json()["etag"]},
        )
        assert reviewed.status_code == 200, reviewed.text
        assert reviewed.json()["definition"]["display_name"] == "我手改的销售复盘"
        created = client.post(
            f"{_BASE}/{draft.id}/create",
            headers={"X-Space-Id": "ws_a", "If-Match": reviewed.json()["etag"]},
            json={"schema_version": 2, "mutation_id": reviewed.json()["mutation_id"]},
        )
        assert created.status_code == 201, created.text
        assert created.json()["draft_id"] == draft.id
    with _client(user_store, draft_store, schema_store, other) as client:
        assert client.get(f"{_BASE}/{draft.id}?schema_version=2", headers={"X-Space-Id": "ws_a"}).status_code == 404


def test_chat_edits_saved_manual_draft_in_same_space(tmp_path, monkeypatch) -> None:
    user_store, draft_store, schema_store, owner, other = _setup(tmp_path, monkeypatch)
    monkeypatch.setenv("FEATURE_SUBAGENT_DRAFT_CHAT_EDIT", "1")
    monkeypatch.setenv("FEATURE_SUBAGENT_DRAFT_CHAT_EDIT_PCT", "100")
    now = _now()
    with sqlite3.connect(user_store.db_path) as connection:
        connection.execute(
            "INSERT INTO workspaces(id,team_id,name,created_at,updated_at) VALUES (?,?,?,?,?)",
            ("ws_a", owner.team_id, "Space A", now, now),
        )
        connection.execute(
            "INSERT INTO workspace_members(workspace_id,user_id,role,joined_at) VALUES (?,?,?,?)",
            ("ws_a", owner.id, "member", now),
        )
        connection.commit()

    async def writable(_request) -> None:
        return None

    monkeypatch.setattr(chat_editor, "assert_not_readonly", writable)
    with _client(user_store, draft_store, schema_store, owner) as client:
        created = client.post(
            _BASE,
            headers={"X-Space-Id": "ws_a", "Idempotency-Key": "chat-manual-task"},
            json={"schema_version": 2, "source_mode": "manual", "purpose": "阅读网页"},
        )
        assert created.status_code == 201, created.text
        draft_id = created.json()["draft_id"]
        request = Request({"type": "http", "app": client.app, "headers": []})
        editor = chat_editor.SubagentDraftChatEditor(request, owner, draft_id, 1, workspace_id="ws_a")
        result = _run(editor.edit({"description": "只阅读用户给定的网页"}))
        assert result["applied"] is True and result["version"] == 2
        saved = client.get(f"{_BASE}/{draft_id}?schema_version=2", headers={"X-Space-Id": "ws_a"})
        assert saved.json()["definition"]["description"] == "只阅读用户给定的网页"
        assert _run(editor.edit({"description": "旧版本不能覆盖"}))["outcome"] == "version_conflict"
        assert (
            _run(chat_editor.SubagentDraftChatEditor(request, owner, draft_id, 2, workspace_id="ws_b").inspect())["outcome"]
            == "draft_unavailable_or_changed"
        )
        assert (
            _run(chat_editor.SubagentDraftChatEditor(request, other, draft_id, 2, workspace_id="ws_a").inspect())["outcome"]
            == "draft_unavailable_or_changed"
        )


def test_draft_review_locks_unavailable_approval_tool_and_blocks_creation(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from bowhead_core.openapi_tool_source import TeamToolEntry

    user_store, draft_store, schema_store, owner, _other = _setup(tmp_path, monkeypatch)
    ref = "teammcp.crm.update"

    async def load(_team_id, _store):
        return [
            TeamToolEntry(
                source_id="crm",
                advertised_name=ref,
                dispatch_name="Update CRM",
                description="Update a customer",
                input_schema={"type": "object"},
                destructive=True,
                kind="mcp",
                read_only_hint=False,
                destructive_hint=True,
            )
        ], {"crm": SimpleNamespace(name="CRM", updated_at="v1")}

    monkeypatch.setattr("bowhead_core.subagent_authoring_catalog.load_team_tool_entries", load)
    monkeypatch.setenv("BOWHEAD_APPROVAL_WRITE_ENABLED", "0")
    draft = _create_ready_draft(user_store, draft_store, owner, tool_refs=["bi_query", ref])
    definition = {key: value for key, value in draft.definition.items() if key not in {"tool_refs", "skills"}}
    client = _client(user_store, draft_store, schema_store, owner)
    response = client.patch(
        f"{_BASE}/{draft.id}",
        headers={"If-Match": f'W/"{draft.id}:1"'},
        json={"schema_version": 1, "definition": definition, "selected_tool_refs": ["bi_query", ref], "selected_skills": []},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    selected = next(tool for tool in body["tools"] if tool["tool_ref"] == ref)
    assert selected["selected"] is True  # Retain it visibly so the author can remove it.
    assert selected["locked_reason"] == "approval_executor_unavailable"
    assert body["actions"]["create"] is False
    repaired = client.patch(
        f"{_BASE}/{draft.id}",
        headers={"If-Match": body["etag"]},
        json={"schema_version": 1, "definition": definition, "selected_tool_refs": ["bi_query"], "selected_skills": []},
    )
    assert repaired.status_code == 200, repaired.text
    assert repaired.json()["actions"]["create"] is True
    assert repaired.json()["definition"]["tool_refs"] == ["bi_query"]


def test_draft_review_preserves_recommendation_provenance_until_selection_changes(tmp_path, monkeypatch) -> None:
    user_store, draft_store, schema_store, owner, _other = _setup(tmp_path, monkeypatch)
    draft = _create_ready_draft(
        user_store,
        draft_store,
        owner,
        recommendations=[
            {
                "tool_ref": "bi_query",
                "display_name": "查询数据",
                "selected": True,
                "evidence": ["intent:ci_1"],
                "confidence": "medium",
                "tier": "required",
                "reason": "读取团队销售数据所需。",
            },
        ],
    )
    definition = {key: value for key, value in draft.definition.items() if key not in {"tool_refs", "skills"}}
    client = _client(user_store, draft_store, schema_store, owner)
    renamed = client.patch(
        f"{_BASE}/{draft.id}",
        headers={"If-Match": f'W/"{draft.id}:1"'},
        json={
            "schema_version": 1,
            "definition": {**definition, "display_name": "门店销售分析"},
            "selected_tool_refs": ["bi_query", "bi_schema"],
            "selected_skills": [],
        },
    )
    assert renamed.status_code == 200, renamed.text
    original = next(tool for tool in renamed.json()["tools"] if tool["tool_ref"] == "bi_query")
    assert (original["reason"], original["evidence"], original["confidence"]) == (
        "读取团队销售数据所需。",
        ["intent:ci_1"],
        "medium",
    )

    deselected = client.patch(
        f"{_BASE}/{draft.id}",
        headers={"If-Match": renamed.json()["etag"]},
        json={"schema_version": 1, "definition": definition, "selected_tool_refs": ["bi_schema"], "selected_skills": []},
    )
    assert deselected.status_code == 200, deselected.text
    reselected = client.patch(
        f"{_BASE}/{draft.id}",
        headers={"If-Match": deselected.json()["etag"]},
        json={
            "schema_version": 1,
            "definition": definition,
            "selected_tool_refs": ["bi_query", "bi_schema"],
            "selected_skills": [],
        },
    )
    assert reselected.status_code == 200, reselected.text
    changed = next(tool for tool in reselected.json()["tools"] if tool["tool_ref"] == "bi_query")
    assert (changed["reason"], changed["evidence"], changed["confidence"]) == (
        "由你选择。",
        ["user_selected"],
        "high",
    )


def test_unmet_required_need_survives_edits_until_a_covering_tool_is_selected(tmp_path, monkeypatch):
    user_store, draft_store, schema_store, owner, _other = _setup(tmp_path, monkeypatch)
    issue = {
        "schema_version": 1,
        "code": REQUIRED_CAPABILITY_UNMET,
        "severity": "error",
        "field": "tool_refs",
        "tool_ref": None,
        "message_key": f"subagent.validation.{REQUIRED_CAPABILITY_UNMET}",
        "message": "必需能力「查询数据」没有选中任何工具",
        "details": {"intent_id": "ci_1", "capability_class": "data_query", "cause": "tool_locked", "candidate_tool_refs": ["bi_query"]},
    }
    draft = _create_ready_draft(user_store, draft_store, owner, tool_refs=["bi_schema"], validation_issues=[issue])
    definition = {key: value for key, value in draft.definition.items() if key not in {"tool_refs", "skills"}}
    client = _client(user_store, draft_store, schema_store, owner)
    assert client.get(f"{_BASE}/{draft.id}").json()["actions"]["create"] is False

    renamed = client.patch(
        f"{_BASE}/{draft.id}",
        headers={"If-Match": f'W/"{draft.id}:1"'},
        json={
            "schema_version": 1,
            "definition": {**definition, "display_name": "门店营业额周报"},
            "selected_tool_refs": ["bi_schema"],
            "selected_skills": [],
        },
    )
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["validation"]["status"] == "invalid"
    assert renamed.json()["validation"]["issues"] == [issue]
    assert renamed.json()["actions"]["create"] is False

    repaired = client.patch(
        f"{_BASE}/{draft.id}",
        headers={"If-Match": renamed.json()["etag"]},
        json={"schema_version": 1, "definition": definition, "selected_tool_refs": ["bi_query", "bi_schema"], "selected_skills": []},
    )
    assert repaired.status_code == 200, repaired.text
    assert repaired.json()["validation"] == {**repaired.json()["validation"], "status": "valid", "issues": []}
    assert repaired.json()["actions"]["create"] is True


def test_static_drafts_route_precedes_subagent_id_and_replays(
    tmp_path,
    monkeypatch,
) -> None:
    user_store, draft_store, schema_store, owner, _other = _setup(
        tmp_path,
        monkeypatch,
    )
    draft = _create_ready_draft(user_store, draft_store, owner)
    client = _client(user_store, draft_store, schema_store, owner)
    client.app.state.subagent_draft_service = _ReplayService(draft)

    response = client.post(
        _BASE,
        headers={"Idempotency-Key": "route-order-key"},
        json={
            "schema_version": 1,
            "source_mode": "intent",
            "purpose": "每周分析销售趋势",
            "locale": "zh-CN",
        },
    )

    assert response.status_code == 201, response.text
    assert response.json()["kind"] == "team_subagent_draft_detail"
    assert response.json()["draft_id"] == draft.id


def test_draft_detail_is_owner_only_without_existence_leak(
    tmp_path,
    monkeypatch,
) -> None:
    user_store, draft_store, schema_store, owner, other = _setup(
        tmp_path,
        monkeypatch,
    )
    draft = _create_ready_draft(user_store, draft_store, owner)

    owner_response = _client(
        user_store,
        draft_store,
        schema_store,
        owner,
    ).get(f"{_BASE}/{draft.id}")
    other_response = _client(
        user_store,
        draft_store,
        schema_store,
        other,
    ).get(f"{_BASE}/{draft.id}")

    assert owner_response.status_code == 200
    assert owner_response.headers["etag"] == f'W/"{draft.id}:1"'
    assert other_response.status_code == 404
    assert other_response.json()["detail"]["error"]["code"] == ("subagent_draft_not_found")


def test_final_create_is_private_and_atomically_consumes_draft(
    tmp_path,
    monkeypatch,
) -> None:
    user_store, draft_store, schema_store, owner, _other = _setup(
        tmp_path,
        monkeypatch,
    )
    draft = _create_ready_draft(user_store, draft_store, owner)
    client = _client(user_store, draft_store, schema_store, owner)

    response = client.post(
        f"{_BASE}/{draft.id}/create",
        headers={"If-Match": f'W/"{draft.id}:1"'},
        json={
            "schema_version": 1,
            "mutation_id": draft.mutation_id,
            "acknowledged_warning_codes": [],
        },
    )

    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["visibility"] == "creator_only"
    persisted = _run(
        user_store.get_team_subagent(
            owner.team_id,
            payload["definition_id"],
        ),
    )
    consumed = _run(
        draft_store.get_draft(
            draft.id,
            team_id=owner.team_id,
            owner_user_id=owner.id,
        ),
    )
    assert persisted is not None
    assert persisted.visibility == "creator_only"
    assert persisted.origin_mode == "intent_draft"
    assert persisted.source_draft_id == draft.id
    assert consumed is not None
    assert consumed.status == "consumed"
    assert consumed.definition is None

    replay = client.post(
        f"{_BASE}/{draft.id}/create",
        headers={"If-Match": f'W/"{draft.id}:1"'},
        json={
            "schema_version": 1,
            "mutation_id": draft.mutation_id,
            "acknowledged_warning_codes": [],
        },
    )
    assert replay.status_code == 200, replay.text
    assert replay.json() == {
        "schema_version": 1,
        "kind": "team_subagent_created_from_draft",
        "replayed": True,
        "draft_id": draft.id,
        "definition_id": persisted.id,
    }


_BLOCKED_SKILL = {
    "kind": "skill",
    "name": "sales-report",
    "identity": "team:oskill_sales",
    "reason_codes": ["skill_pending_reapproval"],
}


def test_blocked_publication_names_the_blocked_skill(tmp_path, monkeypatch) -> None:
    from bowhead_core.agent_revision_store import AgentPublicationBlockedError

    user_store, draft_store, schema_store, owner, _ = _setup(tmp_path, monkeypatch)
    draft = _create_ready_draft(user_store, draft_store, owner)
    client = _client(user_store, draft_store, schema_store, owner)

    async def blocked(*_args, **_kwargs):
        raise AgentPublicationBlockedError("agent_dependency_blocked", reason_codes=("skill_pending_reapproval",), blocked_items=(_BLOCKED_SKILL,))

    monkeypatch.setattr(type(schema_store), "create_team_subagent", blocked)
    response = client.post(
        f"{_BASE}/{draft.id}/create",
        headers={"If-Match": f'W/"{draft.id}:1"'},
        json={"schema_version": 1, "mutation_id": draft.mutation_id, "acknowledged_warning_codes": []},
    )

    assert response.status_code == 422, response.text
    details = response.json()["detail"]["error"]["details"]
    assert details["reason_codes"] == ["skill_pending_reapproval"]
    assert details["blocked_items"] == [_BLOCKED_SKILL]


@pytest.mark.integration
def test_python_draft_creates_a_revision_with_exact_skill_scope(tmp_path, monkeypatch) -> None:
    user_store, draft_store, schema_store, owner, _ = _setup(tmp_path, monkeypatch)
    with sqlite3.connect(user_store.db_path) as db:
        db.execute("UPDATE team_subagent_schema_meta SET release_semantics_activated_at=? WHERE singleton=1", ("2026-09-07T00:00:00+00:00",))
    assert _run(schema_store.release_semantics_active())
    draft = _create_ready_draft(user_store, draft_store, owner, tool_refs=["python_sandbox"])
    client = _client(user_store, draft_store, schema_store, owner)
    response = client.post(
        f"{_BASE}/{draft.id}/create",
        headers={"If-Match": f'W/"{draft.id}:1"'},
        json={"schema_version": 1, "mutation_id": draft.mutation_id, "acknowledged_warning_codes": []},
    )
    assert response.status_code == 201, response.text
    with sqlite3.connect(user_store.db_path) as db:
        row = db.execute("SELECT definition_json FROM team_subagent_revision WHERE subagent_id=?", (response.json()["definition_id"],)).fetchone()
    assert row is not None
    grants = json.loads(row[0])["skill_grants"]
    assert len(grants) == 1
    assert grants[0]["skill_ref"] == {"kind": "builtin", "id": "builtin:python_sandbox", "name": "python_sandbox"}
    assert set(grants[0]["permissions"]) == {"read", "invoke"}


@pytest.mark.integration
def test_code_computation_plugin_draft_creates_without_storing_the_derived_grant(tmp_path, monkeypatch) -> None:
    user_store, draft_store, schema_store, owner, _ = _setup(tmp_path, monkeypatch)
    with sqlite3.connect(user_store.db_path) as db:
        db.execute("UPDATE team_subagent_schema_meta SET release_semantics_activated_at=? WHERE singleton=1", ("2026-09-07T00:00:00+00:00",))
    assert _run(schema_store.release_semantics_active())
    draft = _create_ready_draft(user_store, draft_store, owner, tool_refs=[], builtin_plugin_ids=["builtin:code-computation"])
    now = _now()
    with sqlite3.connect(user_store.db_path) as db:
        db.execute("INSERT INTO workspaces(id,team_id,name,created_at,updated_at) VALUES (?,?,?,?,?)", ("ws_a", owner.team_id, "A", now, now))
        db.execute("INSERT INTO workspace_members(workspace_id,user_id,role,joined_at) VALUES (?,?,?,?)", ("ws_a", owner.id, "member", now))
        # A Plugin selection is checked against the Space's resources, so the draft belongs to one.
        db.execute("UPDATE team_subagent_draft SET workspace_id='ws_a' WHERE id=?", (draft.id,))
    client = _client(user_store, draft_store, schema_store, owner)

    def create():
        return client.post(
            f"{_BASE}/{draft.id}/create",
            headers={"If-Match": f'W/"{draft.id}:1"'},
            json={"schema_version": 1, "mutation_id": draft.mutation_id, "acknowledged_warning_codes": []},
        )

    # The dependency check passes; the fixture's draft only lacks the Space-scoped fingerprint a real save records.
    stale = create()
    assert stale.status_code == 409 and stale.json()["detail"]["error"]["code"] == "subagent_catalog_changed", stale.text
    with sqlite3.connect(user_store.db_path) as db:
        db.execute(
            "UPDATE team_subagent_draft SET catalog_fingerprint=? WHERE id=?",
            (stale.json()["detail"]["error"]["details"]["current_catalog_fingerprint"], draft.id),
        )
    response = create()
    assert response.status_code == 201, response.text
    with sqlite3.connect(user_store.db_path) as db:
        row = db.execute(
            "SELECT definition_json,risk_json FROM team_subagent_revision WHERE subagent_id=?", (response.json()["definition_id"],)
        ).fetchone()
    assert row is not None
    stored = json.loads(row[0])
    # The Plugin's Python Skill grant is derived on every expansion; the revision keeps the Owner's selection.
    assert stored["builtin_plugin_ids"] == ["builtin:code-computation"]
    assert all(grant["skill_ref"]["name"] != "python_sandbox" for grant in stored.get("skill_grants", []))
    assert "precise_skill_scope_required" not in row[1]


@pytest.mark.integration
def test_directly_selected_python_tool_with_a_plugin_creates_with_its_skill_grant(tmp_path, monkeypatch) -> None:
    """Picking Run code next to a Plugin must not need a separate Skill pick (novice walkthrough B1)."""
    user_store, draft_store, schema_store, owner, _ = _setup(tmp_path, monkeypatch)
    with sqlite3.connect(user_store.db_path) as db:
        db.execute("UPDATE team_subagent_schema_meta SET release_semantics_activated_at=? WHERE singleton=1", ("2026-09-07T00:00:00+00:00",))
    draft = _create_ready_draft(user_store, draft_store, owner, tool_refs=["python_sandbox"], builtin_plugin_ids=["builtin:web-research"])
    now = _now()
    with sqlite3.connect(user_store.db_path) as db:
        db.execute("INSERT INTO workspaces(id,team_id,name,created_at,updated_at) VALUES (?,?,?,?,?)", ("ws_a", owner.team_id, "A", now, now))
        db.execute("INSERT INTO workspace_members(workspace_id,user_id,role,joined_at) VALUES (?,?,?,?)", ("ws_a", owner.id, "member", now))
        db.execute("UPDATE team_subagent_draft SET workspace_id='ws_a' WHERE id=?", (draft.id,))
    client = _client(user_store, draft_store, schema_store, owner)

    def create():
        return client.post(
            f"{_BASE}/{draft.id}/create",
            headers={"If-Match": f'W/"{draft.id}:1"'},
            json={"schema_version": 1, "mutation_id": draft.mutation_id, "acknowledged_warning_codes": []},
        )

    # Before the fix the preflight failed with subagent_dependencies_unavailable_or_changed (precise_skill_scope_required).
    stale = create()
    assert stale.status_code == 409 and stale.json()["detail"]["error"]["code"] == "subagent_catalog_changed", stale.text
    with sqlite3.connect(user_store.db_path) as db:
        db.execute(
            "UPDATE team_subagent_draft SET catalog_fingerprint=? WHERE id=?",
            (stale.json()["detail"]["error"]["details"]["current_catalog_fingerprint"], draft.id),
        )
    response = create()
    assert response.status_code == 201, response.text
    with sqlite3.connect(user_store.db_path) as db:
        row = db.execute(
            "SELECT definition_json,risk_json FROM team_subagent_revision WHERE subagent_id=?", (response.json()["definition_id"],)
        ).fetchone()
    assert row is not None
    stored = json.loads(row[0])
    sandbox = [grant for grant in stored["skill_grants"] if grant["skill_ref"]["name"] == "python_sandbox"]
    assert len(sandbox) == 1 and "invoke" in sandbox[0]["permissions"]
    assert "precise_skill_scope_required" not in row[1]


@pytest.mark.integration
def test_plugin_method_survives_original_draft_creation_and_exact_run_admission(tmp_path, monkeypatch) -> None:
    from pathlib import Path

    from bowhead_core.agent_execution_binding import _resolved_team_definition_json
    from bowhead_core.skill_upload import bundle_sha256

    from bowhead_core import system_plugins

    method = {
        "skill_ref": {"kind": "builtin", "id": "builtin:canvas-pptx", "name": "canvas-pptx"},
        "permissions": ["read"],
        "revision": "sha256:" + bundle_sha256(Path(__file__).parents[2] / "bowhead_skill/catalog/canvas-pptx"),
        "distribution": "reference",
    }
    registry = dict(system_plugins._registry())
    raw = registry["builtin:code-computation@1"].model_dump()
    raw.update(version=2, skill_grants=[method])
    plugin = system_plugins.BuiltinPlugin.model_validate(raw)
    registry[plugin.ref] = plugin
    monkeypatch.setattr(system_plugins, "_registry", lambda: registry)
    user_store, draft_store, schema_store, owner, _ = _setup(tmp_path, monkeypatch)
    with sqlite3.connect(user_store.db_path) as db:
        db.execute("UPDATE team_subagent_schema_meta SET release_semantics_activated_at=? WHERE singleton=1", ("2026-09-07T00:00:00+00:00",))
    draft = _create_ready_draft(user_store, draft_store, owner, tool_refs=[], builtin_plugin_ids=["builtin:code-computation"])
    now = _now()
    with sqlite3.connect(user_store.db_path) as db:
        db.execute(
            "INSERT INTO workspaces(id,team_id,name,created_at,updated_at) VALUES (?,?,?,?,?)", ("ws_method", owner.team_id, "Method", now, now)
        )
        db.execute("INSERT INTO workspace_members(workspace_id,user_id,role,joined_at) VALUES (?,?,?,?)", ("ws_method", owner.id, "member", now))
        db.execute("UPDATE team_subagent_draft SET workspace_id='ws_method' WHERE id=?", (draft.id,))
    client = _client(user_store, draft_store, schema_store, owner)

    def create():
        return client.post(
            f"{_BASE}/{draft.id}/create",
            headers={"If-Match": f'W/"{draft.id}:1"'},
            json={"schema_version": 1, "mutation_id": draft.mutation_id, "acknowledged_warning_codes": []},
        )

    stale = create()
    assert stale.status_code == 409 and stale.json()["detail"]["error"]["code"] == "subagent_catalog_changed", stale.text
    with sqlite3.connect(user_store.db_path) as db:
        db.execute(
            "UPDATE team_subagent_draft SET catalog_fingerprint=? WHERE id=?",
            (stale.json()["detail"]["error"]["details"]["current_catalog_fingerprint"], draft.id),
        )
    response = create()
    assert response.status_code == 201, response.text
    with sqlite3.connect(user_store.db_path) as db:
        original = db.execute(
            "SELECT definition_json FROM team_subagent_revision WHERE subagent_id=?", (response.json()["definition_id"],)
        ).fetchone()[0]
    assert json.loads(original)["builtin_plugin_ids"] == ["builtin:code-computation"]
    assert all(grant["skill_ref"]["name"] != "canvas-pptx" for grant in json.loads(original).get("skill_grants", []))
    admitted = _run(_resolved_team_definition_json(None, team_id=owner.team_id, snapshot={"definition_json": original}))
    [retained] = [grant for grant in json.loads(admitted)["skill_grants"] if grant["skill_ref"]["name"] == "canvas-pptx"]
    assert retained == method


def test_validate_body_does_not_persist_or_bump_version(
    tmp_path,
    monkeypatch,
) -> None:
    user_store, draft_store, schema_store, owner, _other = _setup(
        tmp_path,
        monkeypatch,
    )
    draft = _create_ready_draft(user_store, draft_store, owner)
    client = _client(user_store, draft_store, schema_store, owner)

    response = client.post(
        f"{_BASE}/{draft.id}/validate",
        headers={"If-Match": f'W/"{draft.id}:1"'},
        json={
            "schema_version": 1,
            "definition": {
                "name": "edited-sales-review",
                "display_name": "编辑后的销售复盘",
                "description": "只验证，不保存",
                "system_prompt": "Analyze sales safely.",
                "model": "analyst",
                "max_iterations": 8,
                "output_schema": None,
                "visibility": "creator_only",
            },
            "selected_tool_refs": ["bi_query", "bi_schema"],
            "selected_skills": [],
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["kind"] == "team_subagent_draft_validation"
    assert response.json()["persisted"] is False
    assert response.json()["version"] == 1
    persisted = _run(
        draft_store.get_draft(
            draft.id,
            team_id=owner.team_id,
            owner_user_id=owner.id,
        ),
    )
    assert persisted is not None
    assert persisted.row_version == 1
    assert persisted.definition is not None
    assert persisted.definition["name"] == "weekly-sales-review"


def test_text_only_draft_review_confirm_create_and_replay(tmp_path, monkeypatch):
    user_store, draft_store, schema_store, owner, other = _setup(tmp_path, monkeypatch)
    monkeypatch.setenv("FEATURE_SUBAGENT_TEXT_ONLY", "1")
    monkeypatch.setenv("FEATURE_SUBAGENT_TEXT_ONLY_TEAM_IDS", owner.team_id)
    warning = {"code": TEXT_ONLY_CONFIRMATION, "message": "Confirm supplied text only."}
    draft = _create_ready_draft(user_store, draft_store, owner, tool_refs=[], warnings=[warning])
    client = _client(user_store, draft_store, schema_store, owner)
    other_client = _client(user_store, draft_store, schema_store, other)
    assert other_client.get(f"{_BASE}/{draft.id}").status_code == 404
    assert client.get(f"{_BASE}/{draft.id}").json()["actions"]["create"] is True
    definition = {key: value for key, value in draft.definition.items() if key not in {"tool_refs", "skills"}}
    definition["system_prompt"] = "Summarize only text supplied by the user."
    reviewed = client.patch(
        f"{_BASE}/{draft.id}",
        headers={"If-Match": f'W/"{draft.id}:1"'},
        json={
            "definition": definition,
            "selected_tool_refs": [],
            "selected_skills": [],
        },
    )
    assert reviewed.status_code == 200, reviewed.text
    assert reviewed.json()["validation"]["status"] == "warning"
    assert reviewed.json()["warnings"] == [warning]
    body = {"mutation_id": draft.mutation_id, "acknowledged_warning_codes": []}
    assert client.post(f"{_BASE}/{draft.id}/create", headers={"If-Match": reviewed.headers["etag"]}, json=body).status_code == 422
    body["acknowledged_warning_codes"] = [TEXT_ONLY_CONFIRMATION]
    assert client.post(f"{_BASE}/{draft.id}/create", headers={"If-Match": f'W/"{draft.id}:1"'}, json=body).status_code == 409
    with sqlite3.connect(user_store.db_path) as db:
        db.execute("UPDATE team_subagent_schema_meta SET release_semantics_activated_at=? WHERE singleton=1", (_now(),))
    created = client.post(f"{_BASE}/{draft.id}/create", headers={"If-Match": reviewed.headers["etag"]}, json=body)
    assert created.status_code == 201, created.text
    asset_id = created.json()["definition_id"]
    stored = _run(user_store.get_team_subagent(owner.team_id, asset_id))
    assert stored.tool_refs == [] and stored.skills == []
    assert stored.visibility == "creator_only"
    with sqlite3.connect(user_store.db_path) as db:
        row = db.execute("SELECT definition_json FROM team_subagent_revision WHERE subagent_id=?", (asset_id,)).fetchone()
    assert json.loads(row[0])["tool_refs"] == []
    assert json.loads(row[0])["skill_grants"] == []
    monkeypatch.setenv("FEATURE_SUBAGENT_TEXT_ONLY", "0")
    replay = client.post(f"{_BASE}/{draft.id}/create", json=body)
    assert replay.status_code == 200, replay.text
    assert replay.json()["replayed"] is True
    assert replay.json()["definition_id"] == asset_id


@pytest.mark.parametrize("server_proposal,enabled", [(False, True), (True, False)])
def test_empty_draft_cannot_be_created_without_server_proposal_and_current_rollout(tmp_path, monkeypatch, server_proposal, enabled):
    user_store, draft_store, schema_store, owner, _ = _setup(tmp_path, monkeypatch)
    monkeypatch.setenv("FEATURE_SUBAGENT_TEXT_ONLY", "1" if enabled else "0")
    monkeypatch.setenv("FEATURE_SUBAGENT_TEXT_ONLY_TEAM_IDS", owner.team_id)
    warnings = [{"code": TEXT_ONLY_CONFIRMATION, "message": "Confirm text only."}] if server_proposal else []
    draft = _create_ready_draft(user_store, draft_store, owner, tool_refs=[], warnings=warnings)
    client = _client(user_store, draft_store, schema_store, owner)
    assert client.get(f"{_BASE}/{draft.id}").json()["actions"]["create"] is False
    definition = {key: value for key, value in draft.definition.items() if key not in {"tool_refs", "skills"}}
    validated = client.post(
        f"{_BASE}/{draft.id}/validate",
        headers={"If-Match": f'W/"{draft.id}:1"'},
        json={
            "definition": definition,
            "selected_tool_refs": [],
            "selected_skills": [],
        },
    )
    assert validated.status_code == 200, validated.text
    assert validated.json()["validation"]["status"] == "invalid"
    created = client.post(
        f"{_BASE}/{draft.id}/create",
        headers={"If-Match": f'W/"{draft.id}:1"'},
        json={
            "mutation_id": draft.mutation_id,
            "acknowledged_warning_codes": [TEXT_ONLY_CONFIRMATION],
        },
    )
    assert created.status_code == 422, created.text
    assert "subagent_tool_required" in created.text
    assert _run(user_store.get_team_subagent_by_name(owner.team_id, "weekly-sales-review", only_enabled=False)) is None


def test_canvas_suggestion_is_owner_space_bound_and_applied_by_selected_fields(tmp_path, monkeypatch) -> None:
    user_store, draft_store, schema_store, owner, other = _setup(tmp_path, monkeypatch)
    now = _now()
    with sqlite3.connect(user_store.db_path) as connection:
        connection.execute(
            "INSERT INTO workspaces(id,team_id,name,created_at,updated_at) VALUES (?,?,?,?,?)",
            ("ws_a", owner.team_id, "A", now, now),
        )
        connection.execute(
            "INSERT INTO workspace_members(workspace_id,user_id,role,joined_at) VALUES (?,?,?,?)",
            ("ws_a", owner.id, "member", now),
        )
        connection.commit()
    with _client(user_store, draft_store, schema_store, owner) as client:
        created = client.post(
            _BASE,
            headers={"X-Space-Id": "ws_a", "Idempotency-Key": "suggestion-manual-task"},
            json={"schema_version": 2, "source_mode": "manual", "purpose": ""},
        )
        assert created.status_code == 201, created.text
        draft_id = created.json()["draft_id"]
        claim = _run(
            draft_store.claim_request(
                team_id=owner.team_id,
                owner_user_id=owner.id,
                idempotency_key="suggestion-ai-request",
                request={"draft_id": draft_id, "base_version": 1},
            )
        )
        suggestion = _run(
            draft_store.record_canvas_suggestion(
                draft_id,
                team_id=owner.team_id,
                owner_user_id=owner.id,
                workspace_id="ws_a",
                request_key_hash=claim.idempotency_key_hash,
                lease_token=claim.lease_token,
                base_version=1,
                kind="definition",
                payload={
                    "purpose": "总结公开资料",
                    "definition": {
                        "name": "public-summary",
                        "description": "总结公开资料",
                        "system_prompt": "只总结已提供的资料",
                        "tool_refs": [],
                    },
                },
            )
        )
        client.app.state.subagent_draft_service = type(
            "FakeCanvasService",
            (),
            {
                "generate_canvas_suggestion": lambda self, **kwargs: _canvas_result(suggestion),
            },
        )()
        generated = client.post(
            f"{_BASE}/{draft_id}/suggestions",
            headers={"X-Space-Id": "ws_a", "If-Match": created.json()["etag"], "Idempotency-Key": "suggestion-route-request"},
            json={"schema_version": 2, "purpose": "总结公开资料"},
        )
        assert generated.status_code == 201, generated.text
        assert generated.json()["suggestion_id"] == suggestion.id
        listed = client.get(f"{_BASE}/{draft_id}/suggestions", headers={"X-Space-Id": "ws_a"})
        assert listed.status_code == 200 and len(listed.json()["items"]) == 1
        assert client.get(f"{_BASE}/{draft_id}/suggestions").status_code == 400
        with _client(user_store, draft_store, schema_store, other) as other_client:
            assert other_client.get(f"{_BASE}/{draft_id}/suggestions", headers={"X-Space-Id": "ws_a"}).status_code == 404
        applied = client.post(
            f"{_BASE}/{draft_id}/suggestions/{suggestion.id}/apply",
            headers={"X-Space-Id": "ws_a", "If-Match": created.json()["etag"]},
            json={
                "schema_version": 2,
                "selected_fields": ["name", "description", "system_prompt"],
                "mutation_id": "11111111-1111-4111-8111-111111111111",
            },
        )
        assert applied.status_code == 200, applied.text
        assert applied.json()["draft_id"] == draft_id
        assert applied.json()["definition"]["name"] == "public-summary"
        assert applied.json()["purpose"] == "总结公开资料"
        assert client.get(f"{_BASE}/{draft_id}/suggestions", headers={"X-Space-Id": "ws_a"}).json()["items"] == []
        second_claim = _run(
            draft_store.claim_request(
                team_id=owner.team_id,
                owner_user_id=owner.id,
                idempotency_key="suggestion-clarify-request",
                request={"draft_id": draft_id, "base_version": 2},
            )
        )
        clarification = _run(
            draft_store.record_canvas_suggestion(
                draft_id,
                team_id=owner.team_id,
                owner_user_id=owner.id,
                workspace_id="ws_a",
                request_key_hash=second_claim.idempotency_key_hash,
                lease_token=second_claim.lease_token,
                base_version=2,
                kind="clarification",
                payload={"question": "Which source?"},
            )
        )
        with _client(user_store, draft_store, schema_store, other) as other_client:
            assert (
                other_client.post(
                    f"{_BASE}/{draft_id}/suggestions/{clarification.id}/dismiss",
                    headers={"X-Space-Id": "ws_a"},
                ).status_code
                == 404
            )
        dismissed = client.post(
            f"{_BASE}/{draft_id}/suggestions/{clarification.id}/dismiss",
            headers={"X-Space-Id": "ws_a"},
        )
        assert dismissed.status_code == 200, dismissed.text
        assert (
            client.post(
                f"{_BASE}/{draft_id}/suggestions/{clarification.id}/dismiss",
                headers={"X-Space-Id": "ws_a"},
            ).status_code
            == 200
        )
        assert client.get(f"{_BASE}/{draft_id}/suggestions", headers={"X-Space-Id": "ws_a"}).json()["items"] == []


async def _canvas_result(suggestion):
    return CanvasGenerationResult(suggestion=suggestion)


class _ClearPurposeGenerator:
    """Stands in for the team model: returns one clear, tool-backed blueprint."""

    async def generate(self, **_kwargs):
        from bowhead_core.subagent_authoring_models import (
            CapabilityIntent,
            DraftBlueprint,
            GeneratedSystemPrompt,
            PurposeAssessment,
        )
        from bowhead_core.subagent_draft_generator import (
            StructuredOutputMode,
            SubagentDraftGenerationResult,
        )

        assessment = PurposeAssessment(
            outcome="clear",
            evidence_source_or_object=True,
            evidence_action=True,
            evidence_expected_result=True,
            clarification_question=None,
            reason_code="purpose_clear",
        )
        blueprint = DraftBlueprint(
            purpose_assessment=assessment,
            goal_summary="Analyze weekly store sales anomalies",
            display_name="Sales anomaly analyst",
            slug_seed="sales-anomaly",
            description="Analyze weekly store sales anomalies",
            system_prompt=GeneratedSystemPrompt(
                role="You are a sales analyst.",
                goals=["Identify material anomalies."],
                boundaries=["Do not modify source systems."],
                evidence_rules=["Use available evidence."],
                failure_behavior=["Explain missing data."],
                output_expectations=["Return concise findings."],
            ),
            capability_intents=[
                CapabilityIntent(
                    intent_id="ci_sales",
                    capability_class="data_query",
                    operation="read",
                    object_terms=["sales"],
                    source_preference="native",
                    necessity="required",
                    reason="Read weekly sales",
                    confidence=0.95,
                ),
            ],
            max_iterations_hint=10,
            assumptions=[],
            warnings=[],
        )
        return SubagentDraftGenerationResult(
            assessment=assessment,
            blueprint=blueprint,
            mode=StructuredOutputMode.JSON_OBJECT_WITH_VALIDATION,
            attempts=1,
            input_tokens=100,
            output_tokens=200,
        )


class _NoChargeAccountant:
    async def preflight(self, **_kwargs) -> None:
        return None

    async def settle(self, **_kwargs) -> None:
        return None

    async def release(self, **_kwargs) -> None:
        return None


class _RecordingBus:
    def __init__(self) -> None:
        self.events: list = []

    async def emit(self, event) -> bool:
        self.events.append(event)
        return True


def test_chat_request_drafts_a_private_subagent_with_default_configuration(tmp_path, monkeypatch) -> None:
    """An owner confirms conversational suggestions before private creation."""
    from types import SimpleNamespace

    from bowhead_core.subagent_creator_trigger import TOOL_NAME, register_subagent_creator_trigger
    from bowhead_core.subagent_draft_service import SubagentDraftService
    from bowhead_core.tool_registry import ToolRegistry

    user_store, draft_store, schema_store, owner, other = _setup(tmp_path, monkeypatch)
    now = _now()
    with sqlite3.connect(user_store.db_path) as connection:
        connection.execute(
            "INSERT INTO workspaces(id,team_id,name,created_at,updated_at) VALUES (?,?,?,?,?)",
            ("ws_a", owner.team_id, "Authoring Space", now, now),
        )
        connection.execute(
            "INSERT INTO workspace_members(workspace_id,user_id,role,joined_at) VALUES (?,?,?,?)",
            ("ws_a", owner.id, "member", now),
        )
        connection.commit()
    registry = ToolRegistry()
    assert register_subagent_creator_trigger(registry) is True
    service = SubagentDraftService(
        draft_store=draft_store,
        definition_store=user_store,
        catalog_service=SubagentAuthoringCatalogService(),
        generator=_ClearPurposeGenerator(),
        accountant=_NoChargeAccountant(),
    )
    bus = _RecordingBus()
    ctx = SimpleNamespace(
        parent_bus=bus,
        turn_id="t_chat_create",
        team_id=owner.team_id,
        user_id=owner.id,
        session_id="s_chat_create",
        workspace_id="ws_a",
        language_context=None,
        subagent_draft_service=service,
        subagent_draft_mcp_entitled=True,
        subagent_authoring_context_store=None,
    )

    outcome = _run(
        registry.get(TOOL_NAME).handler(
            {
                "action": "continue",
                "purpose": "每周分析门店销售异常并生成趋势图",
                "purpose_evidence": {"source_or_object": True, "action": True, "expected_result": True},
            },
            ctx,
        ),
    )

    assert outcome["outcome"] == "draft_opened", outcome
    draft_id = outcome["draft_id"]
    assert [event.payload["draft_id"] for event in bus.events] == [draft_id]
    assert bus.events[0].persist_before_delivery is True
    version = 2
    scope_headers = {"X-Space-Id": "ws_a"}
    with _client(user_store, draft_store, schema_store, owner) as client:
        detail = client.get(f"{_BASE}/{draft_id}?schema_version={version}", headers=scope_headers)
        assert detail.status_code == 200, detail.text
        assert detail.json()["draft_status"] == "editing"
        assert detail.json()["definition"] == {}
        suggestions = client.get(f"{_BASE}/{draft_id}/suggestions", headers=scope_headers).json()["items"]
        assert len(suggestions) == 1
        suggestion = suggestions[0]
        fields = ["name", "display_name", "description", "system_prompt", "tool_refs", "model", "output_schema_text"]
        applied = client.post(
            f"{_BASE}/{draft_id}/suggestions/{suggestion['suggestion_id']}/apply",
            headers={**scope_headers, "If-Match": detail.headers["ETag"]},
            json={"schema_version": 2, "selected_fields": fields, "mutation_id": "11111111-1111-4111-8111-111111111111"},
        )
        assert applied.status_code == 200, applied.text
        detail = client.post(
            f"{_BASE}/{draft_id}/validate",
            headers={**scope_headers, "If-Match": applied.headers["ETag"]},
        )
        assert detail.status_code == 200, detail.text
        body = detail.json()
        assert body["draft_status"] == "ready", body
        assert body["actions"]["create"] is True
        assert "bi_query" in body["definition"]["tool_refs"]
        created = client.post(
            f"{_BASE}/{draft_id}/create",
            headers={**scope_headers, "If-Match": detail.headers["ETag"]},
            json={
                "schema_version": version,
                "mutation_id": body["mutation_id"],
                "acknowledged_warning_codes": [warning["code"] for warning in body["warnings"]],
            },
        )
        assert created.status_code == 201, created.text
        definition_id = created.json()["definition_id"]
    persisted = _run(user_store.get_team_subagent(owner.team_id, definition_id))
    assert persisted is not None
    assert persisted.visibility == "creator_only"
    assert persisted.source_draft_id == draft_id
    with _client(user_store, draft_store, schema_store, other) as client:
        assert client.get(f"{_BASE}/{draft_id}?schema_version={version}", headers=scope_headers).status_code == 404
