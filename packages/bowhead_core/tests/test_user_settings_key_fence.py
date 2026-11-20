"""Personal preference saves do not wait for running Turns; privacy and access keys stay fenced.

116, 1.7.59: saving the knowledge-base scope filter repeated 68 times over 9.7 s because every
``user_settings`` write took the exclusive core lease while running Turns held its shared side.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path

import anyio
import pytest
from bowhead_core._db import SQLiteStoreBusy
from bowhead_core.resource_disable import KEY_BUILTIN_PLUGINS_DISABLED, KEY_TEAM_PLUGINS_DISABLED
from bowhead_core.sqlite_domain_runtime import connect_sync
from bowhead_core.sqlite_domains import domain_database_path
from bowhead_core.sqlite_mutation_guards import _FENCED_KEYS
from bowhead_core.user_settings_store import UserSettingsStore
from bowhead_memory.candidates import SETTINGS_KEY_DREAMING_PAUSED
from test_sqlite_domain_stores import domain_directory as domain_directory

from bowhead_core import _db

pytestmark = pytest.mark.integration


@contextmanager
def _running_turn(core: Path) -> Iterator[None]:
    """A Turn's write-capable execution transaction that has read core, so it holds core's shared lease."""
    with closing(connect_sync(domain_database_path(core, "execution"))) as conn:
        conn.execute("BEGIN")
        with closing(conn.execute("SELECT status FROM workspaces WHERE id='ws_1'")) as cursor:
            assert cursor.fetchone() == ("active",)
        assert conn._leases.get("core") == 1, conn._leases
        try:
            yield
        finally:
            conn.rollback()


def test_the_fenced_keys_are_the_privacy_and_access_settings() -> None:
    assert set(_FENCED_KEYS["user_settings"][1]) == {SETTINGS_KEY_DREAMING_PAUSED, KEY_BUILTIN_PLUGINS_DISABLED, KEY_TEAM_PLUGINS_DISABLED}


@pytest.mark.asyncio
async def test_an_interface_preference_saves_while_a_turn_holds_the_core_lease(domain_directory: Path) -> None:
    core = domain_directory / "bowhead.db"
    store = UserSettingsStore(core)
    with _running_turn(core):
        # Before 0231 each of these repeated for the full 30 s and answered store_busy.
        with anyio.fail_after(5):
            await store.set("u_1", "kb_file_scope", "team")
            await store.set("u_1", "kb_file_scope", "personal")
            await store.update_value("u_1", "answer_style", lambda _before: "concise")
            await store.delete("u_1", "answer_style")
    assert await store.get("u_1", "kb_file_scope") == "personal"
    assert await store.get("u_1", "answer_style") is None


@pytest.mark.asyncio
async def test_a_privacy_setting_still_waits_for_the_core_lease(domain_directory: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    core = domain_directory / "bowhead.db"
    store = UserSettingsStore(core)
    monkeypatch.setattr(_db, "AUTHORITY_BUSY_WAIT_SECONDS", 0.3)
    with _running_turn(core):
        for key, value in ((SETTINGS_KEY_DREAMING_PAUSED, "true"), (KEY_TEAM_PLUGINS_DISABLED, '["plugin"]')):
            with pytest.raises(SQLiteStoreBusy):
                await store.set("u_1", key, value)
    await store.set("u_1", SETTINGS_KEY_DREAMING_PAUSED, "true")
    assert await store.get("u_1", SETTINGS_KEY_DREAMING_PAUSED) == "true"
