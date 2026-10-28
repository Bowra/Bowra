"""Durable XLSX processor phase, validation, and persistence contracts."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from bowhead_core.artifact_store import ArtifactStore
from bowhead_core.export_assets import SnapshotAssetResolution
from bowhead_core.export_ir import ExportIR, ExportIRDocument
from bowhead_core.export_manifest import (
    ExportManifest,
    ManifestParity,
    ManifestQuality,
    ManifestRenderer,
    ManifestSummary,
    ManifestValidation,
)
from bowhead_core.export_materializer import MaterializedVisual
from bowhead_core.export_render_subprocess import ExportRenderOutcome
from bowhead_core.export_runner import (
    ExportAttemptStale,
    ExportExecutionContext,
    ExportProcessingError,
)
from bowhead_core.export_snapshot import (
    ExportSnapshot,
    ExportSnapshotStore,
)
from bowhead_core.export_store import ExportJobV2
from bowhead_core.export_xlsx_processor import (
    CanvasXlsxExportProcessor,
)
from bowhead_core.print_theme import current_print_theme_snapshot


@dataclass
class _FakeSnapshotStore:
    snapshot: ExportSnapshot

    async def get_snapshot(
        self,
        snapshot_id: str,
    ) -> ExportSnapshot | None:
        return self.snapshot if snapshot_id == self.snapshot.id else None


class _FakeArtifactStore:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.discarded: list[tuple[object, ...]] = []

    async def stage_export_bytes(
        self,
        **kwargs: Any,
    ) -> SimpleNamespace:
        kind = "manifest" if kwargs["mime"] == "application/json" else "xlsx"
        self.calls.append((kind, kwargs))
        return SimpleNamespace(
            artifact=SimpleNamespace(
                id=f"art_{kind}",
                filename=kwargs["filename"],
                size=len(kwargs["content"]),
            ),
            session_id=kwargs["session_id"],
        )

    async def discard_staged_artifacts(
        self,
        staged_artifacts: tuple[object, ...],
    ) -> None:
        self.discarded.append(staged_artifacts)


@dataclass
class _FakeContext:
    phases: list[str]
    ensure_count: int = 0
    publish_count: int = 0
    fail_ensure_at: int | None = None

    async def advance(
        self,
        phase: str,
        *,
        progress: dict[str, object] | None = None,
    ) -> None:
        del progress
        self.phases.append(phase)

    async def ensure_current(self) -> None:
        self.ensure_count += 1
        if self.ensure_count == self.fail_ensure_at:
            raise ExportAttemptStale("stale")

    async def publish_completion(self, **_kwargs: object) -> None:
        self.publish_count += 1


async def test_xlsx_processor_persists_only_after_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = _snapshot()
    export_ir = _export_ir(snapshot)
    artifacts = _FakeArtifactStore()
    context = _FakeContext(phases=[])
    _patch_pipeline(
        monkeypatch=monkeypatch,
        export_ir=export_ir,
        manifest=_manifest(
            snapshot=snapshot,
            quality="clean",
        ),
    )
    processor = _processor(
        tmp_path=tmp_path,
        snapshot=snapshot,
        artifacts=artifacts,
    )

    completion = await processor.process(
        _job(snapshot),
        cast(ExportExecutionContext, context),
    )

    assert completion.artifact_id == "art_xlsx"
    assert completion.manifest_artifact_id == "art_manifest"
    assert completion.quality_status == "clean"
    assert completion.committed is True
    assert context.phases == [
        "normalizing",
        "materializing",
        "planning",
        "rendering",
        "validating",
        "persisting",
    ]
    assert context.ensure_count == 4
    assert context.publish_count == 1
    assert [name for name, _kwargs in artifacts.calls] == [
        "manifest",
        "xlsx",
    ]
    assert artifacts.calls[1][1]["manifest_artifact_id"] == "art_manifest"
    assert not (tmp_path / "staging/exp_xlsx_data_workbook").exists()


async def test_xlsx_processor_rejected_output_is_not_persisted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = _snapshot()
    export_ir = _export_ir(snapshot)
    artifacts = _FakeArtifactStore()
    context = _FakeContext(phases=[])
    _patch_pipeline(
        monkeypatch=monkeypatch,
        export_ir=export_ir,
        manifest=_manifest(
            snapshot=snapshot,
            quality="rejected",
        ),
    )
    processor = _processor(
        tmp_path=tmp_path,
        snapshot=snapshot,
        artifacts=artifacts,
    )

    with pytest.raises(ExportProcessingError) as error:
        await processor.process(
            _job(snapshot),
            cast(ExportExecutionContext, context),
        )

    assert error.value.code == "export_validation_rejected"
    assert error.value.quality_status == "rejected"
    assert artifacts.calls == []
    assert context.phases[-1] == "validating"
    assert "persisting" not in context.phases
    assert not (tmp_path / "staging/exp_xlsx_data_workbook").exists()


async def test_xlsx_processor_discards_both_staged_blobs_when_stale(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = _snapshot()
    export_ir = _export_ir(snapshot)
    artifacts = _FakeArtifactStore()
    context = _FakeContext(
        phases=[],
        fail_ensure_at=4,
    )
    _patch_pipeline(
        monkeypatch=monkeypatch,
        export_ir=export_ir,
        manifest=_manifest(
            snapshot=snapshot,
            quality="clean",
        ),
    )
    processor = _processor(
        tmp_path=tmp_path,
        snapshot=snapshot,
        artifacts=artifacts,
    )

    with pytest.raises(ExportAttemptStale):
        await processor.process(
            _job(snapshot),
            cast(ExportExecutionContext, context),
        )

    assert [name for name, _kwargs in artifacts.calls] == ["manifest", "xlsx"]
    assert len(artifacts.discarded) == 1
    assert len(artifacts.discarded[0]) == 2
    assert not (tmp_path / "staging/exp_xlsx_data_workbook").exists()


def _processor(
    *,
    tmp_path: Path,
    snapshot: ExportSnapshot,
    artifacts: _FakeArtifactStore,
) -> CanvasXlsxExportProcessor:
    return CanvasXlsxExportProcessor(
        db_path=str(tmp_path / "db.sqlite"),
        snapshot_store=cast(
            ExportSnapshotStore,
            _FakeSnapshotStore(snapshot),
        ),
        artifact_store=cast(ArtifactStore, artifacts),
        staging_root=tmp_path / "staging",
        soffice_path=tmp_path / "soffice",
        browser_renderer_factory=lambda _attempt, _snapshot: cast(
            Any,
            object(),
        ),
    )


def _patch_pipeline(
    *,
    monkeypatch: pytest.MonkeyPatch,
    export_ir: ExportIR,
    manifest: ExportManifest,
) -> None:
    async def resolve(**_kwargs: Any) -> SnapshotAssetResolution:
        return SnapshotAssetResolution(
            resolved=(),
            warnings=(),
        )

    async def materialize(
        **_kwargs: Any,
    ) -> tuple[MaterializedVisual, ...]:
        return ()

    async def run_worker(
        **kwargs: Any,
    ) -> ExportRenderOutcome:
        attempt_dir = Path(kwargs["attempt_dir"])
        output = attempt_dir / "output/output.xlsx"
        output.parent.mkdir(parents=True)
        output.write_bytes(b"PK\x03\x04closed-xlsx")
        await kwargs["on_progress"](
            "validating",
            progress={"sheets": 7},
        )
        return ExportRenderOutcome(
            output_path=output,
            manifest=manifest.to_dict(),
            quality_status=manifest.validation.result,
            warning_count=0,
            size_bytes=15,
        )

    monkeypatch.setattr(
        "bowhead_core.export_xlsx_processor.resolve_snapshot_assets",
        resolve,
    )
    monkeypatch.setattr(
        "bowhead_core.export_xlsx_processor.normalize_export_snapshot",
        lambda *_args, **_kwargs: export_ir,
    )
    monkeypatch.setattr(
        "bowhead_core.export_xlsx_processor.materialize_export_visuals",
        materialize,
    )
    monkeypatch.setattr(
        "bowhead_core.export_xlsx_processor.run_export_format_worker",
        run_worker,
    )


def _snapshot() -> ExportSnapshot:
    return ExportSnapshot(
        id="exs_xlsx_data_workbook",
        schema_version=1,
        canvas_id="cv_xlsx_processor",
        session_id="s_xlsx_processor",
        team_id="team_xlsx_processor",
        workspace_id="ws_xlsx_processor",
        canvas_owner="u_owner",
        requested_by="u_owner",
        fmt="xlsx",
        profile="data_workbook",
        title="Quarterly / Review",
        locale="zh-CN",
        timezone="Asia/Singapore",
        projection_token=f"sha256:{'1' * 64}",
        surface_contract_version=1,
        surface_token=f"sha256:{'2' * 64}",
        parity_contract="data_parity_v1",
        payload={
            "request": {
                "options": {"include_appendix": True},
                "client_surface_receipt_sha256": (f"sha256:{'3' * 64}"),
                "print_theme": current_print_theme_snapshot(),
            },
            "surface_projection": {
                "entries": [],
                "exclusions": [],
            },
        },
        payload_sha256=f"sha256:{'4' * 64}",
        size_bytes=100,
        section_count=0,
        created_at="2026-07-17T00:00:00+00:00",
        expires_at="2026-07-24T00:00:00+00:00",
    )


def _export_ir(snapshot: ExportSnapshot) -> ExportIR:
    return ExportIR(
        schema_version=1,
        snapshot_id=snapshot.id,
        input_sha256=snapshot.payload_sha256,
        surface_token=snapshot.surface_token,
        document=ExportIRDocument(
            title=snapshot.title,
            locale=snapshot.locale,
            timezone=snapshot.timezone,
            profile=snapshot.profile,
        ),
        blocks=(),
        visual_assets=(),
        sources=(),
        normalization_warnings=(),
    )


def _manifest(
    *,
    snapshot: ExportSnapshot,
    quality: ManifestQuality,
) -> ExportManifest:
    return ExportManifest(
        manifest_version=2,
        trust_copy_contract="B1a-v1",
        job_id="exp_xlsx_data_workbook",
        snapshot_id=snapshot.id,
        input_sha256=snapshot.payload_sha256,
        format="xlsx",
        profile=snapshot.profile,
        renderer=ManifestRenderer(
            name="test",
            version="1",
        ),
        summary=ManifestSummary(
            input_sections=0,
            accounted_sections=0,
            rendered=0,
            fallback=0,
            excluded_by_policy=0,
            failed=0,
            accounted_rate=1.0,
            rendered_rate=None,
            semantic_parity_rate=None,
            visual_identity_rate=None,
            typed_data_parity_rate=None,
            unexpected_extra_sections=0,
            demo_residue_count=0,
        ),
        parity=ManifestParity(
            contract=snapshot.parity_contract,
            surface_contract_version=1,
            surface_token=snapshot.surface_token,
            client_surface_receipt_sha256=f"sha256:{'3' * 64}",
            semantic_required=0,
            semantic_matched=0,
            visual_required=0,
            visual_matched=0,
            typed_data_required=0,
            typed_data_matched=0,
        ),
        projection_exclusions=(),
        sections=(),
        validation=ManifestValidation(
            result=quality,
            checks=(),
        ),
    )


def _job(snapshot: ExportSnapshot) -> ExportJobV2:
    return ExportJobV2(
        id="exp_xlsx_data_workbook",
        canvas_id=snapshot.canvas_id,
        fmt="xlsx",
        status="running",
        contract_version=2,
        renderer_contract_version=1,
        profile=snapshot.profile,
        parity_contract=snapshot.parity_contract,
        surface_token=snapshot.surface_token,
        phase="resolving_assets",
        quality_status="pending",
        warning_count=0,
        snapshot_id=snapshot.id,
        requested_by=snapshot.requested_by,
        idempotency_key="idem-xlsx-data",
        request_sha256=f"sha256:{'5' * 64}",
        attempt=1,
        worker_id="worker-xlsx",
        runner_kind="local",
        runner_task_id=None,
        lease_expires_at=None,
        progress_json=None,
        artifact_id=None,
        manifest_artifact_id=None,
        url=None,
        filename=None,
        size=None,
        error_code=None,
        error=None,
        started_at="2026-07-17T00:00:00+00:00",
        finished_at=None,
        expires_at="2026-10-17T00:00:00+00:00",
        created_at="2026-07-17T00:00:00+00:00",
        updated_at="2026-07-17T00:00:00+00:00",
    )
