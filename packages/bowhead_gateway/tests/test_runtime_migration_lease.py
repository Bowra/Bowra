"""Gateway owns a shared migration lease through startup and shutdown."""

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from bowhead_core.sqlite_upgrade import SQLiteUpgradeError, migration_lock
from fastapi import FastAPI

from bowhead_gateway import main


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, "startup", "shutdown"])
async def test_lifespan_migration_lease_covers_failures_and_shutdown(tmp_path, monkeypatch, failure):
    monkeypatch.setattr(main, "get_settings", lambda: SimpleNamespace(db_url=f"sqlite+aiosqlite:///{tmp_path / 'bowhead.db'}"))
    observed = []

    @asynccontextmanager
    async def inner(app):
        with pytest.raises(SQLiteUpgradeError), migration_lock(tmp_path):
            pytest.fail("migration acquired during startup")
        observed.append("startup")
        if failure == "startup":
            raise RuntimeError("startup failed")
        try:
            yield
        finally:
            with pytest.raises(SQLiteUpgradeError), migration_lock(tmp_path):
                pytest.fail("migration acquired during shutdown")
            observed.append("shutdown")
            if failure == "shutdown":
                raise RuntimeError("shutdown failed")

    monkeypatch.setattr(main, "_application_lifespan", inner)
    if failure:
        with pytest.raises(RuntimeError, match=failure):
            async with main.lifespan(FastAPI()):
                pass
    else:
        async with main.lifespan(FastAPI()):
            with pytest.raises(SQLiteUpgradeError), migration_lock(tmp_path):
                pytest.fail("migration acquired during serving")
    assert observed == (["startup"] if failure == "startup" else ["startup", "shutdown"])
    with migration_lock(tmp_path):
        pass
