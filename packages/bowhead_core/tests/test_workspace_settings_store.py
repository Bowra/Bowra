"""WorkspaceSettingsStore 单测."""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

import pytest
from bowhead_core.workspace_settings_store import (
    KEY_DEFAULT_DATA_LANGUAGE,
    KEY_DEFAULT_UI_LANGUAGE,
    KEY_I18N_V1_ENABLED,
    KEY_WEEK_START_DAY,
    WorkspaceSettingsStore,
)

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
async def store(tmp_path: Path) -> WorkspaceSettingsStore:
    s = WorkspaceSettingsStore(tmp_path / "ws_settings.db")
    await s.init()
    return s


@pytest.mark.asyncio
class TestBasic:
    async def test_set_and_get(self, store: WorkspaceSettingsStore) -> None:
        await store.set("ws42", KEY_DEFAULT_UI_LANGUAGE, "en-US")
        assert await store.get("ws42", KEY_DEFAULT_UI_LANGUAGE) == "en-US"

    async def test_get_missing_returns_none(self, store: WorkspaceSettingsStore) -> None:
        assert await store.get("ws42", KEY_DEFAULT_UI_LANGUAGE) is None

    async def test_set_overwrites(self, store: WorkspaceSettingsStore) -> None:
        await store.set("ws42", KEY_WEEK_START_DAY, "monday")
        await store.set("ws42", KEY_WEEK_START_DAY, "sunday")
        assert await store.get("ws42", KEY_WEEK_START_DAY) == "sunday"

    async def test_delete(self, store: WorkspaceSettingsStore) -> None:
        await store.set("ws42", KEY_I18N_V1_ENABLED, "true")
        await store.delete("ws42", KEY_I18N_V1_ENABLED)
        assert await store.get("ws42", KEY_I18N_V1_ENABLED) is None

    async def test_delete_missing_no_op(self, store: WorkspaceSettingsStore) -> None:
        await store.delete("ws42", "nonexistent")  # Does not raise.

    async def test_workspace_isolation(self, store: WorkspaceSettingsStore) -> None:
        await store.set("ws42", KEY_DEFAULT_UI_LANGUAGE, "zh-CN")
        await store.set("ws43", KEY_DEFAULT_UI_LANGUAGE, "en-US")
        assert await store.get("ws42", KEY_DEFAULT_UI_LANGUAGE) == "zh-CN"
        assert await store.get("ws43", KEY_DEFAULT_UI_LANGUAGE) == "en-US"


@pytest.mark.asyncio
class TestListForWorkspace:
    async def test_list_returns_all_keys(self, store: WorkspaceSettingsStore) -> None:
        await store.set("ws42", KEY_DEFAULT_UI_LANGUAGE, "en-US")
        await store.set("ws42", KEY_DEFAULT_DATA_LANGUAGE, "zh-CN")
        await store.set("ws42", KEY_WEEK_START_DAY, "monday")
        result = await store.list_for_workspace("ws42")
        assert result == {
            KEY_DEFAULT_UI_LANGUAGE: "en-US",
            KEY_DEFAULT_DATA_LANGUAGE: "zh-CN",
            KEY_WEEK_START_DAY: "monday",
        }

    async def test_list_workspace_isolation(self, store: WorkspaceSettingsStore) -> None:
        await store.set("ws42", KEY_DEFAULT_UI_LANGUAGE, "en-US")
        await store.set("ws43", KEY_WEEK_START_DAY, "sunday")
        ws42 = await store.list_for_workspace("ws42")
        ws43 = await store.list_for_workspace("ws43")
        assert ws42 == {KEY_DEFAULT_UI_LANGUAGE: "en-US"}
        assert ws43 == {KEY_WEEK_START_DAY: "sunday"}

    async def test_list_empty_workspace(self, store: WorkspaceSettingsStore) -> None:
        assert await store.list_for_workspace("ws_empty") == {}


@pytest.mark.unit
async def test_compare_and_apply_rejects_a_changed_field_without_partial_write(store):
    await store.set("ws42", KEY_DEFAULT_DATA_LANGUAGE, "de-DE")
    result = await store.compare_and_apply("ws42", {KEY_WEEK_START_DAY: (None, "sunday"), KEY_DEFAULT_DATA_LANGUAGE: (None, "en-US")})
    assert result is None
    assert await store.get("ws42", KEY_WEEK_START_DAY) is None
    assert await store.get("ws42", KEY_DEFAULT_DATA_LANGUAGE) == "de-DE"


@pytest.mark.unit
async def test_compare_and_apply_rolls_back_a_database_write_failure(store):
    with sqlite3.connect(store._path) as connection:
        connection.execute(
            "CREATE TRIGGER fail_data_language BEFORE INSERT ON workspace_settings WHEN NEW.key='default_data_language' BEGIN SELECT RAISE(ABORT, 'test write failure'); END"
        )
    with pytest.raises(sqlite3.IntegrityError):
        await store.compare_and_apply("ws42", {KEY_WEEK_START_DAY: (None, "sunday"), KEY_DEFAULT_DATA_LANGUAGE: (None, "en-US")})
    assert await store.list_for_workspace("ws42") == {}


@pytest.mark.unit
async def test_compare_and_apply_retries_known_effect_and_restores_unset_values(store):
    changes = {KEY_WEEK_START_DAY: (None, "sunday"), KEY_DEFAULT_DATA_LANGUAGE: (None, "en-US")}
    assert await store.compare_and_apply("ws42", changes) == {KEY_WEEK_START_DAY: None, KEY_DEFAULT_DATA_LANGUAGE: None}
    assert await store.compare_and_apply("ws42", changes) == {KEY_WEEK_START_DAY: "sunday", KEY_DEFAULT_DATA_LANGUAGE: "en-US"}
    undo = {key: (desired, previous) for key, (previous, desired) in changes.items()}
    assert await store.compare_and_apply("ws42", undo) is not None
    assert await store.list_for_workspace("ws42") == {}


@pytest.mark.asyncio
class TestIdempotentInit:
    async def test_init_twice_no_error(self, tmp_path: Path) -> None:
        s = WorkspaceSettingsStore(tmp_path / "x.db")
        await s.init()
        await s.init()  # Second call is idempotent.
        await s.set("ws1", KEY_I18N_V1_ENABLED, "true")
        assert await s.get("ws1", KEY_I18N_V1_ENABLED) == "true"
