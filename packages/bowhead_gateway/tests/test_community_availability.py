"""Community availability retains Team boundaries in both outbound URL modes."""

from types import SimpleNamespace

import pytest
from bowhead_core.entitlements import CommunityResolver
from bowhead_core.user_store import UserStore
from bowhead_gateway._entitlement_gate import (
    assert_feature_licensed,
    assert_not_readonly,
    feature_license_checker,
    gate_turn_http,
    is_feature_licensed_request,
)
from fastapi import HTTPException


@pytest.mark.parametrize("mode", ["local", "saas"])
async def test_missing_community_resolver_denies_every_shared_gate(mode):
    app = SimpleNamespace(state=SimpleNamespace(edition="community", deployment_mode=mode))
    request = SimpleNamespace(app=app)
    for operation in (
        lambda: gate_turn_http(request, "team_a"),
        lambda: assert_feature_licensed(request, "kb"),
        lambda: assert_not_readonly(request),
    ):
        with pytest.raises(HTTPException) as caught:
            await operation()
        assert caught.value.status_code == 503
        assert caught.value.detail["error"]["code"] == "entitlement_unavailable"
    assert not await is_feature_licensed_request(request, "kb")
    assert not await feature_license_checker(app, "mcp")("team_a")


@pytest.mark.parametrize("mode", ["local", "saas"])
@pytest.mark.parametrize("team_status", ["active", "suspended", "missing"])
async def test_community_http_gates_use_availability_without_purchase_states(tmp_path, monkeypatch, mode, team_status):
    store = UserStore(str(tmp_path / "availability.db"))
    await store.initialize()
    team = await store.create_team(name="Community")
    if team_status == "suspended":
        import aiosqlite

        async with aiosqlite.connect(store.db_path) as db:
            await db.execute("UPDATE teams SET status='suspended'")
            await db.commit()
    team_id = None if team_status == "missing" else team.id

    async def current_user(_request):
        return SimpleNamespace(id="", team_id=team_id)

    monkeypatch.setattr("bowhead_gateway.routes.auth._current_user", current_user)
    app = SimpleNamespace(
        state=SimpleNamespace(edition="community", deployment_mode=mode, entitlements=CommunityResolver(mode=mode, user_store=store))
    )
    request = SimpleNamespace(app=app)
    for operation in (lambda: gate_turn_http(request, team_id), lambda: assert_feature_licensed(request, "kb"), lambda: assert_not_readonly(request)):
        if team_status == "active":
            await operation()
        else:
            with pytest.raises(HTTPException) as caught:
                await operation()
            assert caught.value.detail["error"]["code"] == "suspended"
    assert await is_feature_licensed_request(request, "kb") is (team_status == "active")
