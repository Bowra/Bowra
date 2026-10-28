"""Public branding resolves only from a trusted entry and the shared contract."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from bowhead_gateway.public_branding import resolve_public_branding
from bowhead_gateway.public_entry_context import PublicAccessContext


class _Resolver:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def resolve(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            payload={
                "schemaVersion": 2,
                "brand": {"applicationName": "Acme"},
            },
            revision_id="tbr_aaaaaaaaaaaaaaaaaaaaaaaa",
            revision_checksum=f"sha256:{'a' * 64}",
        )


def _context() -> PublicAccessContext:
    return PublicAccessContext(
        base_url="https://acme.example/bowhead",
        origin="https://acme.example",
        base_path="/bowhead",
        app_root="https://acme.example/bowhead/",
        match_source="entry_id",
        entry_id="pe_aaaaaaaaaaaaaaaaaaaaaaaa",
        team_id="team_acme",
    )


@pytest.mark.unit
async def test_public_render_uses_the_effective_theme_resolver() -> None:
    resolver = _Resolver()
    app = SimpleNamespace(
        state=SimpleNamespace(
            effective_theme_resolver=resolver,
            deployment_mode="saas",
        ),
    )

    payload = await resolve_public_branding(app, _context())

    assert payload is not None
    assert payload["brand"]["applicationName"] == "Acme"
    assert resolver.calls[0]["team_id"] == "team_acme"
    assert resolver.calls[0]["render_safe_mode"] is False


@pytest.mark.unit
async def test_public_render_requires_a_bound_verified_entry_context() -> None:
    resolver = _Resolver()
    app = SimpleNamespace(
        state=SimpleNamespace(
            effective_theme_resolver=resolver,
            deployment_mode="saas",
        ),
    )
    unbound = PublicAccessContext(
        base_url="https://shared.example",
        origin="https://shared.example",
        base_path="",
        app_root="https://shared.example/",
        match_source="legacy",
        entry_id=None,
        team_id=None,
    )

    assert await resolve_public_branding(app, unbound) is None
    assert resolver.calls == []
