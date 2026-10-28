from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import aiosqlite
import pytest
from bowhead_core.legacy_theme_registry import legacy_m1_compiled_scheme
from bowhead_core.personal_appearance_service import (
    PersonalAppearanceBase,
    PersonalAppearanceBaseIncompatible,
    PersonalAppearanceCompileStale,
    PersonalAppearanceConflict,
    PersonalAppearanceService,
    PersonalAppearanceUndoExpired,
)
from bowhead_core.personal_appearance_store import PersonalAppearanceBaseGuard, PersonalAppearanceStore
from bowhead_core.personal_theme_models import (
    KEY_APPEARANCE_THEME_CONFIG_V3,
)
from bowhead_core.team_settings_store import TeamSettingsStore
from bowhead_core.theme_config import KEY_APPEARANCE_THEME_CONFIG
from bowhead_core.user_settings_store import UserSettingsStore


def _base(profile_id: str = "bowhead") -> PersonalAppearanceBase:
    compiled = legacy_m1_compiled_scheme(profile_id)
    return PersonalAppearanceBase(
        identity=f"legacy:{compiled['checksum']}",
        color_scheme={
            "tokenSchemaVersion": compiled["tokenSchemaVersion"],
            "light": compiled["light"],
            "dark": compiled["dark"],
            "checksum": compiled["checksum"],
        },
        effective={
            "mode": "system",
            "typographyProfile": {"type": "preset", "id": "bowhead"},
            "typeScale": "standard",
            "density": "comfortable",
            "radius": "balanced",
            "motion": "standard",
        },
        sources={
            "mode": "product",
            "typographyProfile": "product",
            "typeScale": "product",
            "density": "product",
            "radius": "product",
            "motion": "product",
            "colorScheme": "team_default",
        },
        team={"revisionId": None, "revisionNumber": 0},
        guard=PersonalAppearanceBaseGuard(
            team_id="team_1",
            team_policy_raw=None,
            active_revision_id=None,
            fallback_revision_id=None,
            selected_revision_id=None,
            selected_revision_checksum=compiled["checksum"],
        ),
    )


def _alphabetical_base() -> PersonalAppearanceBase:
    base = _base()
    base.color_scheme["light"] = dict(sorted(base.color_scheme["light"].items()))
    base.color_scheme["dark"] = dict(sorted(base.color_scheme["dark"].items()))
    return base


@pytest.fixture
async def service_ctx(tmp_path):
    db_path = tmp_path / "personal-service.db"
    generic = UserSettingsStore(db_path)
    await generic.init()
    async with aiosqlite.connect(db_path) as db:
        await db.executescript(
            """
            CREATE TABLE team_settings (
                team_id TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL,
                updated_at TEXT NOT NULL, updated_by TEXT,
                PRIMARY KEY (team_id, key)
            );
            CREATE TABLE team_branding_state (
                team_id TEXT PRIMARY KEY, active_revision_id TEXT NOT NULL,
                fallback_revision_id TEXT NOT NULL
            );
            CREATE TABLE team_branding_revisions (
                id TEXT PRIMARY KEY, team_id TEXT NOT NULL, checksum TEXT NOT NULL
            );
            """
        )
        await db.commit()
    store = PersonalAppearanceStore(db_path)
    await store.initialize()
    now = [datetime(2026, 9, 5, 14, 30, tzinfo=UTC)]
    service = PersonalAppearanceService(store, clock=lambda: now[0])
    return service, store, generic, now


@pytest.mark.asyncio
async def test_read_projects_legacy_without_materializing_or_chart(service_ctx) -> None:
    service, store, generic, _ = service_ctx
    await generic.set(
        "usr_1",
        KEY_APPEARANCE_THEME_CONFIG,
        json.dumps(
            {
                "version": 1,
                "mode": "dark",
                "colorProfile": {"type": "preset", "id": "ocean"},
                "chartPalette": "high-contrast",
            }
        ),
    )

    view = await service.get(user_id="usr_1", team_id="team_1", base=_base())

    assert view.payload["materialized"] is False
    assert view.payload["rowVersion"] == 0
    assert view.payload["overrides"] == {
        "mode": "dark",
        "accent": {"kind": "preset", "registryVersion": 1, "id": "ocean"},
    }
    assert "chartPalette" not in view.payload["overrides"]
    assert (await store.read("usr_1")).config_raw is None


@pytest.mark.asyncio
async def test_patch_materializes_projection_and_rejects_stale_etag(service_ctx) -> None:
    service, store, generic, _ = service_ctx
    await generic.set(
        "usr_1",
        KEY_APPEARANCE_THEME_CONFIG,
        '{"version":1,"mode":"dark","colorProfile":{"type":"preset","id":"ocean"}}',
    )
    before = await service.get(user_id="usr_1", team_id="team_1", base=_base())

    after = await service.patch(
        user_id="usr_1",
        team_id="team_1",
        base=_base(),
        if_match=before.etag,
        changes={"background": {"kind": "preset", "registryVersion": 1, "id": "pure-white"}},
    )

    assert after.payload["materialized"] is True
    assert after.payload["rowVersion"] == 1
    assert after.payload["overrides"]["mode"] == "dark"
    assert after.payload["overrides"]["accent"]["id"] == "ocean"
    assert after.payload["overrides"]["background"]["id"] == "pure-white"
    assert (await store.read("usr_1")).legacy_raw is not None
    with pytest.raises(PersonalAppearanceConflict):
        await service.patch(
            user_id="usr_1",
            team_id="team_1",
            base=_base(),
            if_match=before.etag,
            changes={"mode": "light"},
        )


@pytest.mark.asyncio
async def test_noop_patch_keeps_version_and_pending_undo(service_ctx) -> None:
    service, store, generic, _ = service_ctx
    await generic.set("usr_1", KEY_APPEARANCE_THEME_CONFIG_V3, '{"version":3,"rowVersion":2,"overrides":{}}')
    before = await service.get(user_id="usr_1", team_id="team_1", base=_base())
    reset = await service.reset(
        user_id="usr_1",
        team_id="team_1",
        base=_base(),
        if_match=before.etag,
        idempotency_key="reset-1",
    )

    after = await service.patch(
        user_id="usr_1",
        team_id="team_1",
        base=_base(),
        if_match=reset.resource.etag,
        changes={"mode": None},
    )

    assert after.payload["rowVersion"] == reset.resource.payload["rowVersion"]
    assert after.payload["pendingUndo"] == reset.resource.payload["pendingUndo"]
    assert (await store.read("usr_1")).undo_raw is not None


@pytest.mark.asyncio
async def test_reset_is_idempotent_and_undo_restores_materialized_state(service_ctx) -> None:
    service, store, generic, _ = service_ctx
    await generic.set(
        "usr_1",
        KEY_APPEARANCE_THEME_CONFIG_V3,
        '{"version":3,"rowVersion":4,"overrides":{"mode":"dark"}}',
    )
    before = await service.get(user_id="usr_1", team_id="team_1", base=_base())

    first = await service.reset(
        user_id="usr_1",
        team_id="team_1",
        base=_base(),
        if_match=before.etag,
        idempotency_key="stable-reset-key",
    )
    retry = await service.reset(
        user_id="usr_1",
        team_id="team_1",
        base=_base(),
        if_match=before.etag,
        idempotency_key="stable-reset-key",
    )

    assert retry.resource.etag == first.resource.etag
    assert retry.reset_id == first.reset_id
    restored = await service.undo(
        user_id="usr_1",
        team_id="team_1",
        base=_base(),
        if_match=first.resource.etag,
        reset_id=first.reset_id,
    )
    assert restored.payload["rowVersion"] == 6
    assert restored.payload["overrides"] == {"mode": "dark"}
    assert restored.payload["pendingUndo"] is None
    assert (await store.read("usr_1")).undo_raw is None


@pytest.mark.asyncio
async def test_expired_undo_is_deleted_without_changing_config(service_ctx) -> None:
    service, store, generic, now = service_ctx
    await generic.set(
        "usr_1",
        KEY_APPEARANCE_THEME_CONFIG_V3,
        '{"version":3,"rowVersion":1,"overrides":{"mode":"dark"}}',
    )
    before = await service.get(user_id="usr_1", team_id="team_1", base=_base())
    reset = await service.reset(
        user_id="usr_1",
        team_id="team_1",
        base=_base(),
        if_match=before.etag,
        idempotency_key="reset-expiry",
    )
    now[0] += timedelta(minutes=11)

    with pytest.raises(PersonalAppearanceUndoExpired):
        await service.undo(
            user_id="usr_1",
            team_id="team_1",
            base=_base(),
            if_match=reset.resource.etag,
            reset_id=reset.reset_id,
        )

    current = await store.read("usr_1")
    assert json.loads(current.config_raw or "null")["rowVersion"] == 2
    assert current.undo_raw is None


@pytest.mark.asyncio
async def test_corrupt_document_can_only_be_recovered_by_nonrestorable_reset(service_ctx) -> None:
    service, store, generic, _ = service_ctx
    await generic.set("usr_1", KEY_APPEARANCE_THEME_CONFIG_V3, "not-json")
    broken = await service.get(user_id="usr_1", team_id="team_1", base=_base())

    assert broken.payload["materialized"] is True
    assert broken.payload["overrides"] == {}
    assert broken.payload["degradations"] == [{"field": "document", "reason": "invalid_persisted_document"}]
    recovered = await service.reset(
        user_id="usr_1",
        team_id="team_1",
        base=_base(),
        if_match=broken.etag,
        idempotency_key="recover-corrupt",
    )
    assert recovered.reset_id is None
    assert recovered.resource.payload["rowVersion"] == 1
    receipt = json.loads((await store.read("usr_1")).undo_raw or "null")
    assert receipt["restorable"] is False


@pytest.mark.asyncio
async def test_changed_patch_consumes_previous_undo(service_ctx) -> None:
    service, store, generic, _ = service_ctx
    await generic.set(
        "usr_1",
        KEY_APPEARANCE_THEME_CONFIG_V3,
        '{"version":3,"rowVersion":1,"overrides":{"mode":"dark"}}',
    )
    before = await service.get(user_id="usr_1", team_id="team_1", base=_base())
    reset = await service.reset(
        user_id="usr_1",
        team_id="team_1",
        base=_base(),
        if_match=before.etag,
        idempotency_key="reset-consume",
    )

    changed = await service.patch(
        user_id="usr_1",
        team_id="team_1",
        base=_base(),
        if_match=reset.resource.etag,
        changes={"mode": "light"},
    )

    assert changed.payload["pendingUndo"] is None
    assert (await store.read("usr_1")).undo_raw is None


@pytest.mark.asyncio
async def test_patch_rejects_base_policy_change_before_atomic_write(service_ctx) -> None:
    service, store, _, _ = service_ctx
    before = await service.get(user_id="usr_1", team_id="team_1", base=_base())
    team_settings = TeamSettingsStore(store.db_path)
    await team_settings.set("team_1", "theme_policy", '{"version":1}', "usr_admin")

    with pytest.raises(PersonalAppearanceConflict):
        await service.patch(
            user_id="usr_1",
            team_id="team_1",
            base=_base(),
            if_match=before.etag,
            changes={"mode": "dark"},
        )

    assert (await store.read("usr_1")).config_raw is None


@pytest.mark.asyncio
async def test_compile_candidate_returns_server_swatches_and_rejects_stale_base(service_ctx) -> None:
    service, _, _, _ = service_ctx
    current = await service.get(user_id="usr_1", team_id="team_1", base=_base())

    candidate = await service.compile_candidate(
        user_id="usr_1",
        team_id="team_1",
        base=_base(),
        base_etag=current.etag,
        candidate={"accent": {"kind": "custom", "seed": "#2F6F84"}},
        candidate_id="picker-1",
    )

    assert candidate["candidateId"] == "picker-1"
    assert candidate["colorScheme"]["light"]["accent"].startswith("#")
    assert candidate["contextRecommendations"]["accent"]["items"][0]["swatch"]["light"]
    with pytest.raises(PersonalAppearanceCompileStale):
        await service.compile_candidate(
            user_id="usr_1",
            team_id="team_1",
            base=_base(),
            base_etag='"personal-v3:stale"',
            candidate={},
            candidate_id="picker-2",
        )


@pytest.mark.asyncio
async def test_palette_registry_compiles_coordinated_saas_choices(service_ctx) -> None:
    service, _, _, _ = service_ctx
    current = await service.get(user_id="usr_1", team_id="team_1", base=_base())

    palettes = current.payload["registries"]["palette"]["items"]
    assert [item["id"] for item in palettes] == [
        "saas-blue",
        "bowhead-editorial",
        "product-indigo",
        "collaboration-teal",
        "focus-violet",
        "neutral-graphite",
    ]
    assert all(item["availability"] == "available" for item in palettes)
    saas_blue = next(item for item in palettes if item["id"] == "saas-blue")
    assert saas_blue["swatch"]["light"]["secondary"] == "#F8FAFC"
    assert saas_blue["swatch"]["dark"]["secondary"] == "#0F172A"

    applied = await service.patch(
        user_id="usr_1",
        team_id="team_1",
        base=_base(),
        if_match=current.etag,
        changes=saas_blue["value"],
    )

    assert applied.payload["overrides"] == saas_blue["value"]
    assert applied.payload["effective"]["colorScheme"]["light"]["background"] == "#F8FAFC"
    assert applied.payload["effective"]["colorScheme"]["dark"]["background"] == "#0F172A"


@pytest.mark.asyncio
async def test_legacy_alphabetical_base_keeps_all_presets_and_custom_colors_available(
    service_ctx,
) -> None:
    service, _, _, _ = service_ctx
    base = _alphabetical_base()
    current = await service.get(user_id="usr_1", team_id="team_1", base=base)

    for registry in current.payload["registries"].values():
        assert all(item["availability"] == "available" for item in registry["items"])
    candidate = await service.compile_candidate(
        user_id="usr_1",
        team_id="team_1",
        base=base,
        base_etag=current.etag,
        candidate={
            "accent": {"kind": "custom", "seed": "#2F6F84"},
            "background": {
                "kind": "custom",
                "light": "#F7F9FC",
                "dark": "#111827",
            },
        },
        candidate_id="picker-legacy-order",
    )
    assert candidate["validation"]["blockingCount"] == 0
    assert candidate["colorScheme"]["light"]["background"] == "#F7F9FC"


@pytest.mark.asyncio
async def test_incompatible_base_is_not_reported_as_an_unsafe_candidate(service_ctx) -> None:
    service, _, _, _ = service_ctx
    invalid_base = _base()
    del invalid_base.color_scheme["light"]["focus"]
    current = await service.get(user_id="usr_1", team_id="team_1", base=invalid_base)

    assert {item["reason"] for item in current.payload["registries"]["accent"]["items"]} == {"base_incompatible"}
    with pytest.raises(PersonalAppearanceBaseIncompatible):
        await service.compile_candidate(
            user_id="usr_1",
            team_id="team_1",
            base=invalid_base,
            base_etag=current.etag,
            candidate={"accent": {"kind": "custom", "seed": "#2F6F84"}},
            candidate_id="picker-base-incompatible",
        )
