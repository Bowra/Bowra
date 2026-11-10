"""Authenticated retirement and real secondary-storage cleanup boundaries."""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx2
import pytest
from alembic import command
from alembic.config import Config
from bowhead_core.account_deletion_service import AccountDeletionService
from bowhead_core.artifact_store import ArtifactStore, LocalBackend
from bowhead_core.session_lifecycle import SessionLifecycleCoordinator
from bowhead_core.session_store import SessionStore
from bowhead_core.sqlite_domain_upgrade import SOURCE_REVISION
from bowhead_core.terminal_protocol_store import TerminalProtocolStore
from bowhead_core.user_store import UserStore
from bowhead_gateway.account_deletion_cleanup import AccountExternalCleanup
from bowhead_gateway.main import create_app
from bowhead_gateway.routes.auth import _issue_jwt
from bowhead_gateway.settings import Settings

from bowhead_core import migrations

pytestmark = pytest.mark.integration
SECRET = "synthetic-account-deletion-jwt-secret"


@pytest.fixture(scope="session")
def account_gateway_schema(tmp_path_factory):
    path = tmp_path_factory.mktemp("account-gateway-schema") / "bowhead.db"
    config = Config()
    config.set_main_option("script_location", str(Path(migrations.__file__).resolve().parent))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    config.attributes["bowhead_target"] = "core"
    command.upgrade(config, SOURCE_REVISION)
    return path


@pytest.fixture
async def context(tmp_path, account_gateway_schema, monkeypatch, complete_community_http_installation):
    monkeypatch.setenv("BOWHEAD_JWT_SECRET", SECRET)
    monkeypatch.setenv("BOWHEAD_EMAIL_VERIFY_REQUIRED", "0")
    path = tmp_path / "bowhead.db"
    with sqlite3.connect(account_gateway_schema) as source, sqlite3.connect(path) as target:
        source.backup(target)
    users = UserStore(path)
    await complete_community_http_installation(users)
    team = await users.create_team(name="Team", domain=None)
    user = await users.create_user(team_id=team.id, email="member@example.com", password="Current-password-123!", email_verified=True)
    artifacts = ArtifactStore(db_path=path, backend=LocalBackend(tmp_path / "blobs"))
    service = AccountDeletionService(
        users,
        lifecycle=SessionLifecycleCoordinator(SessionStore(path), TerminalProtocolStore(str(path))),
        artifacts=artifacts,
        secret="synthetic-fernet-root",
        external_cleanup=AsyncMock(),
    )
    app = create_app()
    app.state.settings = Settings()
    app.state.user_store = users
    app.state.account_deletion_service = service
    return SimpleNamespace(app=app, users=users, user=user, service=service)


@pytest.mark.parametrize(
    "body,code",
    [
        ({"confirm_email": "wrong@example.com", "password": "Current-password-123!"}, "account_email_confirmation_required"),
        ({"confirm_email": "member@example.com", "password": "wrong"}, "account_password_confirmation_failed"),
    ],
)
async def test_http_confirmation_failure_preserves_identity(context, body, code):
    token = _issue_jwt(context.user, SECRET)
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=context.app), base_url="http://gw") as client:
        response = await client.request("DELETE", "/api/v1/profile", json=body, headers={"Authorization": f"Bearer {token}"})
    assert response.status_code in (403, 422)
    assert response.json()["detail"]["code"] == code
    assert (await context.users.get_user(context.user.id)).status == "active"


async def test_http_retirement_releases_email_and_revokes_original_token(context):
    token = _issue_jwt(context.user, SECRET)
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=context.app), base_url="http://gw") as client:
        response = await client.request(
            "DELETE",
            "/api/v1/profile",
            json={"confirm_email": context.user.email, "password": "Current-password-123!"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200, response.text
        result = response.json()
        assert datetime.fromisoformat(result["purge_after"]) - datetime.fromisoformat(result["requested_at"]) == timedelta(days=30)
        assert (await client.get("/api/v1/profile/me", headers={"Authorization": f"Bearer {token}"})).status_code in (401, 403)
    assert await context.users.get_user_by_email(context.user.email) is None
    new = await context.users.create_user(team_id=context.user.team_id, email=context.user.email, password="Fresh-password-123!")
    assert new.id != context.user.id


async def test_passwordless_refresh_is_not_reauthentication(context):
    with sqlite3.connect(context.users.db_path) as db:
        db.execute("UPDATE users SET password_hash=NULL WHERE id=?", (context.user.id,))
    user = await context.users.get_user(context.user.id)
    for fresh in (False, True):
        token = _issue_jwt(user, SECRET, auth_time=None if fresh else 0)
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=context.app), base_url="http://gw") as client:
            response = await client.request(
                "DELETE", "/api/v1/profile", json={"confirm_email": user.email}, headers={"Authorization": f"Bearer {token}"}
            )
        if not fresh:
            assert response.status_code == 403
            assert response.json()["detail"]["code"] == "account_recent_login_required"
        else:
            assert response.status_code == 200, response.text


async def test_instance_restore_uses_snapshot_account_state_and_preserves_history(tmp_path, monkeypatch):
    from bowhead_core.sqlite_domains import DOMAIN_TARGET_FILES
    from community_http_fixture import prepare_gateway_fixture
    from gateway_legacy_database import upgrade

    monkeypatch.chdir(tmp_path)
    data = tmp_path / "data"
    database_directory = data / "sqlite"
    db_path = database_directory / "bowhead.db"
    db_url = f"sqlite+aiosqlite:///{db_path}"
    monkeypatch.setenv("BOWHEAD_DB_URL", db_url)
    monkeypatch.setenv("BOWHEAD_JWT_SECRET", SECRET)
    monkeypatch.setenv("BOWHEAD_EMAIL_VERIFY_REQUIRED", "0")
    monkeypatch.setenv("BOWHEAD_DEPLOYMENT_MODE", "saas")
    monkeypatch.setenv("BOWHEAD_SANDBOX_BACKEND", "subprocess")
    monkeypatch.setenv("BOWHEAD_ASSEMBLY_ROUTER", "mock")
    upgrade(db_url)
    await prepare_gateway_fixture(db_path, monkeypatch)

    def copy_databases(source, destination):
        destination.mkdir(parents=True, exist_ok=True)
        for name in DOMAIN_TARGET_FILES.values():
            with closing(sqlite3.connect(source / name)) as reader, closing(sqlite3.connect(destination / name)) as writer:
                reader.backup(writer)

    def personal_memory():
        with closing(sqlite3.connect(db_path)) as db:
            return db.execute("SELECT value FROM memory_entries WHERE subject_type='user' AND subject_id=?", (user.id,)).fetchall()

    login = {"email": "restored@example.test", "password": "Restore-password-123!"}
    app = create_app()
    async with app.router.lifespan_context(app):
        users = app.state.user_store
        team = await users.create_team(name="Restore Test")
        user = await users.create_user(team_id=team.id, email_verified=True, **login)
        with closing(sqlite3.connect(db_path)) as db, db:
            db.execute(
                "INSERT INTO memory_entries(id,subject_type,subject_id,kind,key,value,locked,source,created_at,updated_at,version,"
                "memory_role,value_schema,memory_policy_version) VALUES "
                "('mem_restore','user',?,'preference','fixture','\"saved\"',0,'user_explicit','2026-01-01','2026-01-01',1,'product_memory','text',1)",
                (user.id,),
            )

    backups = data / "backups"
    before = backups / "before"
    after = backups / "after"
    copy_databases(database_directory, before)
    (backups / "operator-archive.tar").write_bytes(b"operator-managed archive")
    app = create_app()
    async with app.router.lifespan_context(app):
        service = app.state.account_deletion_service
        deletion = await service.request(user.id)
    copy_databases(database_directory, after)
    original_backups = {path.relative_to(backups): path.read_bytes() for path in backups.rglob("*") if path.is_file()}

    # A valid pre-upgrade receipt would retire the restored account under the old startup path.
    legacy = Path(os.environ["BOWHEAD_PRIVACY_LEDGER_DB"]).with_name("account-deletions.db")
    key = hmac.digest(os.environ["BOWHEAD_FERNET_KEY"].encode(), b"bowhead-account-deletion-v1", "sha256")
    subject = hmac.digest(key, user.id.encode(), "sha256").hex()
    signature = hmac.digest(key, json.dumps([subject, deletion.requested_at, deletion.purge_after]).encode(), "sha256").hex()
    with closing(sqlite3.connect(legacy)) as db, db:
        db.execute("CREATE TABLE account_retirements (subject TEXT PRIMARY KEY, requested_at TEXT, purge_after TEXT, signature TEXT)")
        db.execute("INSERT INTO account_retirements VALUES (?, ?, ?, ?)", (subject, deletion.requested_at, deletion.purge_after, signature))
    legacy_content = legacy.read_bytes()

    copy_databases(after, database_directory)
    app = create_app()
    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://gw") as client,
    ):
        response = await client.post("/api/v1/auth/login", json=login)
        assert response.status_code == 401, response.text
        service = app.state.account_deletion_service
        service.clock = lambda: datetime.fromisoformat(deletion.purge_after) + timedelta(days=1)
        async with asyncio.timeout(15):
            while True:
                with closing(sqlite3.connect(db_path)) as db:
                    state = db.execute("SELECT state FROM account_deletions WHERE operation_id=?", (deletion.operation_id,)).fetchone()
                if state == ("completed",):
                    break
                service.wake()
                await asyncio.sleep(0.1)
        assert personal_memory() == []
    assert {path.relative_to(backups): path.read_bytes() for path in backups.rglob("*") if path.is_file()} == original_backups

    copy_databases(before, database_directory)
    app = create_app()
    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://gw") as client,
    ):
        response = await client.post("/api/v1/auth/login", json=login)
        assert response.status_code == 200, response.text
        assert response.json()["user"]["id"] == user.id
        assert personal_memory() == [('"saved"',)]
    assert legacy.read_bytes() == legacy_content


async def test_personal_kb_bytes_extractions_and_all_index_versions_are_erased(context, tmp_path, monkeypatch):
    from bowhead_core.kb.store import KbStore
    from gateway_legacy_database import upgrade

    monkeypatch.chdir(tmp_path)
    upgrade(f"sqlite+aiosqlite:///{Path(context.users.db_path).with_name('bowhead.db')}")
    kb = KbStore(str(tmp_path / "chroma"))
    context.app.state.kb_store = kb
    cleanup = AccountExternalCleanup(context.app)
    context.service.external_cleanup = cleanup
    context.service.external_quarantine = cleanup.quarantine
    private = await context.service.artifacts.put_bytes(
        content=b"private", filename="private.txt", mime="text/plain", created_by=context.user.id, scope="user"
    )
    shared = await context.service.artifacts.put_bytes(
        content=b"shared", filename="shared.txt", mime="text/plain", created_by=context.user.id, scope="user"
    )
    with sqlite3.connect(tmp_path / "kb_files.db") as db:
        for file_id, artifact, scope in (("private_file", private, "personal"), ("shared_file", shared, "team")):
            db.execute(
                "INSERT INTO kb_files(file_id,artifact_id,filename,mime_type,size_bytes,scope,scope_id,workspace_id,team_id,uploaded_by,uploaded_at,parse_status) "
                "VALUES (?,?,?,'text/plain',7,?,?,'',?,?,'t','done')",
                (
                    file_id,
                    artifact.id,
                    artifact.filename,
                    scope,
                    context.user.id if scope == "personal" else context.user.team_id,
                    context.user.team_id,
                    context.user.id,
                ),
            )
    with sqlite3.connect(tmp_path / "kb_extracted_records.db") as db:
        for file_id in ("private_file", "shared_file"):
            db.execute(
                "INSERT INTO kb_extracted_records(record_id,doc_type_id,file_id,workspace_id,fields_json,extractor_model,extracted_at,last_modified_at,last_modified_by) "
                "VALUES (?,'type',?,'ws','{\"excerpt\":\"synthetic content\"}','synthetic','t','t','system')",
                (file_id, file_id),
            )
    for version in ("old", "current"):
        collection = kb._client.get_or_create_collection(f"kb_{version}_team_fixture")
        collection.add(
            ids=["private", "shared"],
            embeddings=[[0.1, 0.2], [0.2, 0.1]],
            documents=["private source", "shared source"],
            metadatas=[{"file_id": "private_file"}, {"file_id": "shared_file"}],
        )
    from bowhead_core.user_store import TeamSkill, TeamSubagent

    package_paths = {}
    for visibility in ("creator_only", "team_wide"):
        for slot in ("source", "candidate"):
            path = tmp_path / ".data" / "team_skills" / context.user.team_id / f"{visibility}_{slot}"
            path.mkdir(parents=True)
            (path / "SKILL.md").write_text("Synthetic private recipe" if visibility == "creator_only" else "Shared recipe")
            package_paths[(visibility, slot)] = path
        await context.users.create_team_skill(
            TeamSkill(
                id=f"skill_{visibility}",
                team_id=context.user.team_id,
                name=f"recipe_{visibility}",
                created_by=context.user.id,
                visibility=visibility,
                source_path=str(package_paths[(visibility, "source")]),
                candidate_path=str(package_paths[(visibility, "candidate")]),
            )
        )
        await context.users.create_team_subagent(
            TeamSubagent(
                id=f"agent_{visibility}",
                team_id=context.user.team_id,
                name=f"agent_{visibility}",
                created_by=context.user.id,
                visibility=visibility,
                status="disabled",
                system_prompt="Synthetic definition",
            )
        )
    now = datetime.now(UTC)
    context.service.clock = lambda: now - timedelta(days=31)
    await context.service.request(context.user.id)
    context.service.clock = lambda: now
    assert await context.service.run_once() == {"completed": 1, "failed": 0}
    assert not Path(private.path).exists()
    assert Path(shared.path).read_bytes() == b"shared"
    for filename, table in (("kb_files.db", "kb_files"), ("kb_extracted_records.db", "kb_extracted_records")):
        with sqlite3.connect(tmp_path / filename) as db:
            assert db.execute(f"SELECT file_id FROM {table}").fetchall() == [("shared_file",)]
    for version in ("old", "current"):
        collection = kb._client.get_collection(f"kb_{version}_team_fixture")
        assert collection.get()["ids"] == ["shared"]

    for (visibility, slot), path in package_paths.items():
        assert path.exists() == (visibility == "team_wide"), slot
    with sqlite3.connect(context.users.db_path) as db:
        assert db.execute("SELECT id FROM team_skill").fetchall() == [("skill_team_wide",)]
        assert db.execute("SELECT id FROM team_subagent").fetchall() == [("agent_team_wide",)]


async def test_enabled_shared_agent_requires_handoff_and_keeps_account_active(context):
    from bowhead_core.user_store import TeamSubagent

    await context.users.create_team_subagent(
        TeamSubagent(
            id="agent_shared_active",
            team_id=context.user.team_id,
            name="shared_active",
            created_by=context.user.id,
            visibility="team_wide",
            status="enabled",
        )
    )
    token = _issue_jwt(context.user, SECRET)
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=context.app), base_url="http://gw") as client:
        response = await client.request(
            "DELETE",
            "/api/v1/profile",
            json={
                "confirm_email": context.user.email,
                "password": "Current-password-123!",
            },
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 409
    assert response.json()["detail"] == {
        "code": "account_resource_transfer_required",
        "teams": [{"id": context.user.team_id, "name": "Team"}],
    }
    assert (await context.users.get_user_by_email(context.user.email)).status == "active"
    with sqlite3.connect(context.users.db_path) as db:
        assert db.execute("SELECT count(*) FROM account_deletions").fetchone() == (0,)


async def test_password_change_between_confirmation_and_retirement_requires_new_login(context, monkeypatch):
    from bowhead_core.user_store import hash_password

    original = context.service.request

    async def raced(user_id, **kwargs):
        await context.users.update_user_password(user_id, hash_password("Changed-password-123!"))
        return await original(user_id, **kwargs)

    monkeypatch.setattr(context.service, "request", raced)
    token = _issue_jwt(context.user, SECRET)
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=context.app), base_url="http://gw") as client:
        response = await client.request(
            "DELETE",
            "/api/v1/profile",
            json={
                "confirm_email": context.user.email,
                "password": "Current-password-123!",
            },
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "account_credentials_changed"
    assert (await context.users.get_user_by_email(context.user.email)).status == "active"


async def test_retired_multipart_office_upload_is_rejected_before_blob_io(context):
    from bowhead_gateway.routes.files_presign import confirm_multipart_office_upload

    await context.service.request(context.user.id)
    with pytest.raises(sqlite3.IntegrityError, match="account is deleted"):
        await confirm_multipart_office_upload(
            context.service.artifacts,
            content=b"Private Office bytes",
            filename="private.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            user_id=context.user.id,
            workspace_id="ws_1",
            session_id=None,
            authorize=AsyncMock(),
        )
    assert list(context.service.artifacts.backend.root_dir.rglob("*")) == []
