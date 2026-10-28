from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from bowhead_core.public_entries import PublicEntry, PublicEntryStore
from bowhead_core.user_store import UserStore
from bowhead_gateway.identity_email import enqueue_identity_email
from bowhead_gateway.public_entry_context import (
    PublicAccessContext,
    reset_public_access_context,
    set_public_access_context,
)
from starlette.requests import Request


class _EntryStore:
    def __init__(self, entry: PublicEntry) -> None:
        self.entry = entry

    async def get_for_team(self, team_id: str, entry_id: str) -> PublicEntry | None:
        if team_id == self.entry.team_id and entry_id == self.entry.id:
            return self.entry
        return None

    async def default_for_team(self, team_id: str) -> PublicEntry | None:
        return self.entry if team_id == self.entry.team_id else None

    async def list_active(self) -> list[PublicEntry]:
        return [self.entry]


def _entry() -> PublicEntry:
    return PublicEntry(
        id="pe_aaaaaaaaaaaaaaaaaaaaaaaa",
        team_id="team_acme",
        base_url="https://public.example/bowhead",
        scheme="https",
        host="public.example",
        port=None,
        base_path="/bowhead",
        is_default=True,
        status="active",
        verification_method="manual",
        verification_expires_at=None,
        verified_at="2026-07-26T00:00:00+00:00",
        created_by="u_admin",
        updated_by="u_admin",
        created_at="2026-07-26T00:00:00+00:00",
        updated_at="2026-07-26T00:00:00+00:00",
    )


def _request(entry: PublicEntry) -> Request:
    app = SimpleNamespace(
        state=SimpleNamespace(public_entry_store=_EntryStore(entry)),
    )
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/",
            "headers": [],
            "app": app,
        },
    )


@pytest.mark.asyncio
async def test_identity_email_pins_entry_revision_and_multipart(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    entry = _entry()
    request = _request(entry)
    store = UserStore(tmp_path / "users.db")
    await store.initialize()

    async def branded(_app, context):  # type: ignore[no-untyped-def]
        assert context.entry_id == entry.id
        return SimpleNamespace(
            payload={
                "brand": {
                    "applicationName": "Acme Intelligence",
                    "logoOnLight": {
                        "kind": "asset",
                        "artifactId": "art_aaaaaaaaaaaaaaaaaaaaaaaa",
                        "path": "/api/v1/branding/assets/art_aaaaaaaaaaaaaaaaaaaaaaaa",
                    },
                },
                "effective": {
                    "colorScheme": {
                        "light": {"accent": "#3F6F8C", "accentInk": "#FFFFFF"},
                    },
                },
                "team": {"revisionId": "tbr_aaaaaaaaaaaaaaaaaaaaaaaa"},
            },
            revision_checksum=f"sha256:{'b' * 64}",
        )

    monkeypatch.setattr(
        "bowhead_gateway.identity_email.resolve_public_branding_bundle",
        branded,
    )
    rendered = await enqueue_identity_email(
        request,
        store,
        team_id=entry.team_id,
        to_email="member@example.com",
        purpose="verify",
        kind="verify",
        action_path="/api/v1/auth/verify-email?token=abc.def",
    )

    assert rendered.action_url == ("https://public.example/bowhead/api/v1/auth/verify-email?token=abc.def")
    rows = await store.claim_pending_smtp()
    assert len(rows) == 1
    row = rows[0]
    assert row["public_entry_id"] == entry.id
    assert row["brand_revision_id"] == "tbr_aaaaaaaaaaaaaaaaaaaaaaaa"
    assert "Acme Intelligence" in row["subject"]
    assert "Acme Intelligence" in row["body"]
    assert "multipart" not in row["body"]
    assert "https://public.example/bowhead/api/v1/branding/assets/" in row["html_body"]
    assert "background:#3F6F8C;color:#FFFFFF" in row["html_body"]
    assert json.loads(row["branding_snapshot"]) == {
        "accent": "#3F6F8C",
        "accent_ink": "#FFFFFF",
        "application_name": "Acme Intelligence",
        "brand_revision_checksum": f"sha256:{'b' * 64}",
        "brand_revision_id": "tbr_aaaaaaaaaaaaaaaaaaaaaaaa",
        "locale": "en-US",
        "logo_url": ("https://public.example/bowhead/api/v1/branding/assets/art_aaaaaaaaaaaaaaaaaaaaaaaa"),
        "public_base_url": "https://public.example/bowhead",
        "public_entry_id": entry.id,
        "recipe_version": "bowhead-identity-email-v1",
        "team_id": entry.team_id,
    }


@pytest.mark.asyncio
async def test_identity_email_ignores_context_bound_to_another_team(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    entry = _entry()
    request = _request(entry)
    store = UserStore(tmp_path / "users.db")
    await store.initialize()
    seen = []

    async def fallback(_app, context):  # type: ignore[no-untyped-def]
        seen.append(context)
        return

    monkeypatch.setattr(
        "bowhead_gateway.identity_email.resolve_public_branding_bundle",
        fallback,
    )
    token = set_public_access_context(
        PublicAccessContext(
            base_url="https://other.example",
            origin="https://other.example",
            base_path="",
            app_root="https://other.example/",
            match_source="entry_id",
            entry_id="pe_bbbbbbbbbbbbbbbbbbbbbbbb",
            team_id="team_other",
        ),
    )
    try:
        rendered = await enqueue_identity_email(
            request,
            store,
            team_id=entry.team_id,
            to_email="member@example.com",
            purpose="password_reset",
            kind="password_reset",
            action_path="/reset-password?flow=self_service&token=secret",
        )
    finally:
        reset_public_access_context(token)

    assert seen[0].entry_id == entry.id
    from bowhead_gateway.product_identity import product_name

    assert rendered.snapshot.application_name == product_name()
    assert rendered.snapshot.logo_url is None
    assert rendered.action_url.startswith(entry.base_url)
    assert "other.example" not in rendered.action_url


@pytest.mark.asyncio
async def test_identity_email_freezes_locale_and_rejects_unsafe_brand_colors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    entry = _entry()
    request = _request(entry)
    store = UserStore(tmp_path / "users.db")
    await store.initialize()

    async def branded(_app, _context):  # type: ignore[no-untyped-def]
        return SimpleNamespace(
            payload={
                "brand": {"applicationName": "Acme Intelligence"},
                "effective": {
                    "colorScheme": {
                        "light": {
                            "accent": "url(javascript:alert(1))",
                            "accentInk": "#fff",
                        },
                    },
                },
                "team": {"revisionId": "tbr_aaaaaaaaaaaaaaaaaaaaaaaa"},
            },
            revision_checksum=f"sha256:{'c' * 64}",
        )

    monkeypatch.setattr(
        "bowhead_gateway.identity_email.resolve_public_branding_bundle",
        branded,
    )
    rendered = await enqueue_identity_email(
        request,
        store,
        team_id=entry.team_id,
        to_email="member@example.com",
        purpose="verify",
        kind="verify",
        action_path="/api/v1/auth/verify-email?token=abc.def",
        locale="en-US",
    )

    assert rendered.snapshot.locale == "en-US"
    # Unsafe brand colors fall back to the product default light accent (SaaS Blue since 1.5.0).
    assert rendered.snapshot.accent == "#1754DB"
    assert rendered.snapshot.accent_ink == "#FFFFFF"
    assert "javascript:" not in rendered.html_body
    assert "background:#1754DB;color:#FFFFFF" in rendered.html_body


@pytest.mark.asyncio
async def test_identity_email_uses_request_context_when_team_has_no_verified_entry(tmp_path) -> None:
    user_store = UserStore(tmp_path / "new-team.db")
    await user_store.initialize()
    team = await user_store.create_team(name="New Team")
    entry_store = PublicEntryStore(user_store.db_path)
    await entry_store.initialize()
    app = SimpleNamespace(
        state=SimpleNamespace(
            public_entry_store=entry_store,
            user_store=user_store,
        ),
    )
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/register",
            "headers": [(b"host", b"attacker.invalid")],
            "app": app,
        },
    )

    context_token = set_public_access_context(
        PublicAccessContext(
            base_url="https://current.example/bowhead",
            origin="https://current.example",
            base_path="/bowhead",
            app_root="https://current.example/bowhead/",
            match_source="direct",
        ),
    )
    try:
        rendered = await enqueue_identity_email(
            request,
            user_store,
            team_id=team.id,
            to_email="owner@example.com",
            purpose="verify",
            kind="verify",
            action_path="/api/v1/auth/verify-email?token=abc",
        )
    finally:
        reset_public_access_context(context_token)

    assert rendered.action_url == "https://current.example/bowhead/api/v1/auth/verify-email?token=abc"
    rows = await user_store.claim_pending_smtp()
    assert len(rows) == 1
    assert rows[0]["public_entry_id"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(("edition", "name"), [("community", "Bowra"), ("business", "Bowhead")])
@pytest.mark.parametrize(
    ("kind", "english", "chinese"),
    [
        ("verify", "Verify your", "验证你的"),
        ("team_invite", "Invitation", "邀请加入"),
        ("password_reset", "Reset your", "重置"),
    ],
)
@pytest.mark.parametrize("locale", ["en-US", "zh-CN"])
async def test_identity_email_uses_edition_and_selected_language(monkeypatch, tmp_path, edition, name, kind, english, chinese, locale) -> None:
    from bowhead_gateway.identity_email import render_identity_email

    from bowhead_gateway import edition_extensions

    manifest = tmp_path / "build_manifest.json"
    manifest.write_text(json.dumps({"schema_version": 1, "edition": edition}))
    monkeypatch.setattr(edition_extensions, "BUILD_MANIFEST_PATH", manifest)

    async def fallback(_app, _context):
        return None

    monkeypatch.setattr("bowhead_gateway.identity_email.resolve_public_branding_bundle", fallback)
    request = _request(_entry())
    request.scope["headers"] = [(b"accept-language", locale.encode())]
    rendered = await render_identity_email(request, team_id=_entry().team_id, kind=kind, action_path="/action", team_name="Bowhead Customer")
    assert rendered.snapshot.application_name == name
    assert rendered.snapshot.locale == locale
    assert rendered.subject.startswith(chinese if locale == "zh-CN" else english)
    assert name in rendered.subject
    if kind == "team_invite":
        assert "Bowhead Customer" in rendered.text_body


@pytest.mark.asyncio
async def test_identity_email_matches_target_team_on_shared_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from bowhead_gateway.identity_email import render_identity_email
    from bowhead_gateway.public_entry_context import public_access_context_for_entry

    entries = PublicEntryStore(tmp_path / "entries.db")
    await entries.initialize()

    async def active(team: str, base: str) -> PublicEntry:
        pending, challenge = await entries.create_pending(team_id=team, base_url=base, actor_id="usr_owner")
        return await entries.verify_challenge(entry_id=pending.id, token=challenge)

    await active("team_target", "https://default.example/bowhead")
    current = await active("team_target", "https://shared.example:10010/ai/apps/bowhead")
    other = await active("team_other", current.base_url)

    async def no_brand(_app: object, _context: object) -> None:
        return None

    monkeypatch.setattr("bowhead_gateway.identity_email.resolve_public_branding_bundle", no_brand)
    request = Request(
        {"type": "http", "method": "POST", "path": "/", "headers": [], "app": SimpleNamespace(state=SimpleNamespace(public_entry_store=entries))}
    )
    token = set_public_access_context(public_access_context_for_entry(other))
    try:
        rendered = await render_identity_email(
            request, team_id="team_target", kind="password_reset", action_path="/reset-password?token=opaque", locale="en-US"
        )
    finally:
        reset_public_access_context(token)
    assert rendered.action_url == f"{current.base_url}/reset-password?token=opaque"
    assert rendered.snapshot.public_entry_id == current.id
    assert rendered.snapshot.team_id == "team_target"
