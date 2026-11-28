"""Personal Teams protocol and SQLite storage regressions."""

from importlib import import_module
from sqlite3 import connect
from types import SimpleNamespace

import pytest
from bowhead_gateway.routes import teams
from bowhead_gateway.routes._deps import require_team_admin
from bowhead_gateway.teams.store import TeamsStore
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def teams_store(tmp_path):
    path = tmp_path / "teams.db"
    with connect(path) as db:
        db.executescript("""
            CREATE TABLE teams(id TEXT PRIMARY KEY);
            CREATE TABLE users(id TEXT PRIMARY KEY);
            CREATE TABLE workspaces(id TEXT PRIMARY KEY, team_id TEXT NOT NULL);
            INSERT INTO workspaces VALUES ('ws_a', 'team_a'), ('ws_b', 'team_b');
            INSERT INTO teams VALUES ('team_a'), ('team_b');
            INSERT INTO users VALUES ('usr_a'), ('usr_b');
        """)
        db.executescript(import_module("bowhead_core.migrations.versions.0074_teams_personal_connection")._DDL)
        db.execute("INSERT INTO teams_identity_bindings VALUES ('tenant','oid','usr_a','team_a','old','old')")
        db.execute("INSERT INTO teams_workspace_bindings VALUES ('tenant','conv','oid','ws_a','old')")
        db.execute("INSERT INTO teams_activity_dedup VALUES ('legacy-id','old')")
        import_module("bowhead_core.migrations.versions.0235_teams_personal_chat").upgrade_personal_chat(db.execute)
    return TeamsStore(path)


def test_admin_config_persists_partial_updates_and_rejects_invalid_enabled_config(teams_store, monkeypatch):
    monkeypatch.setattr(teams, "encrypt_llm_config", lambda _: "encrypted-fixture")
    app = FastAPI()
    app.state.teams_store = teams_store
    app.include_router(teams.router)
    app.dependency_overrides[require_team_admin] = lambda: SimpleNamespace(id="usr_a", team_id="team_a")
    with TestClient(app) as client:
        response = client.patch("/admin/config", json={"status": "draft", "entra_tenant_id": "tenant"})
        assert response.status_code == 200
        row_id = response.json()["id"]
        saved = client.patch(
            "/admin/config",
            json={
                "microsoft_app_id": "app",
                "app_secret": "test-secret",
                "sso_audience": "audience",
                "sso_connection_name": "oauth",
                "teams_manifest_id": "manifest",
                "status": "enabled",
            },
        )
        assert saved.status_code == 200
        assert saved.json()["id"] == row_id
        assert saved.json()["app_secret_configured"]
        assert "test-secret" not in saved.text
        assert client.patch("/admin/config", json={"sso_connection_name": "renamed"}).status_code == 200
        assert client.patch("/admin/config", json={"app_secret": None}).status_code == 422
        current = client.get("/admin/config").json()
        assert current["entra_tenant_id"] == "tenant"
        assert current["sso_connection_name"] == "renamed"
        assert current["app_secret_configured"]


async def test_migration_preserves_space_and_requires_confirmation_of_old_identity(teams_store):
    from sqlite3 import IntegrityError

    store = teams_store
    assert await store.get_workspace("tenant", "conv", "oid", "team_a") == "ws_a"
    assert await store.get_identity("tenant", "oid", "team_a") is None
    await store.bind_identity(tenant_id="tenant", oid="oid", user_id="usr_a", team_id="team_a")
    await store.bind_identity(tenant_id="tenant", oid="oid", user_id="usr_b", team_id="team_b")
    with pytest.raises(IntegrityError):
        await store.bind_identity(tenant_id="tenant", oid="oid", user_id="usr_b", team_id="team_a")
    await store.set_workspace("tenant", "conv", "oid", "ws_b", "team_b")
    assert await store.get_identity("tenant", "oid", "team_a") == ("usr_a", "team_a")
    assert await store.get_identity("tenant", "oid", "team_b") == ("usr_b", "team_b")
    assert await store.get_workspace("tenant", "conv", "oid", "team_a") == "ws_a"
    assert await store.get_workspace("tenant", "conv", "oid", "team_b") == "ws_b"
    async with store.process_activity("new-key", "legacy-id") as claimed:
        assert not claimed


async def test_activity_lease_releases_failed_attempt_and_fences_a_second_worker(teams_store):
    from bowhead_gateway.teams.store import ActivityInProgress

    async with teams_store.process_activity("key", "incoming") as first:
        assert first
        with pytest.raises(ActivityInProgress):
            async with TeamsStore(teams_store.db_path).process_activity("key", "incoming"):
                pytest.fail("Two workers claimed the same Activity")
    async with teams_store.process_activity("key", "incoming") as completed:
        assert not completed
    with pytest.raises(TimeoutError):
        async with teams_store.process_activity("second-key", "second") as claimed:
            assert claimed
            raise TimeoutError
    async with TeamsStore(teams_store.db_path).process_activity("second-key", "second") as retry:
        assert retry


async def test_scoped_session_reuses_history_only_in_its_original_space():
    from unittest.mock import AsyncMock

    sessions = SimpleNamespace(get_session=AsyncMock(return_value=SimpleNamespace(workspace_id="ws_old", user_id="usr_a")))
    app = SimpleNamespace(state=SimpleNamespace(orchestrator=SimpleNamespace(session_store=sessions)))
    connection = SimpleNamespace(id="conn", entra_tenant_id="tenant", microsoft_app_id="app")
    original = await teams.personal_session_id(app, connection, "conv", "usr_a", "ws_old")
    second = await teams.personal_session_id(app, connection, "conv", "usr_a", "ws_new")
    assert original == "teams:tenant:conv:usr_a"
    assert second != original
    assert await teams.personal_session_id(app, connection, "conv", "usr_a", "ws_new") == second
    assert await teams.personal_session_id(app, connection, "conv", "usr_b", "ws_old") != original


async def test_retry_delivers_committed_result_after_restart_and_rechecks_revocation(teams_store, monkeypatch):
    from unittest.mock import AsyncMock

    from bowhead_gateway.teams import delivery
    from cryptography.fernet import Fernet

    monkeypatch.setenv("BOWHEAD_FERNET_KEY", Fernet.generate_key().decode())
    store = teams_store
    connection = await store.upsert_connection(
        team_id="team_a",
        actor_id="usr_a",
        values={
            "entra_tenant_id": "tenant",
            "microsoft_app_id": "app",
            "app_secret_ref": "secret",
            "sso_audience": "sso",
            "sso_connection_name": "sso",
            "teams_manifest_id": "manifest",
            "status": "enabled",
        },
    )
    await store.bind_identity(tenant_id="tenant", oid="oid", user_id="usr_a", team_id="team_a")
    turn = SimpleNamespace(team_id="team_a", workspace_id="ws_a", user_id="usr_a", session_id="s_a", turn_id="t_a", status="success")
    session = SimpleNamespace(user_id="usr_a", workspace_id="ws_a", deleted_at=None)
    activity = {"from": {"aadObjectId": "oid"}, "conversation": {"id": "conv"}, "serviceUrl": "https://smba.trafficmanager.net/amer/"}
    event = SimpleNamespace(seq=1, event_type="final_answer", payload={"text": "Saved answer"})
    done = SimpleNamespace(seq=2, event_type="done", payload={"status": "success"})
    users = SimpleNamespace(
        get_user=AsyncMock(return_value=SimpleNamespace(status="active")),
        get_team_role=AsyncMock(return_value="member"),
        get_workspace_team_id=AsyncMock(return_value="team_a"),
        get_member_role=AsyncMock(return_value="member"),
        get_workspace=AsyncMock(return_value={"status": "active"}),
    )
    app = SimpleNamespace(
        state=SimpleNamespace(
            teams_store=store,
            user_store=users,
            orchestrator=SimpleNamespace(
                session_store=SimpleNamespace(get_turn=AsyncMock(return_value=turn), get_session=AsyncMock(return_value=session))
            ),
            turn_log_store=SimpleNamespace(max_seq=AsyncMock(return_value=2), list_events=AsyncMock(return_value=[event, done])),
        )
    )
    sender = AsyncMock(side_effect=[TimeoutError(), None])
    monkeypatch.setattr(teams, "_send_activity", sender)
    async with store.process_activity("key", "message"):
        await delivery.track_delivery(app, "key", activity, connection, turn, "en-US", "en-US")
    assert await delivery.deliver_next(app)
    with connect(store.db_path) as db:
        assert db.execute("SELECT delivery_seq FROM teams_activity_dedup WHERE activity_id='key'").fetchone() == (-1,)
        db.execute("UPDATE teams_activity_dedup SET delivery_due=0 WHERE activity_id='key'")
    app.state.teams_store = TeamsStore(store.db_path)
    assert await delivery.deliver_next(app)
    assert sender.await_count == 2
    assert sender.call_args.args[-1]["body"][1]["text"] == "Saved answer"
    assert await delivery.deliver_next(app)
    assert not await delivery.deliver_next(app)
    async with store.process_activity("second", "another"):
        await delivery.track_delivery(app, "second", activity, connection, turn, "en-US", "en-US")
    users.get_member_role.return_value = None
    assert await delivery.deliver_next(app)
    assert sender.await_count == 2
    with connect(store.db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM teams_activity_dedup WHERE delivery IS NOT NULL").fetchone() == (0,)


async def test_empty_drafts_can_coexist_and_claimed_app_ids_are_unique(teams_store):
    from sqlite3 import IntegrityError

    for team in ("team_a", "team_b"):
        await teams_store.upsert_connection(team_id=team, actor_id="usr_a", values={})
    fields = {"entra_tenant_id": "tenant", "microsoft_app_id": "app"}
    await teams_store.upsert_connection(team_id="team_a", actor_id="usr_a", values=fields)
    with pytest.raises(IntegrityError):
        await teams_store.upsert_connection(team_id="team_b", actor_id="usr_a", values=fields)
    assert (await teams_store.get_connection_for_team("team_b")).microsoft_app_id == ""


async def test_personal_binding_requires_the_signed_in_matching_member(teams_store, monkeypatch):
    from unittest.mock import AsyncMock
    from urllib.parse import parse_qs, urlsplit

    from bowhead_gateway.teams import identity

    monkeypatch.setenv("BOWHEAD_JWT_SECRET", "a-local-test-key-for-confirming-personal-accounts")
    app = FastAPI()
    app.include_router(teams.router)
    app.state.teams_store = teams_store
    active = AsyncMock(return_value=True)
    app.state.user_store = SimpleNamespace(user_has_active_team_membership=active)
    account = SimpleNamespace(id="usr_b")
    app.dependency_overrides[teams._current_user] = lambda: account
    conn = await teams_store.upsert_connection(
        team_id="team_a",
        actor_id="usr_a",
        values={
            "entra_tenant_id": "tenant",
            "microsoft_app_id": "app",
            "app_secret_ref": "secret",
            "sso_audience": "sso",
            "sso_connection_name": "sso",
            "teams_manifest_id": "manifest",
            "status": "enabled",
        },
    )
    monkeypatch.setattr(identity, "team_public_app_url", AsyncMock(return_value="https://example.test/prefix/me/integrations/teams/bind/"))
    offered = await identity.pairing_card(SimpleNamespace(app=app), conn, "tenant", "oid", "usr_a", "en-US")
    token = parse_qs(urlsplit(offered["actions"][0]["url"]).query)["pair"][0]
    with TestClient(app) as client:
        assert client.post("/bindings/confirm", json={"token": token}).status_code == 401
        client.headers["Authorization"] = "Bearer member-test-session"
        assert client.post("/bindings/confirm", json={"token": token}).status_code == 403
        account.id = "usr_a"
        active.return_value = False
        assert client.post("/bindings/confirm", json={"token": token}).status_code == 403
        assert await teams_store.get_identity("tenant", "oid", "team_a") is None
        active.return_value = True
        assert client.post("/bindings/confirm", json={"token": token}).status_code == 200
        assert await teams_store.get_identity("tenant", "oid", "team_a") == ("usr_a", "team_a")


async def test_personal_form_uses_stored_option_labels_and_typed_answers(monkeypatch):
    from unittest.mock import AsyncMock

    from bowhead_core.clarification_store import ClarificationRequest
    from bowhead_gateway.teams.clarification import _answers, question_card

    qs = [
        {"question": "Markets", "type": "multi_select", "options": ["A", "B"], "option_labels": ["Alpha", "Beta"]},
        {"question": "Notes", "type": "free_text", "options": []},
    ]
    card = ClarificationRequest(
        request_id="cr_1",
        turn_id="t_1",
        user_id="usr_a",
        workspace_id="ws_a",
        session_id="s_1",
        token="",
        rationale="",
        candidates=[],
        kind="ambiguous_turn",
        resume_context={"card_questions": qs, "question_round": {}},
    )
    app = SimpleNamespace(state=SimpleNamespace(clarification_store=SimpleNamespace(get=AsyncMock(return_value=card))))
    monkeypatch.delenv("FEATURE_TEAMS_CHAT_CLARIFICATION", raising=False)
    assert await question_card(app, "cr_1", "t_1", "en-US") is None
    monkeypatch.setenv("FEATURE_TEAMS_CHAT_CLARIFICATION", "1")
    form = await question_card(app, "cr_1", "t_1", "en-US")
    assert form["body"][0]["choices"] == [{"title": "Alpha", "value": "0"}, {"title": "Beta", "value": "1"}]
    assert form["body"][1]["type"] == "Input.Text"
    answers = _answers(qs, {"q0": "0,1", "q1": "Use retail only"}).answers
    assert [(answer.source, answer.answer, answer.option_index) for answer in answers] == [
        ("candidate", "A", 0),
        ("candidate", "B", 1),
        ("custom", "Use retail only", None),
    ]
    with pytest.raises(ValueError):
        _answers(qs, {"q0": "2", "q1": "Use retail only"})


async def test_connector_and_sso_require_the_configured_actor_and_channel(monkeypatch):
    from unittest.mock import Mock

    from bowhead_gateway.teams import identity
    from fastapi import HTTPException

    tenant = "12345678-1234-1234-1234-123456789abc"
    conn = SimpleNamespace(microsoft_app_id="app", sso_audience="resource", sso_auth_app_id="sso", sso_connection_name="oauth")
    key = SimpleNamespace(key=object(), _jwk_data={"endorsements": ["msteams"]})
    monkeypatch.setattr(identity, "_keys", lambda _url: SimpleNamespace(get_signing_key_from_jwt=lambda _token: key))
    activity = {
        "channelId": "msteams",
        "serviceUrl": "https://smba.trafficmanager.net/amer/",
        "from": {"aadObjectId": "actor"},
        "value": {"connectionName": "oauth"},
    }
    decoder = Mock(return_value={"serviceurl": activity["serviceUrl"]})
    monkeypatch.setattr(identity.jwt, "decode", decoder)
    await identity.verify_connector("fixture", conn, activity)
    assert decoder.call_args.kwargs["algorithms"] == ["RS256"]
    assert decoder.call_args.kwargs["audience"] == "app"
    assert decoder.call_args.kwargs["issuer"] == "https://api.botframework.com"
    key._jwk_data = {}
    with pytest.raises(HTTPException) as denied:
        await identity.verify_connector("fixture", conn, activity)
    assert denied.value.status_code == 403
    decoder.return_value = {"tid": tenant, "oid": "actor", "scp": "access_as_user"}
    assert (await identity.verify_sso("fixture", conn, tenant, activity))["oid"] == "actor"
    assert decoder.call_args.kwargs["issuer"] == f"https://login.microsoftonline.com/{tenant}/v2.0"
    decoder.return_value = {"tid": tenant, "oid": "different-member", "scp": "access_as_user"}
    with pytest.raises(HTTPException) as denied:
        await identity.verify_sso("fixture", conn, tenant, activity)
    assert denied.value.status_code == 401


async def test_retried_personal_message_starts_once_in_the_authenticated_space(teams_store, monkeypatch):
    from unittest.mock import AsyncMock

    import httpx2

    app = FastAPI()
    app.include_router(teams.router)
    app.state.teams_store = teams_store
    app.state.user_store = SimpleNamespace(
        user_has_active_team_membership=AsyncMock(return_value=True),
        get_user=AsyncMock(return_value=SimpleNamespace(status="active")),
        get_team_role=AsyncMock(return_value="member"),
        get_member_role=AsyncMock(return_value="member"),
        get_workspace_team_id=AsyncMock(return_value="team_a"),
        get_workspace=AsyncMock(return_value={"status": "active"}),
        list_workspaces_for_user=AsyncMock(return_value=[{"workspace_id": "ws_a"}]),
    )
    app.state.orchestrator = SimpleNamespace(
        session_store=SimpleNamespace(get_session=AsyncMock(return_value=None), get_turn=AsyncMock(return_value=None))
    )
    await teams_store.bind_identity(tenant_id="tenant", oid="oid", user_id="usr_a", team_id="team_a")
    conn = SimpleNamespace(id="conn", team_id="team_a", entra_tenant_id="tenant", microsoft_app_id="app")
    monkeypatch.setattr(teams, "_authenticate_activity", AsyncMock(return_value=(conn, "tenant")))
    start = AsyncMock()
    monkeypatch.setattr(teams, "_start_turn", start)
    activity = {
        "id": "incoming",
        "type": "message",
        "text": "Compare revenue",
        "from": {"aadObjectId": "oid"},
        "conversation": {"id": "conv", "conversationType": "personal"},
    }
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="https://example.test") as client:
        assert (await client.post("/messages", json=activity)).status_code == 200
        assert (await client.post("/messages", json=activity)).status_code == 200
        assert start.await_count == 1
        fields = start.await_args.kwargs
        assert fields["workspace_id"] == "ws_a" and fields["user_id"] == "usr_a"
        assert fields["turn_id"] == "t_" + fields["activity_key"][:32]
        app.state.user_store.get_member_role.return_value = None
        activity["id"] = "following-message"
        assert (await client.post("/messages", json=activity)).status_code == 403
        assert start.await_count == 1


def test_personal_picker_includes_every_available_space():
    spaces = [{"workspace_id": f"ws_{i}", "name": f"Space {i}"} for i in range(15)]
    card = teams._workspace_picker_card(spaces, "en-US")
    field = card["body"][-1]
    assert field["id"] == "workspace_id"
    assert [choice["value"] for choice in field["choices"]] == [space["workspace_id"] for space in spaces]
    assert card["actions"][0]["verb"] == "bowhead.workspace.select"
