from __future__ import annotations

import json

import aiosqlite
import pytest
from bowhead_core.personal_appearance_store import (
    PersonalAppearanceSchemaUnavailableError,
    PersonalAppearanceStore,
)
from bowhead_core.personal_theme_models import (
    KEY_APPEARANCE_THEME_CONFIG_V3,
    KEY_APPEARANCE_THEME_UNDO_V3,
)
from bowhead_core.theme_config import KEY_APPEARANCE_THEME_CONFIG
from bowhead_core.user_settings_store import UserSettingsStore


@pytest.mark.asyncio
async def test_store_initialization_verifies_schema_without_creating_it(tmp_path) -> None:
    db_path = tmp_path / "missing.db"
    store = PersonalAppearanceStore(db_path)

    with pytest.raises(PersonalAppearanceSchemaUnavailableError):
        await store.initialize()

    async with aiosqlite.connect(db_path) as db:
        rows = await db.execute_fetchall("SELECT name FROM sqlite_master WHERE type='table' AND name='user_settings'")
    assert rows == []


@pytest.mark.asyncio
async def test_store_reads_only_personal_rows_in_one_snapshot(tmp_path) -> None:
    db_path = tmp_path / "personal.db"
    generic = UserSettingsStore(db_path)
    await generic.init()
    await generic.set("usr_1", KEY_APPEARANCE_THEME_CONFIG, '{"version":1,"mode":"dark"}')
    await generic.set(
        "usr_1",
        KEY_APPEARANCE_THEME_CONFIG_V3,
        '{"version":3,"rowVersion":2,"overrides":{}}',
    )
    await generic.set("usr_1", KEY_APPEARANCE_THEME_UNDO_V3, '{"version":1}')
    await generic.set("usr_1", "ui_language", "zh-CN")
    store = PersonalAppearanceStore(db_path)
    await store.initialize()

    rows = await store.read("usr_1")

    assert rows.legacy_raw == '{"version":1,"mode":"dark"}'
    assert rows.config_raw == '{"version":3,"rowVersion":2,"overrides":{}}'
    assert rows.undo_raw == '{"version":1}'
    assert rows.config_updated_at is not None
    assert store.db_path == str(db_path)


@pytest.mark.asyncio
async def test_compare_and_write_is_atomic_and_rejects_stale_rows(tmp_path) -> None:
    db_path = tmp_path / "personal.db"
    generic = UserSettingsStore(db_path)
    await generic.init()
    legacy = '{"version":1,"mode":"dark"}'
    await generic.set("usr_1", KEY_APPEARANCE_THEME_CONFIG, legacy)
    store = PersonalAppearanceStore(db_path)
    await store.initialize()
    before = await store.read("usr_1")
    first_config = '{"version":3,"rowVersion":1,"overrides":{"mode":"light"}}'
    receipt = json.dumps({"version": 1}, separators=(",", ":"))

    committed = await store.compare_and_write(
        user_id="usr_1",
        expected=before,
        config_raw=first_config,
        undo_raw=receipt,
    )
    stale_commit = await store.compare_and_write(
        user_id="usr_1",
        expected=before,
        config_raw='{"version":3,"rowVersion":1,"overrides":{}}',
        undo_raw=None,
    )

    assert committed is True
    assert stale_commit is False
    current = await store.read("usr_1")
    assert current.config_raw == first_config
    assert current.undo_raw == receipt
    assert current.legacy_raw == legacy


@pytest.mark.asyncio
async def test_compare_and_write_can_delete_receipt_with_config_update(tmp_path) -> None:
    db_path = tmp_path / "personal.db"
    generic = UserSettingsStore(db_path)
    await generic.init()
    await generic.set("usr_1", KEY_APPEARANCE_THEME_UNDO_V3, "receipt")
    store = PersonalAppearanceStore(db_path)
    await store.initialize()
    before = await store.read("usr_1")

    committed = await store.compare_and_write(
        user_id="usr_1",
        expected=before,
        config_raw='{"version":3,"rowVersion":1,"overrides":{}}',
        undo_raw=None,
    )

    assert committed is True
    current = await store.read("usr_1")
    assert current.config_raw == '{"version":3,"rowVersion":1,"overrides":{}}'
    assert current.undo_raw is None
