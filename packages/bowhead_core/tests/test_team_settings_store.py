from __future__ import annotations

import pytest
from bowhead_core.team_settings_store import TeamSettingsStore


@pytest.mark.asyncio
async def test_team_settings_store_crud_and_team_isolation(tmp_path) -> None:
    store = TeamSettingsStore(tmp_path / "team-settings.db")
    await store.init()

    await store.set("team_a", "theme_policy", '{"version":1}', updated_by="user_a")
    await store.set("team_b", "theme_policy", '{"version":1,"x":true}', updated_by="user_b")

    assert await store.get("team_a", "theme_policy") == '{"version":1}'
    assert await store.get("team_b", "theme_policy") == '{"version":1,"x":true}'
    assert await store.list_for_team("team_a") == {"theme_policy": '{"version":1}'}

    await store.delete("team_a", "theme_policy")

    assert await store.get("team_a", "theme_policy") is None
    assert await store.get("team_b", "theme_policy") == '{"version":1,"x":true}'


@pytest.mark.asyncio
async def test_team_settings_store_compare_and_set_rejects_stale_value(tmp_path) -> None:
    store = TeamSettingsStore(tmp_path / "team-settings-cas.db")
    await store.init()
    await store.set("team_a", "chart", "v1", updated_by="user_a")

    assert await store.compare_and_set(
        "team_a",
        "chart",
        expected_value="v1",
        value="v2",
        updated_by="user_b",
    )
    assert not await store.compare_and_set(
        "team_a",
        "chart",
        expected_value="v1",
        value="stale",
        updated_by="user_a",
    )
    assert await store.get("team_a", "chart") == "v2"
