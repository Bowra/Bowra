"""Export snapshot artifact pin, scope, resolver, and GC contracts."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from bowhead_core._db import open_shared_ctx
from bowhead_core.artifact_store import ArtifactStore, LocalBackend
from bowhead_core.canvas_store import CanvasStore
from bowhead_core.export_assets import (
    ExportAssetError,
    list_snapshot_asset_pins,
    load_resolved_chart_rows,
    resolve_snapshot_assets,
)
from bowhead_core.export_snapshot import (
    ExportSnapshot,
    ExportSnapshotCandidate,
    ExportSnapshotRequest,
    ExportSnapshotStore,
)
from bowhead_core.session_store import SessionStore

from bowhead_core import export_assets as export_assets_module

if TYPE_CHECKING:
    from bowhead_core.canvas_store import Canvas


@dataclass(frozen=True)
class Stores:
    path: Path
    sessions: SessionStore
    canvases: CanvasStore
    snapshots: ExportSnapshotStore
    artifacts: ArtifactStore
    canvas: Canvas


def _request(*, requested_by: str = "u_owner") -> ExportSnapshotRequest:
    return ExportSnapshotRequest(
        requested_by=requested_by,
        team_id="team_1",
        fmt="pptx",
        profile="full_report",
        parity_contract="full_content_v1",
        locale="zh-CN",
        timezone="Asia/Singapore",
        client_surface_receipt_sha256="sha256:receipt",
        theme={"kind": "default"},
        options={"include_appendix": True},
    )


@pytest.fixture
async def stores(tmp_path: Path) -> Stores:
    path = tmp_path / "assets.db"
    from alembic import command
    from alembic.config import Config

    from bowhead_core import migrations

    config = Config()
    config.set_main_option("script_location", str(Path(migrations.__file__).resolve().parent))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    config.attributes["bowhead_target"] = "core"
    command.upgrade(config, "head")
    sessions = SessionStore(path)
    canvases = CanvasStore(path)
    snapshots = ExportSnapshotStore(path)
    artifacts = ArtifactStore(
        db_path=str(path),
        backend=LocalBackend(root_dir=tmp_path / "blobs"),
    )
    await sessions.initialize()
    await canvases.initialize()
    await snapshots.initialize()
    await artifacts.initialize()
    await sessions.ensure_session(
        session_id="s_assets",
        workspace_id="ws_1",
        user_id="u_owner",
    )
    canvas = await canvases.ensure_canvas(
        session_id="s_assets",
        workspace_id="ws_1",
        user_id="u_owner",
        title="Asset report",
    )
    return Stores(
        path=path,
        sessions=sessions,
        canvases=canvases,
        snapshots=snapshots,
        artifacts=artifacts,
        canvas=canvas,
    )


async def _commit(stores: Stores) -> tuple[ExportSnapshotCandidate, ExportSnapshot]:
    candidate = await stores.snapshots.build_candidate(
        canvas_id=stores.canvas.id,
        request=_request(),
    )
    snapshot = await stores.snapshots.commit_candidate(
        candidate,
        expected_projection_token=candidate.content_projection.projection_token,
        expected_surface_token=candidate.surface_projection.surface_token,
    )
    return candidate, snapshot


async def test_snapshot_extracts_validates_and_atomically_pins_assets(stores: Stores) -> None:
    image = await stores.artifacts.put_bytes(
        content=b"image-bytes",
        filename="image.png",
        mime="image/png",
        source="image_generate",
        created_by="u_owner",
        session_id="s_assets",
        scope="session",
    )
    chart_data = await stores.artifacts.put_bytes(
        content=b"x,y\n1,2\n",
        filename="chart.csv",
        mime="text/csv",
        source="bi_fetch_rows",
        created_by="u_owner",
        scope="user",
    )
    image_section = await stores.canvases.append_section(
        canvas_id=stores.canvas.id,
        kind="image",
        payload={"artifact_id": image.id, "caption": "Pinned image"},
    )
    chart_section = await stores.canvases.append_section(
        canvas_id=stores.canvas.id,
        kind="chart",
        payload={
            "spec": {
                "kind": "bar",
                "title": "Pinned data",
                "data_artifact_id": chart_data.id,
                "row_count": 1,
                "columns": [],
                "rows": [{"x": 1}],
            }
        },
    )

    candidate, snapshot = await _commit(stores)
    refs = {(ref.artifact_id, ref.section_id, ref.role, ref.required) for ref in candidate.asset_refs}
    assert refs == {
        (image.id, image_section.id, "image", True),
        (chart_data.id, chart_section.id, "chart_data", True),
    }
    assert snapshot.payload["assets"] == [
        {
            "artifact_id": ref.artifact_id,
            "required": ref.required,
            "role": ref.role,
            "section_id": ref.section_id,
        }
        for ref in candidate.asset_refs
    ]
    pins = await list_snapshot_asset_pins(str(stores.path), snapshot.id)
    assert len(pins) == 2
    image_pin = next(pin for pin in pins if pin.artifact_id == image.id)
    chart_pin = next(pin for pin in pins if pin.artifact_id == chart_data.id)
    assert image_pin.scope == "session"
    assert image_pin.scope_session_id == "s_assets"
    assert chart_pin.scope == "user"
    assert chart_pin.scope_session_id is None


async def test_resolved_chart_rows_hydrate_full_csv_not_preview(
    stores: Stores,
    tmp_path: Path,
) -> None:
    csv_content = ("region,revenue\n" + "\n".join(f"R{index},{index}" for index in range(60)) + "\n").encode()
    chart_data = await stores.artifacts.put_bytes(
        content=csv_content,
        filename="full-chart.csv",
        mime="text/csv",
        source="bi_fetch_rows",
        created_by="u_owner",
        scope="user",
    )
    await stores.canvases.append_section(
        canvas_id=stores.canvas.id,
        kind="chart",
        payload={
            "spec": {
                "kind": "bar",
                "title": "Full data",
                "data_artifact_id": chart_data.id,
                "row_count": 60,
                "columns": [],
                "rows": [
                    {
                        "region": f"R{index}",
                        "revenue": index,
                    }
                    for index in range(50)
                ],
            }
        },
    )
    _, snapshot = await _commit(stores)
    attempt_dir = tmp_path / "exp_full_chart" / "attempt-1"
    resolution = await resolve_snapshot_assets(
        db_path=str(stores.path),
        snapshot_id=snapshot.id,
        artifact_store=stores.artifacts,
        attempt_dir=attempt_dir,
    )

    rows = load_resolved_chart_rows(
        snapshot=snapshot,
        asset_resolution=resolution,
        attempt_dir=attempt_dir,
    )

    assert len(rows[chart_data.id]) == 60
    assert rows[chart_data.id][-1] == {
        "region": "R59",
        "revenue": 59,
    }


@pytest.mark.parametrize("row_count", [98, 100_000])
def test_chart_csv_parser_preserves_supported_row_counts(
    row_count: int,
) -> None:
    content = ("value\n" + "\n".join(str(index) for index in range(row_count)) + "\n").encode()

    rows = export_assets_module._parse_chart_csv(  # noqa: SLF001
        content,
    )

    assert len(rows) == row_count
    assert rows[-1] == {"value": row_count - 1}


def test_chart_csv_parser_rejects_malformed_input() -> None:
    with pytest.raises(
        ExportAssetError,
        match="chart_data_invalid",
    ):
        export_assets_module._parse_chart_csv(  # noqa: SLF001
            b'a,b\n"unterminated,1\n',
        )


async def test_design_flatten_pin_requires_matching_artifact_provenance(
    stores: Stores,
) -> None:
    background = await stores.artifacts.put_bytes(
        content=b"background",
        filename="background.png",
        mime="image/png",
        created_by="u_owner",
        session_id="s_assets",
    )
    flattened = await stores.artifacts.put_bytes(
        content=b"flattened",
        filename="flattened.png",
        mime="image/png",
        created_by="u_owner",
        session_id="s_assets",
    )
    section = await stores.canvases.append_section(
        canvas_id=stores.canvas.id,
        kind="design",
        payload={
            "title": "Campaign",
            "size_preset": "square",
            "canvas_width": 1080,
            "canvas_height": 1080,
            "background": {
                "type": "artifact",
                "artifact_id": background.id,
            },
            "layers": [],
        },
    )
    await stores.artifacts.put_design_flatten_provenance(
        artifact_id=flattened.id,
        canvas_id=stores.canvas.id,
        section_id=section.id,
        section_rev=section.rev,
        size_preset="square",
        background_artifact_id=background.id,
    )
    updated = await stores.canvases.patch_section(
        section_id=section.id,
        patch={
            "flattened_artifact_id": flattened.id,
            "flattened_source_rev": section.rev,
            "flattened_at": "2026-07-17T00:00:00+00:00",
        },
        expected_rev=section.rev,
    )
    assert updated is not None

    candidate, snapshot = await _commit(stores)

    assert candidate.design_flatten_expectations[0].section_rev == 2
    pins = await list_snapshot_asset_pins(str(stores.path), snapshot.id)
    assert {(pin.artifact_id, pin.role) for pin in pins} == {
        (background.id, "design_background"),
        (flattened.id, "design_flattened"),
    }


async def test_stale_design_flatten_is_ignored_and_background_is_pinned(
    stores: Stores,
) -> None:
    background = await stores.artifacts.put_bytes(
        content=b"background",
        filename="background.png",
        mime="image/png",
        created_by="u_owner",
        session_id="s_assets",
    )
    flattened = await stores.artifacts.put_bytes(
        content=b"flattened",
        filename="flattened.png",
        mime="image/png",
        created_by="u_owner",
        session_id="s_assets",
    )
    section = await stores.canvases.append_section(
        canvas_id=stores.canvas.id,
        kind="design",
        payload={
            "title": "Campaign",
            "size_preset": "square",
            "canvas_width": 1080,
            "canvas_height": 1080,
            "background": {
                "type": "artifact",
                "artifact_id": background.id,
            },
            "layers": [],
        },
    )
    await stores.artifacts.put_design_flatten_provenance(
        artifact_id=flattened.id,
        canvas_id=stores.canvas.id,
        section_id=section.id,
        section_rev=section.rev,
        size_preset="square",
        background_artifact_id=background.id,
    )
    with_flatten = await stores.canvases.patch_section(
        section_id=section.id,
        patch={
            "flattened_artifact_id": flattened.id,
            "flattened_source_rev": section.rev,
        },
        expected_rev=section.rev,
    )
    assert with_flatten is not None
    stale = await stores.canvases.patch_section(
        section_id=section.id,
        patch={"title": "Campaign updated"},
        expected_rev=with_flatten.rev,
    )
    assert stale is not None

    candidate, snapshot = await _commit(stores)

    assert candidate.design_flatten_expectations == ()
    assert {(ref.artifact_id, ref.role) for ref in candidate.asset_refs} == {(background.id, "design_background")}
    pins = await list_snapshot_asset_pins(str(stores.path), snapshot.id)
    assert {(pin.artifact_id, pin.role) for pin in pins} == {(background.id, "design_background")}


async def test_design_flatten_forged_provenance_is_not_pinned(
    stores: Stores,
) -> None:
    background = await stores.artifacts.put_bytes(
        content=b"background",
        filename="background.png",
        mime="image/png",
        created_by="u_owner",
        session_id="s_assets",
    )
    flattened = await stores.artifacts.put_bytes(
        content=b"flattened",
        filename="flattened.png",
        mime="image/png",
        created_by="u_owner",
        session_id="s_assets",
    )
    section = await stores.canvases.append_section(
        canvas_id=stores.canvas.id,
        kind="design",
        payload={
            "title": "Campaign",
            "size_preset": "square",
            "canvas_width": 1080,
            "canvas_height": 1080,
            "background": {
                "type": "artifact",
                "artifact_id": background.id,
            },
            "layers": [],
            "flattened_artifact_id": flattened.id,
            "flattened_source_rev": 0,
        },
    )
    await stores.artifacts.put_design_flatten_provenance(
        artifact_id=flattened.id,
        canvas_id="cv_other",
        section_id=section.id,
        section_rev=0,
        size_preset="square",
        background_artifact_id=background.id,
    )
    candidate = await stores.snapshots.build_candidate(
        canvas_id=stores.canvas.id,
        request=_request(),
    )

    with pytest.raises(ExportAssetError) as error:
        await stores.snapshots.commit_candidate(
            candidate,
            expected_projection_token=(candidate.content_projection.projection_token),
            expected_surface_token=candidate.surface_projection.surface_token,
        )
    assert error.value.code == "asset_missing_or_forbidden"
    assert await stores.snapshots.get_snapshot(candidate.id) is None


@pytest.mark.parametrize("failure", ["other_user", "other_session", "soft_deleted", "bad_mime"])
async def test_pin_validation_fails_closed_without_orphan_snapshot(
    stores: Stores,
    failure: str,
) -> None:
    session_id = "s_assets"
    created_by = "u_owner"
    mime = "image/png"
    if failure == "other_session":
        await stores.sessions.ensure_session(
            session_id="s_other",
            workspace_id="ws_1",
            user_id="u_owner",
        )
        session_id = "s_other"
    elif failure == "other_user":
        created_by = "u_other"
    elif failure == "bad_mime":
        mime = "text/plain"
    artifact = await stores.artifacts.put_bytes(
        content=b"bytes",
        filename="asset.png",
        mime=mime,
        created_by=created_by,
        session_id=session_id,
        scope="session",
    )
    await stores.canvases.append_section(
        canvas_id=stores.canvas.id,
        kind="image",
        payload={"artifact_id": artifact.id},
    )
    if failure == "soft_deleted":
        assert await stores.artifacts.soft_delete(artifact.id, user_id="u_owner")
    candidate = await stores.snapshots.build_candidate(
        canvas_id=stores.canvas.id,
        request=_request(),
    )

    with pytest.raises(ExportAssetError) as error:
        await stores.snapshots.commit_candidate(
            candidate,
            expected_projection_token=candidate.content_projection.projection_token,
            expected_surface_token=candidate.surface_projection.surface_token,
        )
    assert error.value.code == "asset_missing_or_forbidden"
    assert await stores.snapshots.get_snapshot(candidate.id) is None
    assert await list_snapshot_asset_pins(str(stores.path), candidate.id) == ()


async def test_static_snapshot_does_not_persist_raw_source_identity(stores: Stores) -> None:
    # Static export projection replaces raw source identities with irreversible
    # ordinals before asset extraction.  A tool-call id is therefore neither
    # persisted in the manifest nor treated as an artifact pin; required image
    # assets remain pinned normally.
    image = await stores.artifacts.put_bytes(
        content=b"image-bytes",
        filename="image.png",
        mime="image/png",
        source="image_generate",
        created_by="u_owner",
        session_id="s_assets",
        scope="session",
    )
    await stores.canvases.append_section(
        canvas_id=stores.canvas.id,
        kind="image",
        payload={"artifact_id": image.id},
    )
    await stores.canvases.append_section(
        canvas_id=stores.canvas.id,
        kind="sources",
        payload={"entries": [{"artifact_id": "tl_missing_tool_call"}]},
    )
    candidate = await stores.snapshots.build_candidate(
        canvas_id=stores.canvas.id,
        request=_request(),
    )
    assert "tl_missing_tool_call" not in candidate.payload_json
    assert {(ref.artifact_id, ref.role, ref.required) for ref in candidate.asset_refs} == {(image.id, "image", True)}

    # Commit succeeds because no raw, unresolvable source identity enters the
    # asset pin contract.
    await stores.snapshots.commit_candidate(
        candidate,
        expected_projection_token=candidate.content_projection.projection_token,
        expected_surface_token=candidate.surface_projection.surface_token,
    )
    assert await stores.snapshots.get_snapshot(candidate.id) is not None
    pinned_ids = {pin.artifact_id for pin in await list_snapshot_asset_pins(str(stores.path), candidate.id)}
    assert "tl_missing_tool_call" not in pinned_ids
    assert image.id in pinned_ids


async def test_pinned_required_asset_resolves_after_user_soft_delete(stores: Stores) -> None:
    content = b"immutable-image"
    artifact = await stores.artifacts.put_bytes(
        content=content,
        filename="image.png",
        mime="image/png",
        created_by="u_owner",
        session_id="s_assets",
    )
    await stores.canvases.append_section(
        canvas_id=stores.canvas.id,
        kind="image",
        payload={"artifact_id": artifact.id},
    )
    _, snapshot = await _commit(stores)
    assert await stores.artifacts.soft_delete(artifact.id, user_id="u_owner")

    resolution = await resolve_snapshot_assets(
        db_path=str(stores.path),
        snapshot_id=snapshot.id,
        artifact_store=stores.artifacts,
    )

    expected_hash = f"sha256:{hashlib.sha256(content).hexdigest()}"
    assert resolution.warnings == ()
    assert resolution.resolved[0].content_sha256 == expected_hash
    pins = await list_snapshot_asset_pins(str(stores.path), snapshot.id)
    assert pins[0].content_sha256 == expected_hash
    assert pins[0].resolved_at is not None


async def test_resolver_materializes_only_safe_attempt_relative_files(
    stores: Stores,
    tmp_path: Path,
) -> None:
    content = b"immutable-image"
    artifact = await stores.artifacts.put_bytes(
        content=content,
        filename="../../secret.png",
        mime="image/png",
        created_by="u_owner",
        session_id="s_assets",
    )
    await stores.canvases.append_section(
        canvas_id=stores.canvas.id,
        kind="image",
        payload={"artifact_id": artifact.id},
    )
    _, snapshot = await _commit(stores)
    attempt_dir = tmp_path / "exp_job" / "attempt-1"

    resolution = await resolve_snapshot_assets(
        db_path=str(stores.path),
        snapshot_id=snapshot.id,
        artifact_store=stores.artifacts,
        attempt_dir=attempt_dir,
    )

    resolved = resolution.resolved[0]
    assert resolved.mime == "image/png"
    assert resolved.relative_path is not None
    assert resolved.relative_path.startswith("assets/")
    assert artifact.id not in resolved.relative_path
    assert ".." not in resolved.relative_path
    assert (attempt_dir / resolved.relative_path).read_bytes() == content


async def test_soft_delete_race_cannot_create_snapshot_without_pin(stores: Stores) -> None:
    artifact = await stores.artifacts.put_bytes(
        content=b"race",
        filename="race.png",
        mime="image/png",
        created_by="u_owner",
        session_id="s_assets",
    )
    await stores.canvases.append_section(
        canvas_id=stores.canvas.id,
        kind="image",
        payload={"artifact_id": artifact.id},
    )
    candidate = await stores.snapshots.build_candidate(
        canvas_id=stores.canvas.id,
        request=_request(),
    )

    commit_result, delete_result = await asyncio.gather(
        stores.snapshots.commit_candidate(
            candidate,
            expected_projection_token=candidate.content_projection.projection_token,
            expected_surface_token=candidate.surface_projection.surface_token,
        ),
        stores.artifacts.soft_delete(artifact.id, user_id="u_owner"),
        return_exceptions=True,
    )

    assert delete_result is True
    if isinstance(commit_result, ExportAssetError):
        assert commit_result.code == "asset_missing_or_forbidden"
        assert await stores.snapshots.get_snapshot(candidate.id) is None
        assert await list_snapshot_asset_pins(str(stores.path), candidate.id) == ()
    else:
        assert not isinstance(commit_result, BaseException)
        pins = await list_snapshot_asset_pins(str(stores.path), candidate.id)
        assert len(pins) == 1
        assert pins[0].artifact_id == artifact.id


async def test_resolver_warns_for_optional_blob_but_fails_required_blob(stores: Stores) -> None:
    optional = await stores.artifacts.put_bytes(
        content=b"<html></html>",
        filename="embed.html",
        mime="text/html",
        created_by="u_owner",
        session_id="s_assets",
    )
    required = await stores.artifacts.put_bytes(
        content=b"image",
        filename="image.png",
        mime="image/png",
        created_by="u_owner",
        session_id="s_assets",
    )
    await stores.canvases.append_section(
        canvas_id=stores.canvas.id,
        kind="html_artifact",
        payload={"artifact_id": optional.id, "title": "Embed"},
    )
    _, optional_snapshot = await _commit(stores)
    Path(optional.path).unlink()

    optional_resolution = await resolve_snapshot_assets(
        db_path=str(stores.path),
        snapshot_id=optional_snapshot.id,
        artifact_store=stores.artifacts,
    )
    assert optional_resolution.resolved == ()
    assert len(optional_resolution.warnings) == 1
    assert optional_resolution.warnings[0].code == "optional_asset_unavailable"

    await stores.canvases.append_section(
        canvas_id=stores.canvas.id,
        kind="image",
        payload={"artifact_id": required.id},
    )
    _, required_snapshot = await _commit(stores)
    Path(required.path).unlink()
    with pytest.raises(ExportAssetError) as required_error:
        await resolve_snapshot_assets(
            db_path=str(stores.path),
            snapshot_id=required_snapshot.id,
            artifact_store=stores.artifacts,
        )
    assert required_error.value.code == "asset_missing_or_forbidden"


async def test_resolver_rejects_unknown_snapshot_scope(stores: Stores) -> None:
    with pytest.raises(ExportAssetError) as error:
        await resolve_snapshot_assets(
            db_path=str(stores.path),
            snapshot_id="exs_unknown",
            artifact_store=stores.artifacts,
        )
    assert error.value.code == "snapshot_not_found"


async def test_unexpired_snapshot_pin_blocks_hard_gc(stores: Stores) -> None:
    artifact = await stores.artifacts.put_bytes(
        content=b"pinned",
        filename="pinned.png",
        mime="image/png",
        created_by="u_owner",
        session_id="s_assets",
    )
    await stores.canvases.append_section(
        canvas_id=stores.canvas.id,
        kind="image",
        payload={"artifact_id": artifact.id},
    )
    _, snapshot = await _commit(stores)
    assert await stores.artifacts.soft_delete(artifact.id, user_id="u_owner")
    async with open_shared_ctx(str(stores.path)) as db:
        await db.execute(
            "UPDATE artifacts SET deleted_at='1970-01-01T00:00:00+00:00' WHERE id=?",
            (artifact.id,),
        )
        await db.commit()

    protected = await stores.artifacts.hard_delete_old_soft_deleted(days=30)
    assert protected["pins_skipped"] == 1
    assert protected["rows_removed"] == 0
    assert await stores.artifacts.get(artifact.id, include_deleted=True) is not None

    async with open_shared_ctx(str(stores.path)) as db:
        await db.execute(
            "DELETE FROM canvas_export_snapshots WHERE id=?",
            (snapshot.id,),
        )
        await db.commit()
    collected = await stores.artifacts.hard_delete_old_soft_deleted(days=30)
    assert collected["rows_removed"] == 1
    assert await stores.artifacts.get(artifact.id, include_deleted=True) is None


async def test_asset_size_limits_are_enforced_on_unique_blobs(
    stores: Stores,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from bowhead_core import export_assets

    first = await stores.artifacts.put_bytes(
        content=b"123",
        filename="first.png",
        mime="image/png",
        created_by="u_owner",
        session_id="s_assets",
    )
    second = await stores.artifacts.put_bytes(
        content=b"456",
        filename="second.png",
        mime="image/png",
        created_by="u_owner",
        session_id="s_assets",
    )
    await stores.canvases.append_section(
        canvas_id=stores.canvas.id,
        kind="image",
        payload={"artifact_id": first.id},
    )
    await stores.canvases.append_section(
        canvas_id=stores.canvas.id,
        kind="image",
        payload={"artifact_id": second.id},
    )
    candidate = await stores.snapshots.build_candidate(
        canvas_id=stores.canvas.id,
        request=_request(),
    )
    monkeypatch.setattr(export_assets, "MAX_SNAPSHOT_ASSET_BYTES", 2)
    monkeypatch.setattr(export_assets, "MAX_SNAPSHOT_ASSETS_TOTAL_BYTES", 100)

    with pytest.raises(ExportAssetError) as single_error:
        await stores.snapshots.commit_candidate(
            candidate,
            expected_projection_token=candidate.content_projection.projection_token,
            expected_surface_token=candidate.surface_projection.surface_token,
        )
    assert single_error.value.code == "asset_too_large"
    assert await stores.snapshots.get_snapshot(candidate.id) is None

    monkeypatch.setattr(export_assets, "MAX_SNAPSHOT_ASSET_BYTES", 4)
    monkeypatch.setattr(export_assets, "MAX_SNAPSHOT_ASSETS_TOTAL_BYTES", 5)
    with pytest.raises(ExportAssetError) as total_error:
        await stores.snapshots.commit_candidate(
            candidate,
            expected_projection_token=candidate.content_projection.projection_token,
            expected_surface_token=candidate.surface_projection.surface_token,
        )
    assert total_error.value.code == "snapshot_assets_too_large"
