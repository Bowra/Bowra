"""Provenance, one-time owner claims, and edition availability boundaries."""

from __future__ import annotations

import asyncio
import sqlite3
from importlib import import_module
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from bowhead_core.entitlements import CommunityResolver, EntitlementDenied
from bowhead_core.installation import (
    InstallationError,
    community_setup_token,
    initialize_installation,
    preflight_installation,
    read_installation,
    setup_community_owner,
)
from bowhead_core.user_store import UserStore, verify_password

from bowhead_core import migrations


def _migration(path: Path, target: str = "core") -> Config:
    config = Config()
    config.set_main_option("script_location", str(Path(migrations.__file__).parent))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    config.attributes["bowhead_target"] = target
    return config


@pytest.fixture(scope="module")
def migrated_template(tmp_path_factory):
    path = tmp_path_factory.mktemp("community-template") / "bowhead.db"
    command.upgrade(_migration(path), "head")
    return path


@pytest.fixture
def database(tmp_path, migrated_template, monkeypatch):
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=")
    path = tmp_path / "bowhead.db"
    with sqlite3.connect(migrated_template) as source, sqlite3.connect(path) as destination:
        source.backup(destination)
    return str(path)


@pytest.mark.parametrize("target", ["core", "files", "doc_types", "extracted_records", "extracted_rows", "per_row_jobs", "team_templates", "matrix"])
def test_installation_migration_preserves_existing_data(tmp_path, target):
    path = tmp_path / "existing.db"
    config = _migration(path, target)
    revision = import_module("bowhead_core.migrations.versions.0134_installation_identity")
    command.upgrade(config, revision.down_revision)
    with sqlite3.connect(path) as db:
        before = [line for line in db.iterdump() if line.startswith("INSERT INTO") and '"alembic_version"' not in line]
    command.upgrade(config, revision.revision)
    command.upgrade(config, revision.revision)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (revision.revision,)
        assert [line for line in db.iterdump() if line.startswith("INSERT INTO") and '"alembic_version"' not in line] == before
        if target == "core":
            assert db.execute("SELECT COUNT(*) FROM installation_identity").fetchone() == (0,)
        else:
            assert db.execute("SELECT 1 FROM sqlite_master WHERE name='installation_identity'").fetchone() is None


def test_preflight_never_creates_or_adopts_an_unknown_database(tmp_path, database):
    missing = tmp_path / "absent" / "bowhead.db"
    assert preflight_installation(str(missing), "community") is True
    assert not missing.parent.exists()
    before = Path(database).read_bytes()
    with pytest.raises(InstallationError, match="unknown installation provenance"):
        preflight_installation(database, "community")
    assert Path(database).read_bytes() == before


async def _initialize(database):
    identity = await initialize_installation(database, "community", admitted_fresh=True)
    return community_setup_token(identity)


async def _claim(database, token, email="owner@example.com"):
    return await setup_community_owner(database, setup_token=token, team_name="Example", email=email, password="StrongPassword7")


async def test_concurrent_owner_claims_commit_exactly_once(database):
    token = await _initialize(database)
    results = await asyncio.gather(_claim(database, token), _claim(database, token, "racer@example.com"), return_exceptions=True)
    owners = [value for value in results if not isinstance(value, BaseException)]
    assert len(owners) == 1
    assert sum(isinstance(value, InstallationError) for value in results) == 1
    owner = owners[0]
    assert verify_password("StrongPassword7", owner.password_hash)
    store = UserStore(database)
    assert await store.is_team_admin(owner.id, owner.team_id)
    assert await store.get_team_creator_id(owner.team_id) == owner.id
    assert owner.credit_quota == 5000
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT COUNT(*) FROM users").fetchone() == (1,)
        assert db.execute("SELECT COUNT(*) FROM teams").fetchone() == (1,)
        assert db.execute("SELECT COUNT(*) FROM team_members").fetchone() == (1,)
        assert db.execute("SELECT COUNT(*) FROM team_license").fetchone() == (0,)
    assert read_installation(database).setup_complete
    with pytest.raises(InstallationError, match="not pending"):
        community_setup_token(read_installation(database))


@pytest.mark.parametrize("invalid_token", ["incorrect", "invalid-\u2603", "invalid-\ud800"], ids=["wrong-proof", "non-ascii", "surrogate"])
async def test_invalid_proof_does_not_claim_owner(database, invalid_token):
    token = await _initialize(database)
    with pytest.raises(PermissionError):
        await _claim(database, invalid_token)
    assert not read_installation(database).setup_complete
    await _claim(database, token)


async def test_community_has_unbounded_seats_but_preserves_team_and_budget_guards(database):
    owner = await _claim(database, await _initialize(database))
    store = UserStore(database)
    resolver = CommunityResolver(mode="local", user_store=store)
    store.set_seat_guard(resolver)
    snapshot = await resolver.get(owner.team_id)
    assert snapshot.max_seats is None
    assert snapshot.source == "community"
    await resolver.assert_can_add_seat(owner.team_id, active_count=10**100)
    for feature in ("kb", "mcp", "scheduling"):
        assert await resolver.is_feature_licensed(owner.team_id, feature)
    for feature in ("custom_branding", "audit_export", "unknown"):
        assert not await resolver.is_feature_licensed(owner.team_id, feature)
    for number in range(3):
        await store.create_user(team_id=owner.team_id, email=f"member{number}@example.com", password="StrongPassword7")
    assert await store.count_active_seats(owner.team_id) == 4
    assert (await store.get_team(owner.team_id)).team_credit_quota == 30000
    for missing in (None, "unknown"):
        with pytest.raises(EntitlementDenied):
            await resolver.assert_can_add_seat(missing, active_count=0)
    with sqlite3.connect(database) as db:
        db.execute("UPDATE teams SET status='suspended'")
    with pytest.raises(EntitlementDenied):
        await resolver.assert_can_run_turn(owner.team_id)


async def test_business_upgrade_preserves_owners_and_prevents_conversion_back(database):
    owner = await _claim(database, await _initialize(database))
    original = read_installation(database)
    assert preflight_installation(database, "business") is False
    business = await initialize_installation(database, "business")
    assert business.installation_id == original.installation_id
    assert business.origin_edition == "community"
    assert business.current_edition == "business"
    assert (await UserStore(database).get_user(owner.id)).email == owner.email
    before = Path(database).read_bytes()
    with pytest.raises(InstallationError, match="Business-to-Community"):
        preflight_installation(database, "community")
    assert Path(database).read_bytes() == before
    with pytest.raises(InstallationError):
        await _claim(database, "old-token", "new@example.com")


async def test_existing_business_database_receives_no_community_adoption(database):
    store = UserStore(database)
    team = await store.create_team(name="Existing Business")
    user = await store.create_user(team_id=team.id, email="existing@example.com", password="StrongPassword7")
    before = Path(database).read_bytes()
    with pytest.raises(InstallationError, match="unknown"):
        preflight_installation(database, "community")
    assert Path(database).read_bytes() == before
    identity = await initialize_installation(database, "business")
    assert identity.origin_edition == "business"
    assert (await store.get_user(user.id)).team_id == team.id


async def test_failed_owner_write_rolls_back_and_allows_valid_retry(database):
    token = await _initialize(database)
    with sqlite3.connect(database) as db:
        db.execute("CREATE TRIGGER reject_setup_member BEFORE INSERT ON team_members BEGIN SELECT RAISE(ABORT, 'simulated write failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match="simulated write failure"):
        await _claim(database, token)
    assert not read_installation(database).setup_complete
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT COUNT(*) FROM users").fetchone() == (0,)
        assert db.execute("SELECT COUNT(*) FROM teams").fetchone() == (0,)
        db.execute("DROP TRIGGER reject_setup_member")
    await _claim(database, token)


@pytest.mark.parametrize("core_state", ["missing", "empty", "empty_schema"])
def test_empty_core_does_not_adopt_existing_knowledge_database(tmp_path, core_state):
    core = tmp_path / "bowhead.db"
    if core_state == "empty":
        core.touch()
    elif core_state == "empty_schema":
        with sqlite3.connect(core) as db:
            db.execute("VACUUM")
    knowledge = tmp_path / "kb_files.db"
    with sqlite3.connect(knowledge) as db:
        db.execute("CREATE TABLE old_files(id TEXT)")
    with pytest.raises(InstallationError, match="unknown installation provenance"):
        preflight_installation(str(tmp_path / "bowhead.db"), "community")
    assert core.exists() is (core_state != "missing")
