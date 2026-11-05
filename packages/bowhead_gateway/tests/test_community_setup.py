"""Community setup APIs and admission run without commercial activation."""

from __future__ import annotations

import gc
import json
import shutil
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest
from bowhead_core.entitlements import CommunityResolver
from bowhead_core.installation import community_setup_token, initialize_installation, read_installation
from bowhead_core.public_entries import PublicEntryStore
from bowhead_core.user_store import UserStore
from bowhead_gateway.installation import CommunitySetupMiddleware
from bowhead_gateway.public_entry_context import PublicEntryContextMiddleware
from bowhead_gateway.routes import auth, bootstrap
from fastapi import FastAPI
from fastapi.testclient import TestClient
from gateway_legacy_database import upgrade


@pytest.fixture(scope="module")
def template(tmp_path_factory):
    path = tmp_path_factory.mktemp("community-gateway") / "bowhead.db"
    upgrade(f"sqlite:///{(path).with_name('bowhead.db')}", target="core")
    return path


@pytest.fixture
async def app_ctx(tmp_path, template, monkeypatch):
    path = tmp_path / "bowhead.db"
    with sqlite3.connect(template) as source, sqlite3.connect(path) as destination:
        source.backup(destination)
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=")
    monkeypatch.setenv("BOWHEAD_JWT_SECRET", "community-setup-test-secret-at-least-32-bytes")
    monkeypatch.setenv("BOWHEAD_AUTH_MODE", "prod")
    monkeypatch.setenv("BOWHEAD_DEV_USER_FALLBACK", "0")
    identity = await initialize_installation(str(path), "community", admitted_fresh=True)
    store = UserStore(str(path))
    app = FastAPI()
    app.state.edition = "community"
    app.state.deployment_mode = "local"
    app.state.user_store = store
    app.state.entitlements = CommunityResolver(mode="local", user_store=store)
    store.set_seat_guard(app.state.entitlements)
    app.state.public_entry_store = PublicEntryStore(str(path))
    app.add_middleware(CommunitySetupMiddleware)
    app.add_middleware(PublicEntryContextMiddleware)
    app.include_router(bootstrap.router)
    app.include_router(auth.router, prefix="/api/v1/auth")

    @app.get("/activate/")
    async def activate_page():
        return {"page": "setup"}

    @app.post("/api/v1/probe-write")
    async def probe_write():
        return {"reachable": True}

    auth._RATE_BUCKET.clear()
    return app, store, community_setup_token(identity)


def _body(token):
    return {"setup_token": token, "team_name": "Example", "email": "owner@example.com", "password": "StrongPassword7"}


async def test_pending_setup_blocks_other_account_paths_and_allows_normal_login_after_claim(app_ctx):
    app, store, token = app_ctx
    client = TestClient(app)
    assert client.get("/activate/").status_code == 200
    status = client.get("/api/v1/bootstrap/status")
    assert status.json() == {"edition": "community", "required": True, "deployment_mode": "local", "setup_token_required": True}
    assert token not in status.text
    assert client.post("/api/v1/auth/register", json={"email": "attacker@example.com", "password": "StrongPassword7"}).status_code == 503
    assert client.post("/api/v1/probe-write").status_code == 503
    assert client.post("/api/v1/bootstrap/community", json=_body("wrong")).status_code == 403
    assert not read_installation(store.db_path).setup_complete
    created = client.post("/api/v1/bootstrap/community", json=_body(token))
    assert created.status_code == 201, created.text
    assert created.json()["max_seats"] is None
    assert created.json()["next_route"] == "/login"
    assert "bowhead_jwt" not in created.cookies
    assert client.post("/api/v1/probe-write").status_code == 200
    assert client.get("/api/v1/bootstrap/status").json()["required"] is False
    assert client.post("/api/v1/bootstrap/community", json=_body(token)).status_code == 409
    login = client.post("/api/v1/auth/login", json={"email": "owner@example.com", "password": "StrongPassword7"})
    assert login.status_code == 200, login.text
    assert "bowhead_jwt" in login.cookies


async def test_pending_guard_observes_completion_from_another_worker(app_ctx):
    app, store, token = app_ctx
    first = TestClient(app)
    other = FastAPI()
    other.state.edition = "community"
    other.state.user_store = store
    other.add_middleware(CommunitySetupMiddleware)

    @other.post("/api/probe")
    async def probe():
        return {"ok": True}

    second = TestClient(other)
    assert second.post("/api/probe").status_code == 503
    assert first.post("/api/v1/bootstrap/community", json=_body(token)).status_code == 201
    assert second.post("/api/probe").status_code == 200


async def test_missing_runtime_provenance_never_grants_api_access(app_ctx):
    app, store, _ = app_ctx
    with sqlite3.connect(store.db_path) as db:
        db.execute("DELETE FROM installation_identity")
    client = TestClient(app)
    assert client.get("/api/v1/bootstrap/status").status_code == 503
    assert client.post("/api/v1/probe-write").status_code == 503


def test_prepare_admits_before_migration_and_restarts_pending_instance(tmp_path, monkeypatch):
    import sys

    from bowhead_gateway import installation

    monkeypatch.setenv("BOWHEAD_FERNET_KEY", "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=")
    monkeypatch.setattr(installation, "read_build_edition", lambda: "community")
    path = tmp_path / "fresh" / "bowhead.db"
    command = str(Path(sys.executable).with_name("bowhead"))
    installation.prepare_installation(db_url=f"sqlite:///{path}", migration_command=command)
    identity = read_installation(str(path))
    assert identity.current_edition == "community" and not identity.setup_complete
    installation.prepare_installation(db_url=f"sqlite:///{path}", migration_command=command)
    assert read_installation(str(path)) == identity
    with sqlite3.connect(path) as db:
        db.execute("UPDATE installation_identity SET current_edition='business'")
    before = path.read_bytes()
    with pytest.raises(ValueError, match="Business-to-Community"):
        installation.prepare_installation(db_url=f"sqlite:///{path}", migration_command="must-never-run")
    assert path.read_bytes() == before


def test_preparation_rejects_unknown_data_before_creating_admission_lock(tmp_path, monkeypatch):
    from bowhead_gateway import installation

    monkeypatch.setattr(installation, "read_build_edition", lambda: "community")
    path = tmp_path / "bowhead.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE historical_record(id TEXT)")
        db.execute("INSERT INTO historical_record VALUES('keep')")
    before = path.read_bytes()
    with pytest.raises(ValueError, match="unknown installation provenance"):
        installation.prepare_installation(db_url=f"sqlite:///{path}", migration_command="must-never-run")
    assert path.read_bytes() == before
    assert not (tmp_path / ".installation-prepare.lock").exists()


def test_prepare_recovers_killed_core_transaction_before_provenance_and_migrates_normally(tmp_path, monkeypatch):
    from bowhead_core.sqlite_domains import DOMAIN_TARGET_FILES
    from bowhead_core.sqlite_upgrade import SQLiteUpgradeError, database_head, runtime_shared_lease

    from bowhead_gateway import installation

    monkeypatch.setattr(installation, "read_build_edition", lambda: "business")
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=")
    path = tmp_path / "sqlite" / "bowhead.db"
    url = f"sqlite:///{path}"
    upgrade(url)
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO _phase0_probe(k,ts) VALUES('retained-before-crash',1.0)")
    subprocess.run(
        [
            sys.executable,
            "-c",
            "import os,sqlite3,sys; db=sqlite3.connect(sys.argv[1]); "
            "db.execute('PRAGMA cache_size=1'); db.execute('BEGIN IMMEDIATE'); "
            "db.execute('CREATE TABLE interrupted_startup(value BLOB)'); "
            "db.execute('INSERT INTO interrupted_startup VALUES(zeroblob(100000))'); os._exit(0)",
            str(path),
        ],
        check=True,
    )
    journal = path.with_name(path.name + "-journal")
    assert journal.is_file()
    command = str(Path(sys.executable).with_name("bowhead"))
    with runtime_shared_lease(path.parent), pytest.raises(SQLiteUpgradeError, match="already owns this data directory"):
        installation.prepare_installation(db_url=url, migration_command=command)
    assert journal.is_file()
    installation.prepare_installation(db_url=url, migration_command=command)
    assert read_installation(str(path)).current_edition == "business"
    current_head = json.loads((path.parent / "sqlite-layout.json").read_text())["target_head"]
    assert all(database_head(path.parent / filename) == current_head for filename in DOMAIN_TARGET_FILES.values())
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT ts FROM _phase0_probe WHERE k='retained-before-crash'").fetchone() == (1.0,)
        assert db.execute("SELECT 1 FROM sqlite_schema WHERE name='interrupted_startup'").fetchone() is None


@pytest.fixture(scope="module")
def stopped_wal_upgrade_source(tmp_path_factory, template):
    directory = tmp_path_factory.mktemp("stopped-wal-upgrade") / "sqlite"
    directory.mkdir()
    shutil.copy2(template, directory / "bowhead.db")
    upgrade(f"sqlite:///{directory / 'bowhead.db'}")
    return directory


@pytest.mark.parametrize("stage", ["initial", "vacuum_resume"])
def test_prepare_subprocess_finishes_existing_wal_migration_without_garbage_collection(tmp_path, monkeypatch, stopped_wal_upgrade_source, stage):
    from bowhead_core.sqlite_domain_upgrade import SPLIT_REVISION
    from bowhead_core.sqlite_domains import DOMAIN_TARGET_FILES
    from bowhead_core.sqlite_upgrade import LAYOUT_FILE, database_head

    from bowhead_cli import migrate
    from bowhead_gateway import installation

    monkeypatch.setattr(installation, "read_build_edition", lambda: "business")
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=")
    directory = tmp_path / "sqlite"
    shutil.copytree(stopped_wal_upgrade_source, directory)
    path = directory / "bowhead.db"
    url = f"sqlite:///{path}"
    checkpoint = None
    if stage == "vacuum_resume":
        original = migrate.run_domain_upgrade

        def stop_after_vacuum(*args, **kwargs):
            def fail(phase):
                if phase == "after_vacuum":
                    raise RuntimeError("stopped before vacuum checkpoint")

            return original(*args, **kwargs, fault=fail)

        with pytest.MonkeyPatch.context() as interruption:
            interruption.setattr(migrate, "run_domain_upgrade", stop_after_vacuum)
            with pytest.raises(RuntimeError, match="before vacuum checkpoint"):
                migrate.upgrade(url)
        checkpoint = json.loads((directory / LAYOUT_FILE).read_text())
        assert checkpoint["phase"] == "vacuum"
        assert all(database_head(directory / filename) == SPLIT_REVISION for filename in DOMAIN_TARGET_FILES.values())
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        db.execute("INSERT INTO _phase0_probe(k,ts) VALUES('retained-wal-source',1.0)")
        db.commit()
    gc.collect()
    enabled = gc.isenabled()
    gc.disable()
    try:
        installation.prepare_installation(db_url=url, migration_command=str(Path(sys.executable).with_name("bowhead")))
        assert read_installation(str(path)).current_edition == "business"
    finally:
        if enabled:
            gc.enable()
        gc.collect()
    complete = json.loads((directory / LAYOUT_FILE).read_text())
    assert complete["status"] == "ready" and complete["phase"] == "complete"
    assert all(database_head(directory / filename) == complete["target_head"] for filename in DOMAIN_TARGET_FILES.values())
    if checkpoint is not None:
        assert Path(checkpoint["backup"]).is_dir()
        append_backup = Path(complete["backup"])
        assert json.loads((append_backup / "backup-manifest.json").read_text())["heads"] == dict.fromkeys(DOMAIN_TARGET_FILES, SPLIT_REVISION)
        assert json.loads((append_backup / LAYOUT_FILE).read_text())["backup"] == checkpoint["backup"]
        for domain in ("execution", "content", "notifications", "observability"):
            assert complete["targets"][domain]["evidence"] == checkpoint["targets"][domain]["evidence"]
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("SELECT ts FROM _phase0_probe WHERE k='retained-wal-source'").fetchone() == (1.0,)


@pytest.mark.parametrize("prefix", ["", "/bowhead"])
async def test_setup_admission_uses_normalized_application_path(app_ctx, prefix):
    app, _, token = app_ctx
    outer = FastAPI()
    outer.mount(prefix or "/", app)
    client = TestClient(outer)
    assert client.get(prefix + "/activate/").status_code == 200
    assert client.get(prefix + "/api/v1/bootstrap/status").json()["required"] is True
    assert client.post(prefix + "/api/v1/probe-write").status_code == 503
    assert client.post(prefix + "/api/v1/bootstrap/community", json=_body(token)).status_code == 201
    assert client.post(prefix + "/api/v1/probe-write").status_code == 200


@pytest.mark.parametrize("invalid_field", ["setup_token", "password", "email"])
async def test_setup_validation_does_not_publish_proof_or_password(app_ctx, caplog, invalid_field):
    from bowhead_gateway.error_handler import register_error_handlers

    app, _, token = app_ctx
    register_error_handlers(app)
    body = _body(token)
    body[invalid_field] = {"setup_token": token * 4, "password": "pvt7", "email": "invalid-private-email"}[invalid_field]
    response = TestClient(app).post("/api/v1/bootstrap/community", json=body)
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "request_validation_failed"
    for sensitive in (token, body["password"], body["email"]):
        assert sensitive not in response.text
        assert sensitive not in caplog.text
    assert "input" not in response.json()["detail"]


async def test_local_setup_command_uses_persistent_root_from_dotenv(app_ctx, tmp_path, monkeypatch, capsys):
    from bowhead_gateway import installation

    _, store, token = app_ctx
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("BOWHEAD_FERNET_KEY")
    (tmp_path / ".env").write_text("BOWHEAD_FERNET_KEY=MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=\n")
    monkeypatch.setattr(installation, "read_build_edition", lambda: "community")
    monkeypatch.setattr("sys.argv", ["installation", "setup-token", "--db-url", f"sqlite:///{store.db_path}"])
    installation.main()
    assert capsys.readouterr().out.strip() == token
