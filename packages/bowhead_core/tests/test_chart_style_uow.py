from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import aiosqlite
import pytest
from alembic import command
from alembic.config import Config
from bowhead_core.chart_style import KEY_TEAM_CHART_STYLE_POLICY
from bowhead_core.chart_style_models import ChartStyleDomainError
from bowhead_core.chart_style_uow import ChartStyleUnitOfWork
from bowhead_core.team_settings_store import TeamSettingsStore

from bowhead_core import migrations

pytestmark = pytest.mark.integration


def _config(path: Path) -> Config:
    config = Config()
    config.set_main_option("script_location", str(Path(migrations.__file__).resolve().parent))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    config.attributes["bowhead_target"] = "core"
    return config


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "chart-style-uow.db"
    command.upgrade(_config(path), "head")
    return path


def _source(name: str, *, palette: list[str] | None = None) -> dict:
    overrides: dict = {"schemaVersion": 2}
    if palette is not None:
        overrides["categoricalPalette"] = {"light": palette}
    return {"schemaVersion": 2, "name": name, "description": "", "overrides": overrides}


async def _create(uow: ChartStyleUnitOfWork, *, team: str, actor: str, name: str):
    return await uow.create(
        team_id=team,
        actor_id=actor,
        request_id=str(uuid4()),
        base_preset_id="clean-blue",
        source=_source(name),
    )


async def _publish(uow: ChartStyleUnitOfWork, *, team: str, actor: str, result):
    style_id = result.detail["style"]["id"]
    preview = await uow.preview(team_id=team, style_id=style_id, source=result.detail["draft"]["source"])
    return await uow.publish(
        team_id=team,
        actor_id=actor,
        style_id=style_id,
        if_match=result.detail["draftEtag"],
        acknowledged_validation_fingerprint=preview["validationFingerprint"],
    )


async def test_create_is_idempotent_and_draft_cas_is_strong(db_path: Path) -> None:
    uow = ChartStyleUnitOfWork(db_path)
    request_id = str(uuid4())
    args = {
        "team_id": "team_1",
        "actor_id": "user_1",
        "request_id": request_id,
        "base_preset_id": "clean-blue",
        "source": _source("Sales Style"),
    }
    created = await uow.create(**args)
    replay = await uow.create(**args)
    assert replay.detail["style"]["id"] == created.detail["style"]["id"]
    assert replay.response_etag == created.response_etag

    with pytest.raises(ChartStyleDomainError) as conflict:
        await uow.create(**{**args, "source": _source("Different Style")})
    assert conflict.value.code == "chart_style_idempotency_conflict"

    updated = await uow.replace_draft(
        team_id="team_1",
        actor_id="user_1",
        style_id=created.detail["style"]["id"],
        if_match=created.detail["draftEtag"],
        source=_source("Sales Style 2"),
    )
    assert updated.detail["draft"]["rowVersion"] == 2
    assert not updated.response_etag.startswith("W/")
    with pytest.raises(ChartStyleDomainError) as stale:
        await uow.replace_draft(
            team_id="team_1",
            actor_id="user_2",
            style_id=created.detail["style"]["id"],
            if_match=created.detail["draftEtag"],
            source=_source("Overwrite"),
        )
    assert stale.value.status_code == 412
    assert (await uow.detail(team_id="team_1", style_id=created.detail["style"]["id"]))["draft"]["source"]["name"] == "Sales Style 2"


async def test_publish_edit_restore_and_lifecycle_keep_immutable_history(db_path: Path) -> None:
    uow = ChartStyleUnitOfWork(db_path)
    created = await _create(uow, team="team_1", actor="user_1", name="Editorial One")
    published = await _publish(uow, team="team_1", actor="user_1", result=created)
    first_revision = published.detail["activeRevision"]
    assert published.registry_version == 1
    assert published.detail["state"] == "active_clean"
    assert published.detail["draft"] is None

    editing = await uow.create_draft(
        team_id="team_1",
        actor_id="user_2",
        style_id=published.detail["style"]["id"],
        if_match=published.detail["headEtag"],
    )
    changed = await uow.replace_draft(
        team_id="team_1",
        actor_id="user_2",
        style_id=published.detail["style"]["id"],
        if_match=editing.detail["draftEtag"],
        source=_source("Editorial Two"),
    )
    second = await _publish(uow, team="team_1", actor="user_2", result=changed)
    assert second.detail["activeRevision"]["sequence"] == 2
    assert second.detail["activeRevision"]["revisionChecksum"] != first_revision["revisionChecksum"]

    restored_preview = await uow.preview(
        team_id="team_1",
        style_id=second.detail["style"]["id"],
        source={
            "schemaVersion": 2,
            "name": first_revision["name"],
            "description": first_revision["description"],
            "overrides": first_revision["overrides"],
        },
    )
    restored = await uow.restore(
        team_id="team_1",
        actor_id="user_1",
        style_id=second.detail["style"]["id"],
        revision_id=first_revision["id"],
        if_match=second.detail["headEtag"],
        acknowledged_validation_fingerprint=restored_preview["validationFingerprint"],
    )
    assert restored.detail["activeRevision"]["sequence"] == 3
    assert restored.detail["activeRevision"]["operation"] == "restore"
    assert restored.detail["activeRevision"]["sourceRevisionId"] == first_revision["id"]
    history = await uow.store.list_revisions(
        team_id="team_1",
        style_id=second.detail["style"]["id"],
        limit=10,
        after=None,
    )
    assert [item["sequence"] for item in history["items"]] == [3, 2, 1]

    inactive = await uow.deactivate(
        team_id="team_1",
        actor_id="user_1",
        style_id=restored.detail["style"]["id"],
        if_match=restored.detail["headEtag"],
    )
    assert inactive.detail["state"] == "inactive_clean"
    assert inactive.detail["capabilities"]["restore"] == {"allowed": True, "reason": None}
    active = await uow.activate(
        team_id="team_1",
        actor_id="user_1",
        style_id=restored.detail["style"]["id"],
        if_match=inactive.detail["headEtag"],
    )
    assert active.detail["state"] == "active_clean"
    assert active.registry_version == 5


async def test_publish_conflict_rolls_back_revision_and_keeps_draft(db_path: Path) -> None:
    uow = ChartStyleUnitOfWork(db_path)
    first = await _publish(
        uow,
        team="team_1",
        actor="user_1",
        result=await _create(uow, team="team_1", actor="user_1", name="Same Name"),
    )
    second_created = await _create(uow, team="team_1", actor="user_2", name="Other Name")
    second = await _publish(uow, team="team_1", actor="user_2", result=second_created)
    editing = await uow.create_draft(
        team_id="team_1",
        actor_id="user_2",
        style_id=second.detail["style"]["id"],
        if_match=second.detail["headEtag"],
    )
    renamed = await uow.replace_draft(
        team_id="team_1",
        actor_id="user_2",
        style_id=second.detail["style"]["id"],
        if_match=editing.detail["draftEtag"],
        source=_source("Ｓａｍｅ   Name"),
    )
    preview = await uow.preview(
        team_id="team_1",
        style_id=second.detail["style"]["id"],
        source=renamed.detail["draft"]["source"],
    )
    with pytest.raises(ChartStyleDomainError) as conflict:
        await uow.publish(
            team_id="team_1",
            actor_id="user_2",
            style_id=second.detail["style"]["id"],
            if_match=renamed.detail["draftEtag"],
            acknowledged_validation_fingerprint=preview["validationFingerprint"],
        )
    assert conflict.value.code == "chart_style_name_conflict"
    detail = await uow.detail(team_id="team_1", style_id=second.detail["style"]["id"])
    assert detail["activeRevision"]["sequence"] == 1
    assert detail["draft"]["source"]["name"] == "Same Name"
    assert first.detail["activeRevision"]["sequence"] == 1


async def test_cross_team_is_not_found_and_draft_only_delete_uses_resource_etag(db_path: Path) -> None:
    uow = ChartStyleUnitOfWork(db_path)
    created = await _create(uow, team="team_1", actor="user_1", name="Private Style")
    style_id = created.detail["style"]["id"]

    with pytest.raises(ChartStyleDomainError) as hidden:
        await uow.detail(team_id="team_2", style_id=style_id)
    assert hidden.value.status_code == 404

    with pytest.raises(ChartStyleDomainError) as stale_discard:
        await uow.discard_draft(team_id="team_1", style_id=style_id, if_match='"stale"')
    assert stale_discard.value.status_code == 412

    with pytest.raises(ChartStyleDomainError) as stale:
        await uow.delete_draft_only(team_id="team_1", style_id=style_id, if_match=created.detail["draftEtag"])
    assert stale.value.status_code == 412
    await uow.delete_draft_only(team_id="team_1", style_id=style_id, if_match=created.detail["resourceEtag"])
    with pytest.raises(ChartStyleDomainError) as missing:
        await uow.detail(team_id="team_1", style_id=style_id)
    assert missing.value.status_code == 404


async def test_deactivate_policy_and_registry_changes_roll_back_together(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    uow = ChartStyleUnitOfWork(db_path)
    first = await _publish(
        uow,
        team="team_1",
        actor="user_1",
        result=await _create(uow, team="team_1", actor="user_1", name="Policy Style"),
    )
    style_id = first.detail["style"]["id"]
    settings = TeamSettingsStore(db_path)
    policy = {
        "version": 2,
        "defaultStyleId": "bowhead-editorial",
        "enabledStyleIds": ["bowhead-editorial", "clean-blue", style_id],
        "fallbackBuiltInPresetId": "bowhead-editorial",
    }
    await settings.set("team_1", KEY_TEAM_CHART_STYLE_POLICY, json.dumps(policy), "user_1")

    async def fail_registry(*args, **kwargs):
        del args, kwargs
        raise RuntimeError("injected registry failure")

    monkeypatch.setattr("bowhead_core.chart_style_uow._bump_registry", fail_registry)
    with pytest.raises(RuntimeError, match="injected"):
        await uow.deactivate(
            team_id="team_1",
            actor_id="user_1",
            style_id=style_id,
            if_match=first.detail["headEtag"],
        )

    detail = await uow.detail(team_id="team_1", style_id=style_id)
    assert detail["style"]["lifecycle"] == "active"
    assert json.loads(await settings.get("team_1", KEY_TEAM_CHART_STYLE_POLICY))["enabledStyleIds"][-1] == style_id
    async with (
        aiosqlite.connect(db_path) as db,
        db.execute("SELECT registry_version FROM team_chart_style_registry_state WHERE team_id='team_1'") as cursor,
    ):
        assert await cursor.fetchone() == (1,)


async def test_style_limit_counts_active_or_draft_identity_once(db_path: Path) -> None:
    uow = ChartStyleUnitOfWork(db_path)
    for index in range(12):
        await _create(uow, team="team_1", actor="user_1", name=f"Style {index:02d}")

    with pytest.raises(ChartStyleDomainError) as limit:
        await _create(uow, team="team_1", actor="user_1", name="Style 13")
    assert limit.value.code == "chart_style_limit_reached"


async def test_inactive_editing_style_can_reactivate_without_double_counting(db_path: Path) -> None:
    uow = ChartStyleUnitOfWork(db_path)
    published = await _publish(
        uow,
        team="team_1",
        actor="user_1",
        result=await _create(uow, team="team_1", actor="user_1", name="Editable Inactive"),
    )
    editing = await uow.create_draft(
        team_id="team_1",
        actor_id="user_1",
        style_id=published.detail["style"]["id"],
        if_match=published.detail["headEtag"],
    )
    inactive = await uow.deactivate(
        team_id="team_1",
        actor_id="user_1",
        style_id=published.detail["style"]["id"],
        if_match=editing.detail["headEtag"],
    )
    for index in range(11):
        await _create(uow, team="team_1", actor="user_1", name=f"Capacity {index:02d}")

    active = await uow.activate(
        team_id="team_1",
        actor_id="user_1",
        style_id=published.detail["style"]["id"],
        if_match=inactive.detail["headEtag"],
    )
    assert active.detail["state"] == "active_editing"


async def test_deactivate_removes_custom_style_from_v2_policy_atomically(db_path: Path) -> None:
    uow = ChartStyleUnitOfWork(db_path)
    published = await _publish(
        uow,
        team="team_1",
        actor="user_1",
        result=await _create(uow, team="team_1", actor="user_1", name="Optional Style"),
    )
    style_id = published.detail["style"]["id"]
    settings = TeamSettingsStore(db_path)
    policy = {
        "version": 2,
        "defaultStyleId": "bowhead-editorial",
        "enabledStyleIds": ["bowhead-editorial", "clean-blue", style_id],
        "fallbackBuiltInPresetId": "bowhead-editorial",
    }
    await settings.set("team_1", KEY_TEAM_CHART_STYLE_POLICY, json.dumps(policy), "user_1")

    inactive = await uow.deactivate(
        team_id="team_1",
        actor_id="user_1",
        style_id=style_id,
        if_match=published.detail["headEtag"],
    )

    assert inactive.detail["state"] == "inactive_clean"
    assert json.loads(await settings.get("team_1", KEY_TEAM_CHART_STYLE_POLICY))["enabledStyleIds"] == [
        "bowhead-editorial",
        "clean-blue",
    ]
    assert inactive.registry_version == 2


async def test_corrupt_policy_blocks_lifecycle_mutation(db_path: Path) -> None:
    uow = ChartStyleUnitOfWork(db_path)
    published = await _publish(
        uow,
        team="team_1",
        actor="user_1",
        result=await _create(uow, team="team_1", actor="user_1", name="Policy Guard"),
    )
    settings = TeamSettingsStore(db_path)
    await settings.set(
        "team_1",
        KEY_TEAM_CHART_STYLE_POLICY,
        json.dumps(
            {
                "version": 2,
                "defaultStyleId": "bowhead-editorial",
                "enabledStyleIds": ["bowhead-editorial", "bowhead-editorial"],
                "fallbackBuiltInPresetId": "bowhead-editorial",
            }
        ),
        "user_1",
    )

    with pytest.raises(ChartStyleDomainError) as corrupt:
        await uow.deactivate(
            team_id="team_1",
            actor_id="user_1",
            style_id=published.detail["style"]["id"],
            if_match=published.detail["headEtag"],
        )
    assert corrupt.value.code == "chart_style_policy_corrupt"

    async with (
        aiosqlite.connect(db_path) as db,
        db.execute(
            "SELECT lifecycle FROM team_chart_styles WHERE team_id=? AND id=?",
            ("team_1", published.detail["style"]["id"]),
        ) as cursor,
    ):
        assert await cursor.fetchone() == ("active",)
