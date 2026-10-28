from __future__ import annotations

import asyncio
from pathlib import Path

import aiosqlite
import pytest
from bowhead_core.public_entries import (
    PublicEntryError,
    PublicEntryStore,
    normalize_public_base_url,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("HTTPS://Example.COM:443/app/", "https://example.com/app"),
        ("http://127.0.0.1:10010/", "http://127.0.0.1:10010"),
        ("https://[2001:db8::1]:443/a", "https://[2001:db8::1]/a"),
        ("https://xn--fsqu00a.xn--0zwm56d/bowhead", "https://xn--fsqu00a.xn--0zwm56d/bowhead"),
        ("https://example.com/caf%c3%a9", "https://example.com/caf%C3%A9"),
    ],
)
def test_normalize_public_base_url(raw: str, expected: str) -> None:
    assert normalize_public_base_url(raw).base_url == expected


@pytest.mark.parametrize(
    "raw",
    [
        "ftp://example.com/app",
        "https://user@example.com/app",
        "https://example.com/app?x=1",
        "https://example.com/app#x",
        "https://example.com//app",
        "https://example.com/a/../b",
        "https://example.com/a/%2e%2e/b",
        "https://example.com/a%2fb",
        "https://example.com/a\\b",
        "https://example.com/a b",
        "https://example.com/%zz",
        "https://example.com/%41",
        "https://example.com/</script>",
        "https://example.com./app",
        "https://例子.测试/app",
    ],
)
def test_normalize_public_base_url_rejects_ambiguous_input(raw: str) -> None:
    with pytest.raises(PublicEntryError):
        normalize_public_base_url(raw)


@pytest.fixture
async def store(tmp_path: Path) -> PublicEntryStore:
    result = PublicEntryStore(tmp_path / "public-entries.db")
    await result.initialize()
    return result


@pytest.mark.asyncio
async def test_first_verified_entry_becomes_default_and_token_is_single_use(store: PublicEntryStore) -> None:
    pending, token = await store.create_pending(
        team_id="team_a",
        base_url="https://a.example.com/bowhead",
        actor_id="usr_owner",
    )
    assert pending.status == "pending_verification"
    assert pending.is_default is False

    active = await store.verify_challenge(
        entry_id=pending.id,
        token=token,
    )
    assert active.status == "active"
    assert active.is_default is True

    with pytest.raises(PublicEntryError, match="not pending"):
        await store.verify_challenge(
            entry_id=pending.id,
            token=token,
        )


@pytest.mark.asyncio
async def test_reissued_challenge_invalidates_old_token(store: PublicEntryStore) -> None:
    pending, old_token = await store.create_pending(
        team_id="team_a",
        base_url="https://a.example.com",
        actor_id="usr_owner",
    )
    _, new_token = await store.reissue_challenge(
        team_id="team_a",
        entry_id=pending.id,
        actor_id="usr_owner",
    )
    with pytest.raises(PublicEntryError, match="invalid challenge"):
        await store.verify_challenge(
            entry_id=pending.id,
            token=old_token,
        )
    active = await store.verify_challenge(
        entry_id=pending.id,
        token=new_token,
    )
    assert active.status == "active"


@pytest.mark.asyncio
async def test_default_switch_and_disable_last_are_atomic(store: PublicEntryStore) -> None:
    first, first_token = await store.create_pending(
        team_id="team_a",
        base_url="https://a.example.com",
        actor_id="usr_owner",
    )
    await store.verify_challenge(
        entry_id=first.id,
        token=first_token,
    )
    second, second_token = await store.create_pending(
        team_id="team_a",
        base_url="https://b.example.com/app",
        actor_id="usr_owner",
    )
    await store.verify_challenge(
        entry_id=second.id,
        token=second_token,
    )

    await store.set_default(team_id="team_a", entry_id=second.id, actor_id="usr_owner")
    entries = await store.list_for_team("team_a")
    assert [entry.id for entry in entries if entry.is_default] == [second.id]

    with pytest.raises(PublicEntryError, match="new default"):
        await store.disable(
            team_id="team_a",
            entry_id=second.id,
            actor_id="usr_owner",
        )
    with pytest.raises(PublicEntryError, match="new default"):
        await store.disable(
            team_id="team_a",
            entry_id=second.id,
            actor_id="usr_owner",
            new_default_entry_id=second.id,
        )
    await store.disable(
        team_id="team_a",
        entry_id=second.id,
        actor_id="usr_owner",
        new_default_entry_id=first.id,
    )
    await store.disable(
        team_id="team_a",
        entry_id=first.id,
        actor_id="usr_owner",
        disable_last=True,
    )
    entries = await store.list_for_team("team_a")
    assert not [entry for entry in entries if entry.status == "active"]
    assert not [entry for entry in entries if entry.is_default]


@pytest.mark.asyncio
async def test_overlapping_active_paths_are_rejected(store: PublicEntryStore) -> None:
    first, first_token = await store.create_pending(
        team_id="team_a",
        base_url="https://example.com/app",
        actor_id="usr_owner",
    )
    await store.verify_challenge(
        entry_id=first.id,
        token=first_token,
    )
    nested, nested_token = await store.create_pending(
        team_id="team_a",
        base_url="https://example.com/app/admin",
        actor_id="usr_owner",
    )
    with pytest.raises(PublicEntryError, match="overlapping"):
        await store.verify_challenge(
            entry_id=nested.id,
            token=nested_token,
        )

    sibling, sibling_token = await store.create_pending(
        team_id="team_a",
        base_url="https://example.com/apple",
        actor_id="usr_owner",
    )
    verified = await store.verify_challenge(
        entry_id=sibling.id,
        token=sibling_token,
    )
    assert verified.status == "active"


@pytest.mark.asyncio
async def test_concurrent_first_verification_keeps_one_default(store: PublicEntryStore) -> None:
    first, first_token = await store.create_pending(
        team_id="team_a",
        base_url="https://a.example.com",
        actor_id="usr_owner",
    )
    second, second_token = await store.create_pending(
        team_id="team_a",
        base_url="https://b.example.com",
        actor_id="usr_owner",
    )
    await asyncio.gather(
        store.verify_challenge(
            entry_id=first.id,
            token=first_token,
        ),
        store.verify_challenge(
            entry_id=second.id,
            token=second_token,
        ),
    )
    entries = await store.list_for_team("team_a")
    assert len([entry for entry in entries if entry.status == "active"]) == 2
    assert len([entry for entry in entries if entry.is_default]) == 1


@pytest.mark.asyncio
async def test_legacy_migration_is_idempotent_for_every_team(store: PublicEntryStore) -> None:
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute("CREATE TABLE teams (id TEXT PRIMARY KEY)")
        await db.executemany("INSERT INTO teams(id) VALUES (?)", [("team_a",), ("team_b",)])
        await db.commit()
    first = await store.migrate_legacy("https://legacy.example.com/bowhead")
    second = await store.migrate_legacy("https://legacy.example.com/bowhead/")
    assert first == 2
    assert second == 0
    for team_id in ("team_a", "team_b"):
        entries = await store.list_for_team(team_id)
        assert len(entries) == 1
        assert entries[0].status == "active"
        assert entries[0].is_default is True
        assert entries[0].verification_method == "legacy_migration"


@pytest.mark.asyncio
async def test_bootstrap_active_is_idempotent(store: PublicEntryStore) -> None:
    first = await store.create_bootstrap_active(
        team_id="team_a",
        base_url="https://public.example/bowhead/",
        actor_id="owner_a",
    )
    second = await store.create_bootstrap_active(
        team_id="team_a",
        base_url="https://ignored.example",
        actor_id="owner_a",
    )

    assert first == second
    assert first.status == "active"
    assert first.is_default is True
    assert first.base_url == "https://public.example/bowhead"


@pytest.mark.asyncio
async def test_adopt_browser_entry_registers_alongside_a_stale_entry(
    store: PublicEntryStore,
) -> None:
    """The case create_bootstrap_active cannot serve: the team already has an entry.

    A private deployment whose registered address went stale is otherwise
    unreachable, and bootstrap refuses to touch a team that owns any row at all.
    """
    stale = await store.create_bootstrap_active(
        team_id="team_a",
        base_url="https://old-name.example/bowhead",
        actor_id="owner_a",
    )

    adopted = await store.adopt_browser_entry(
        team_id="team_a",
        base_url="https://new-name.example/bowhead",
        actor_id="owner_a",
    )

    assert adopted.id != stale.id
    assert adopted.status == "active"
    assert adopted.verification_method == "license_browser_bootstrap"
    # Outbound absolute URLs keep pointing wherever the operator already chose.
    assert adopted.is_default is False
    assert (await store.default_for_team("team_a")).id == stale.id


@pytest.mark.asyncio
async def test_adopt_browser_entry_becomes_default_when_team_has_none(
    store: PublicEntryStore,
) -> None:
    pending, _token = await store.create_pending(
        team_id="team_a",
        base_url="https://unverified.example/bowhead",
        actor_id="owner_a",
    )
    assert pending.status == "pending_verification"

    adopted = await store.adopt_browser_entry(
        team_id="team_a",
        base_url="https://new-name.example/bowhead",
        actor_id="owner_a",
    )

    assert adopted.is_default is True


@pytest.mark.asyncio
async def test_adopt_browser_entry_is_idempotent(store: PublicEntryStore) -> None:
    first = await store.adopt_browser_entry(
        team_id="team_a",
        base_url="https://new-name.example/bowhead/",
        actor_id="owner_a",
    )
    second = await store.adopt_browser_entry(
        team_id="team_a",
        base_url="https://new-name.example/bowhead",
        actor_id="owner_a",
    )

    assert first == second
    assert first.base_url == "https://new-name.example/bowhead"


@pytest.mark.asyncio
async def test_adopt_browser_entry_reactivates_a_non_active_row_for_the_same_url(
    store: PublicEntryStore,
) -> None:
    """`UNIQUE(team_id, base_url)` ignores status, so a stale row must be reused.

    An operator who already tried to register the right address — and left it
    stuck in pending_verification — is exactly who hits the lockout.
    """
    await store.create_bootstrap_active(
        team_id="team_a",
        base_url="https://old-name.example/bowhead",
        actor_id="owner_a",
    )
    pending, _token = await store.create_pending(
        team_id="team_a",
        base_url="https://new-name.example/bowhead",
        actor_id="owner_a",
    )

    adopted = await store.adopt_browser_entry(
        team_id="team_a",
        base_url="https://new-name.example/bowhead",
        actor_id="owner_a",
    )

    assert adopted.id == pending.id
    assert adopted.status == "active"
    assert await store.find_active_by_base_url("https://new-name.example/bowhead") == [adopted]


@pytest.mark.asyncio
async def test_adopt_browser_entry_refuses_overlapping_active_path(
    store: PublicEntryStore,
) -> None:
    """Two active entries sharing an origin and path would make matching ambiguous."""
    await store.create_bootstrap_active(
        team_id="team_a",
        base_url="https://same-host.example/bowhead",
        actor_id="owner_a",
    )

    with pytest.raises(PublicEntryError):
        await store.adopt_browser_entry(
            team_id="team_a",
            base_url="https://same-host.example/bowhead/nested",
            actor_id="owner_a",
        )
