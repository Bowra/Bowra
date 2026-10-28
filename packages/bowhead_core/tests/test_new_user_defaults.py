"""New accounts use automatic language preferences without overwriting saved language preferences."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import aiosqlite
from bowhead_core.new_user_defaults import NEW_USER_OUTPUT_LANGUAGE, NEW_USER_UI_LANGUAGE, seed_new_user_settings
from bowhead_core.user_store import UserStore


class _FakeSettings:
    def __init__(self, existing: dict[str, str] | None = None) -> None:
        self.data: dict[str, str] = dict(existing or {})
        self.sets: list[tuple[str, str, str]] = []

    async def get(self, user_id: str, key: str) -> str | None:
        return self.data.get(f"{user_id}:{key}")

    async def set(self, user_id: str, key: str, value: str) -> None:
        self.sets.append((user_id, key, value))
        self.data[f"{user_id}:{key}"] = value


async def test_seeds_automatic_language_for_a_fresh_user() -> None:
    store = _FakeSettings()
    await seed_new_user_settings(store, "usr_new")
    assert store.data == {"usr_new:ui_language": "auto", "usr_new:output_language": "auto"}
    assert NEW_USER_OUTPUT_LANGUAGE == NEW_USER_UI_LANGUAGE == "auto"


async def test_does_not_overwrite_an_existing_value() -> None:
    """Existing account or invitation preferences survive repeated seeding."""
    store = _FakeSettings({"usr_x:ui_language": "zh-CN", "usr_x:output_language": "follow_input"})
    await seed_new_user_settings(store, "usr_x")
    assert store.sets == []


async def test_never_breaks_signup() -> None:
    """Unavailable preference storage does not prevent signup."""

    class _Boom:
        async def get(self, *a: Any, **k: Any) -> str | None:
            raise RuntimeError("down")

        async def set(self, *a: Any, **k: Any) -> None:
            raise RuntimeError("down")

    await seed_new_user_settings(_Boom(), "usr_y")
    await seed_new_user_settings(None, "usr_y")
    await seed_new_user_settings(_FakeSettings(), "")


def test_every_account_creation_route_seeds_defaults() -> None:
    """Every account-creation route uses the shared seeding function."""
    routes = Path(__file__).resolve().parents[2] / "bowhead_gateway" / "src" / "bowhead_gateway" / "routes"
    assert routes.is_dir(), routes
    creators, seeders = set(), set()
    for path in routes.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "store.create_user(" in text:
            creators.add(path.name)
        if "seed_new_user_settings" in text:
            seeders.add(path.name)
    assert creators, "No account-creation routes were found"
    missing = sorted(creators - seeders)
    assert not missing, f"Account-creation routes missing preference seeding: {missing}"


async def test_preserves_a_chinese_interface_selected_before_signup() -> None:
    store = _FakeSettings()
    await seed_new_user_settings(store, "usr_chinese", ui_language="zh-CN")
    assert store.data == {"usr_chinese:ui_language": "zh-CN", "usr_chinese:output_language": "auto"}


async def test_preserves_each_existing_preference_independently() -> None:
    store = _FakeSettings({"usr_partial:ui_language": "zh-CN"})
    await seed_new_user_settings(store, "usr_partial")
    assert store.data == {"usr_partial:ui_language": "zh-CN", "usr_partial:output_language": "auto"}


async def test_account_writer_persists_english_and_preserves_existing_locales(tmp_path: Path) -> None:
    store = UserStore(tmp_path / "language-defaults.db")
    await store.initialize()
    team = await store.create_team(name="Language defaults", max_seats=10)
    user = await store.create_user(team_id=team.id, email="new@example.com")
    loaded = await store.get_user(user.id)
    assert loaded is not None and loaded.locale == "en-US"
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute("UPDATE users SET locale='zh-CN' WHERE id=?", (user.id,))
        await db.commit()
    await store.initialize()
    existing = await store.get_user(user.id)
    assert existing is not None and existing.locale == "zh-CN"
