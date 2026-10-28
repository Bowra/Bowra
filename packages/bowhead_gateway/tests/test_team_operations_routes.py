"""Operations data requires active Team administration."""

from __future__ import annotations

from types import SimpleNamespace

import httpx2
import pytest
from bowhead_gateway.routes import team_operations
from fastapi import FastAPI

pytestmark = pytest.mark.integration


@pytest.mark.anyio
async def test_role_gate_precedes_report_reads(monkeypatch):
    app = FastAPI()
    app.include_router(team_operations.router, prefix="/api/v1/team-operations")
    actor = SimpleNamespace(id="usr_reader", team_id="team_one")

    class Store:
        db_path = "/unused"
        allowed = False

        async def is_team_admin(self, user_id, team_id):
            assert (user_id, team_id) == ("usr_reader", "team_one")
            return self.allowed

    store = Store()
    app.dependency_overrides[team_operations._current_user] = lambda: actor
    app.dependency_overrides[team_operations._store] = lambda: store
    calls = []

    async def report(db_path, *, team_id, days):
        calls.append((db_path, team_id, days))
        return {"timezone": "Asia/Shanghai", "days": []}

    monkeypatch.setattr(team_operations, "get_team_operations_report", report)
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/api/v1/team-operations")).status_code == 403
        store.allowed = True
        assert (await client.get("/api/v1/team-operations?days=2")).status_code == 400
        assert (await client.put("/api/v1/team-operations/timezone", json={"timezone": "Invalid/Zone"})).status_code == 400
        response = await client.get("/api/v1/team-operations?days=30")
        assert response.status_code == 200
        assert response.json() == {"timezone": "Asia/Shanghai", "days": []}
        assert calls == [("/unused", "team_one", 30)]
