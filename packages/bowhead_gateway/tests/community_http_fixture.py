"""Real installation and availability setup for isolated HTTP contract tests."""

import pytest


@pytest.fixture
def complete_community_http_installation(monkeypatch):
    """Complete real first-owner setup for authenticated Community HTTP fixtures."""

    async def complete(store, *, app=None):
        return await initialize_community_http_installation(store, monkeypatch, app=app)

    return complete


async def initialize_community_http_installation(store, monkeypatch, *, app=None):
    """Initialize an explicitly fresh synthetic database through the original setup service."""
    from bowhead_core.installation import community_setup_token, initialize_installation, setup_community_owner
    from bowhead_gateway.edition_extensions import read_build_edition

    if read_build_edition() != "community":
        return None
    import os

    if not os.environ.get("BOWHEAD_JWT_SECRET"):
        monkeypatch.setenv("BOWHEAD_JWT_SECRET", "synthetic-community-http-contract-secret-32bytes")
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=")
    identity = await initialize_installation(str(store.db_path), "community", admitted_fresh=True)
    owner = await store.get_user_by_email("setup-owner@example.test") if identity.setup_complete else None
    if not identity.setup_complete:
        owner = await setup_community_owner(
            str(store.db_path),
            setup_token=community_setup_token(identity),
            team_name="HTTP Contract Setup",
            email="setup-owner@example.test",
            password="SyntheticSetupOwner7!",
        )
    if app is not None:
        from bowhead_core.entitlements import deployment_mode_from_env
        from bowhead_gateway.edition_extensions import initialize_entitlement_services

        mode = deployment_mode_from_env()
        app.state.deployment_mode = mode
        await initialize_entitlement_services(app, store, mode)
    return owner


async def seed_community_http_space(store, owner, workspace_id):
    """Create explicit synthetic Space membership for a completed setup owner."""
    from bowhead_core._db import open_shared_ctx
    from bowhead_core.user_store import _now

    now = _now()
    async with open_shared_ctx(store.db_path) as db:
        await db.execute(
            "INSERT INTO workspaces(id,team_id,name,name_key,status,revision,created_at,updated_at) VALUES (?,?,?,?,'active',1,?,?)",
            (workspace_id, owner.team_id, workspace_id, workspace_id, now, now),
        )
        await db.execute("INSERT INTO workspace_members(workspace_id,user_id,joined_at) VALUES (?,?,?)", (workspace_id, owner.id, now))
        await db.commit()


def community_http_headers(owner):
    """Authenticate the real setup owner without relying on development identity hints."""
    if owner is None:
        return {}
    import os
    import time

    import jwt

    now = int(time.time())
    token = jwt.encode(
        {"sub": owner.id, "team_id": owner.team_id, "iat": now, "exp": now + 3600}, os.environ["BOWHEAD_JWT_SECRET"], algorithm="HS256"
    )
    return {"Authorization": f"Bearer {token}"}


def bind_scripted_community_model_runtime(monkeypatch, router):
    """Bind an explicit scripted Team model in hand-built HTTP fixtures."""
    from bowhead_gateway import turn_service

    async def resolve(*_args, **_kwargs):
        return turn_service.TeamModelRuntimeResolution(router=router, result="team_safe_success", reason="resolved")

    monkeypatch.setattr(turn_service, "resolve_team_model_runtime", resolve)


async def prepare_gateway_fixture(db_path, monkeypatch):
    """Run normal installation preparation before real Gateway lifespan tests."""
    import asyncio
    import os
    import sys
    from pathlib import Path

    from bowhead_core.user_store import UserStore
    from bowhead_gateway.installation import prepare_installation

    if not os.environ.get("BOWHEAD_FERNET_KEY"):
        monkeypatch.setenv("BOWHEAD_FERNET_KEY", "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=")
    monkeypatch.setenv("BOWHEAD_PRIVACY_LEDGER_DB", str(db_path) + ".privacy")
    await initialize_community_http_installation(UserStore(str(db_path)), monkeypatch)
    await asyncio.to_thread(
        prepare_installation, db_url=f"sqlite+aiosqlite:///{db_path}", migration_command=str(Path(sys.executable).with_name("bowhead"))
    )


async def create_scripted_http_actor(store, monkeypatch):
    """Create real Team membership for scripted chat fixtures in either edition."""
    import os

    if not os.environ.get("BOWHEAD_JWT_SECRET"):
        monkeypatch.setenv("BOWHEAD_JWT_SECRET", "synthetic-scripted-http-contract-secret-32bytes")
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=")
    owner = await initialize_community_http_installation(store, monkeypatch)
    if owner is not None:
        return owner
    team = await store.create_team(name="Scripted HTTP Team")
    return await store.create_user(team_id=team.id, email="scripted-owner@example.test", email_verified=True)
