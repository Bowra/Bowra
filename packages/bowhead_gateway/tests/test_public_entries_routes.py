from __future__ import annotations

from types import SimpleNamespace

import pytest
from bowhead_core.public_entries import PublicEntryStore
from bowhead_gateway.public_entry_context import (
    PublicAccessContext,
    reset_public_access_context,
    set_public_access_context,
)
from bowhead_gateway.routes.public_entries import (
    PublicEntryDisable,
    PublicEntryVerify,
    disable_public_entry,
    list_public_entries,
    public_entry_challenge_page,
    verify_public_entry,
)
from starlette.requests import Request


def _request(store: PublicEntryStore, path: str) -> Request:
    app = SimpleNamespace(state=SimpleNamespace(public_entry_store=store))
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": path,
            "headers": [(b"origin", b"https://public.example")],
            "client": ("10.0.0.10", 12345),
            "app": app,
        },
    )


@pytest.mark.asyncio
async def test_challenge_page_keeps_token_in_fragment_and_activates_entry_without_context(tmp_path) -> None:
    store = PublicEntryStore(tmp_path / "entry.db")
    await store.initialize()
    entry, challenge_token = await store.create_pending(
        team_id="team_a",
        base_url="https://public.example/bowhead",
        actor_id="owner_a",
    )
    request = _request(
        store,
        f"/.well-known/bowhead-public-entry/{entry.id}",
    )
    page = await public_entry_challenge_page(entry.id, request)

    assert challenge_token.encode() not in page.body
    assert page.headers["cache-control"] == "no-store"
    assert "default-src 'none'" in page.headers["content-security-policy"]

    verified = await verify_public_entry(
        entry.id,
        PublicEntryVerify(token=challenge_token),
        request,
    )
    assert verified.status == "active"
    assert verified.is_default is True


@pytest.mark.asyncio
async def test_challenge_page_is_available_without_context(tmp_path) -> None:
    store = PublicEntryStore(tmp_path / "entry.db")
    await store.initialize()
    entry, _token = await store.create_pending(
        team_id="team_a",
        base_url="http://10.10.0.5:10010",
        actor_id="owner_a",
    )

    page = await public_entry_challenge_page(
        entry.id,
        _request(store, f"/.well-known/bowhead-public-entry/{entry.id}"),
    )

    assert page.status_code == 200


@pytest.mark.asyncio
async def test_entry_list_only_describes_public_entry_lifecycle(tmp_path) -> None:
    store = PublicEntryStore(tmp_path / "entry.db")
    await store.initialize()
    entry = await store.create_bootstrap_active(
        team_id="team_a",
        base_url="https://public.example/bowhead",
        actor_id="owner_a",
    )

    context_token = set_public_access_context(
        PublicAccessContext(
            base_url=entry.base_url,
            origin="https://public.example",
            base_path=entry.base_path,
            app_root=entry.app_root,
            match_source="cookie",
            entry_id=entry.id,
            team_id="team_a",
        ),
    )
    try:
        entries = await list_public_entries(
            _request(store, "/api/v1/team-settings/public-entries"),
            user=SimpleNamespace(team_id="team_a"),
        )

        assert len(entries) == 1
        assert entries[0].is_current is True
        assert entries[0].match_source == "cookie"
    finally:
        reset_public_access_context(context_token)


@pytest.mark.asyncio
async def test_disable_route_accepts_explicit_final_entry_confirmation(tmp_path) -> None:
    store = PublicEntryStore(tmp_path / "entry.db")
    await store.initialize()
    entry = await store.create_bootstrap_active(
        team_id="team_a",
        base_url="https://public.example/bowhead",
        actor_id="owner_a",
    )

    result = await disable_public_entry(
        entry.id,
        PublicEntryDisable(disable_last=True),
        _request(store, f"/api/v1/team-settings/public-entries/{entry.id}/disable"),
        user=SimpleNamespace(id="owner_a", team_id="team_a"),
    )

    assert result.status == "disabled"
    assert result.is_default is False
