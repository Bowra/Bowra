"""Durable Canvas Export V2 PPTX processor assembly."""

from __future__ import annotations

import asyncio
import json
import re
import shutil
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bowhead_core.artifact_store import ArtifactStore, StagedArtifact
from bowhead_core.export_assets import resolve_snapshot_assets
from bowhead_core.export_browser_visual import (
    PlaywrightVisualRenderer,
    chart_style_theme_from_snapshot,
    print_theme_from_snapshot,
)
from bowhead_core.export_capabilities import ExportTarget
from bowhead_core.export_copy_contract import (
    CURRENT_MANIFEST_PRODUCER,
    MAX_COPY_MANIFEST_BYTES,
    current_manifest_payload_matches,
    output_producer_for,
)
from bowhead_core.export_ir import (
    ExportIR,
    build_capability_plan,
    normalize_export_snapshot,
)
from bowhead_core.export_materializer import (
    BrowserVisualRenderer,
    materialize_export_visuals,
)
from bowhead_core.export_pptx import (
    PPTX_MIME,
    PptxProfile,
)
from bowhead_core.export_render_subprocess import (
    run_export_format_worker,
)
from bowhead_core.export_runner import (
    ExportAttemptStale,
    ExportExecutionContext,
    ExportProcessingError,
)
from bowhead_core.export_snapshot import (
    ExportSnapshot,
    ExportSnapshotStore,
)
from bowhead_core.export_store import ExportCompletion, ExportJobV2

_MANIFEST_MIME = "application/json"
_SAFE_TITLE_RE = re.compile(r"[/\\\x00-\x1f\x7f]+")

BrowserRendererFactory = Callable[
    [Path, ExportSnapshot],
    BrowserVisualRenderer,
]


@dataclass(frozen=True, slots=True)
class CanvasPptxExportProcessor:
    db_path: str
    snapshot_store: ExportSnapshotStore
    artifact_store: ArtifactStore
    staging_root: Path
    soffice_path: Path
    pdftoppm_path: Path
    browser_renderer_factory: BrowserRendererFactory

    def supports(self, fmt: str, profile: str) -> bool:
        return fmt == "pptx" and profile in {"full_report", "executive_summary"}

    def supported_pairs(self) -> tuple[tuple[str, str], ...]:
        return (
            ("pptx", "full_report"),
            ("pptx", "executive_summary"),
        )

    async def process(
        self,
        job: ExportJobV2,
        context: ExportExecutionContext,
    ) -> ExportCompletion:
        if not self.supports(job.fmt, job.profile):
            raise ExportProcessingError(
                "unsupported_required_capability",
                "该导出范围暂不支持",
            )
        snapshot = await self.snapshot_store.get_snapshot(job.snapshot_id)
        if snapshot is None or not _snapshot_matches_job(
            snapshot=snapshot,
            job=job,
        ):
            raise ExportProcessingError(
                "snapshot_unavailable",
                "导出快照不可用，请重新导出",
            )
        attempt_dir = _prepare_attempt_dir(
            staging_root=self.staging_root,
            job_id=job.id,
            attempt=job.attempt,
        )
        try:
            asset_resolution = await resolve_snapshot_assets(
                db_path=self.db_path,
                snapshot_id=snapshot.id,
                artifact_store=self.artifact_store,
                attempt_dir=attempt_dir,
            )
            await context.advance(
                "normalizing",
                progress={
                    "resolved_assets": len(asset_resolution.resolved),
                },
            )
            export_ir = normalize_export_snapshot(
                snapshot,
                asset_content_hashes={asset.artifact_id: asset.content_sha256 for asset in asset_resolution.resolved},
            )
            await context.advance(
                "materializing",
                progress={"sections": snapshot.section_count},
            )
            visuals = await materialize_export_visuals(
                export_ir=export_ir,
                asset_resolution=asset_resolution,
                attempt_dir=attempt_dir,
                browser_renderer=self.browser_renderer_factory(
                    attempt_dir,
                    snapshot,
                ),
            )
            await context.advance(
                "planning",
                progress={"visuals": len(visuals)},
            )
            include_appendix = _include_appendix(snapshot)
            profile = _pptx_profile(snapshot.profile)
            target = _assert_pptx_plan(
                export_ir=export_ir,
                profile=profile,
                include_appendix=include_appendix,
            )
            await context.advance("rendering")
            outcome = await run_export_format_worker(
                fmt="pptx",
                job_id=job.id,
                snapshot=snapshot,
                export_ir=export_ir,
                materialized_visuals=visuals,
                attempt_dir=attempt_dir,
                include_appendix=include_appendix,
                target=target,
                profile=profile,
                soffice_path=self.soffice_path,
                pdftoppm_path=self.pdftoppm_path,
                on_progress=context.advance,
            )
            if outcome.quality_status == "rejected":
                raise ExportProcessingError(
                    "export_validation_rejected",
                    "导出完整性校验未通过，请重新导出",
                    quality_status="rejected",
                )
            await context.ensure_current()
            await context.advance(
                "persisting",
                progress={
                    "quality_status": outcome.quality_status,
                    "warning_count": outcome.warning_count,
                },
            )
            return await self._persist(
                job_id=job.id,
                snapshot=snapshot,
                output_path=outcome.output_path,
                manifest=outcome.manifest,
                quality_status=outcome.quality_status,
                warning_count=outcome.warning_count,
                context=context,
            )
        except (ExportAttemptStale, ExportProcessingError):
            raise
        except Exception as exc:
            raise ExportProcessingError(
                "renderer_failed",
                "导出处理失败，请重新导出",
            ) from exc
        finally:
            _cleanup_attempt(
                staging_root=self.staging_root,
                attempt_dir=attempt_dir,
            )

    async def _persist(
        self,
        *,
        job_id: str,
        snapshot: ExportSnapshot,
        output_path: Path,
        manifest: dict[str, Any],
        quality_status: str,
        warning_count: int,
        context: ExportExecutionContext,
    ) -> ExportCompletion:
        await context.ensure_current()
        if not current_manifest_payload_matches(manifest, job_id=job_id, fmt=snapshot.fmt):
            raise ExportProcessingError("export_manifest_invalid", "导出校验清单无效，请重新导出")
        manifest_content = json.dumps(
            manifest,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(manifest_content) > MAX_COPY_MANIFEST_BYTES:
            raise ExportProcessingError("export_manifest_too_large", "导出校验清单过大，请缩小报告后重试")
        base_name = _safe_export_title(snapshot.title)
        staged_artifacts: list[StagedArtifact] = []
        published = False
        try:
            manifest_artifact = await self.artifact_store.stage_export_bytes(
                content=manifest_content,
                filename=f"{base_name}.manifest.json",
                mime=_MANIFEST_MIME,
                canvas_id=snapshot.canvas_id,
                created_by=snapshot.requested_by,
                session_id=snapshot.session_id,
                producer_kind=CURRENT_MANIFEST_PRODUCER,
            )
            staged_artifacts.append(manifest_artifact)
            await context.ensure_current()
            output_content = await asyncio.to_thread(
                output_path.read_bytes,
            )
            pptx_artifact = await self.artifact_store.stage_export_bytes(
                content=output_content,
                filename=f"{base_name}.pptx",
                mime=PPTX_MIME,
                canvas_id=snapshot.canvas_id,
                created_by=snapshot.requested_by,
                session_id=snapshot.session_id,
                producer_kind=output_producer_for(engine="durable", fmt="pptx"),
                manifest_artifact_id=(manifest_artifact.artifact.id),
            )
            staged_artifacts.append(pptx_artifact)
            await context.ensure_current()
            completion = ExportCompletion(
                artifact_id=pptx_artifact.artifact.id,
                manifest_artifact_id=manifest_artifact.artifact.id,
                filename=pptx_artifact.artifact.filename,
                size=pptx_artifact.artifact.size,
                quality_status=quality_status,
                warning_count=warning_count,
                committed=True,
            )
            await context.publish_completion(
                artifact_store=self.artifact_store,
                output_artifact=pptx_artifact,
                manifest_artifact=manifest_artifact,
                completion=completion,
            )
            published = True
            return completion
        finally:
            if not published and staged_artifacts:
                await _discard_staged_safely(
                    self.artifact_store,
                    tuple(staged_artifacts),
                )


def build_playwright_renderer_factory(
    *,
    frontend_dir: Path,
    frontend_base_url: str,
    node_binary: Path,
    script_path: Path,
) -> BrowserRendererFactory:
    def create(
        attempt_dir: Path,
        snapshot: ExportSnapshot,
    ) -> BrowserVisualRenderer:
        return PlaywrightVisualRenderer(
            attempt_dir=attempt_dir,
            frontend_dir=frontend_dir,
            frontend_base_url=frontend_base_url,
            locale=snapshot.locale,
            timezone=snapshot.timezone,
            theme=chart_style_theme_from_snapshot(snapshot),
            node_binary=node_binary,
            script_path=script_path,
            print_theme=print_theme_from_snapshot(snapshot),
        )

    return create


def _snapshot_matches_job(
    *,
    snapshot: ExportSnapshot,
    job: ExportJobV2,
) -> bool:
    return (
        snapshot.id == job.snapshot_id
        and snapshot.canvas_id == job.canvas_id
        and snapshot.requested_by == job.requested_by
        and snapshot.fmt == job.fmt
        and snapshot.profile == job.profile
        and snapshot.parity_contract == job.parity_contract
        and snapshot.surface_token == job.surface_token
    )


def _include_appendix(snapshot: ExportSnapshot) -> bool:
    request = snapshot.payload.get("request")
    options = request.get("options") if isinstance(request, dict) else None
    value = options.get("include_appendix") if isinstance(options, dict) else None
    if not isinstance(value, bool):
        raise ExportProcessingError(
            "invalid_options",
            "导出选项无效，请重新导出",
        )
    return value


def _pptx_profile(value: str) -> PptxProfile:
    if value == "full_report":
        return "full_report"
    if value == "executive_summary":
        return "executive_summary"
    raise ExportProcessingError(
        "unsupported_required_capability",
        "该导出范围暂不支持",
    )


def _assert_pptx_plan(
    *,
    export_ir: ExportIR,
    profile: PptxProfile,
    include_appendix: bool,
) -> ExportTarget:
    target: ExportTarget = "pptx_full" if profile == "full_report" else "pptx_summary"
    plan = build_capability_plan(
        export_ir,
        target=target,
        include_appendix=include_appendix,
    )
    if profile == "full_report":
        invalid = [
            entry
            for entry in plan
            if entry.action
            not in {
                "render",
                "fallback",
                "appendix",
            }
        ]
    else:
        invalid = [
            entry
            for entry in plan
            if entry.action
            not in {
                "render",
                "fallback",
                "appendix",
                "summary_candidate",
                "exclude",
            }
        ]
    if invalid:
        raise ExportProcessingError(
            "unsupported_required_capability",
            "报告包含当前无法可靠导出的内容",
        )
    return target


def _prepare_attempt_dir(
    *,
    staging_root: Path,
    job_id: str,
    attempt: int,
) -> Path:
    if not staging_root.is_absolute() or not re.fullmatch(r"exp_[a-zA-Z0-9_-]+", job_id) or attempt < 1:
        raise ExportProcessingError(
            "export_staging_invalid",
            "导出执行环境不可用，请稍后重试",
        )
    staging_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if staging_root.is_symlink() or not staging_root.is_dir():
        raise ExportProcessingError(
            "export_staging_invalid",
            "导出执行环境不可用，请稍后重试",
        )
    attempt_dir = staging_root / job_id / f"attempt-{attempt}"
    try:
        attempt_dir.mkdir(
            mode=0o700,
            parents=True,
            exist_ok=False,
        )
    except FileExistsError as exc:
        raise ExportProcessingError(
            "export_staging_conflict",
            "导出执行环境冲突，请重新导出",
        ) from exc
    return attempt_dir


def _cleanup_attempt(
    *,
    staging_root: Path,
    attempt_dir: Path,
) -> None:
    if not attempt_dir.exists() or attempt_dir.is_symlink() or not attempt_dir.resolve().is_relative_to(staging_root.resolve()):
        return
    shutil.rmtree(attempt_dir)
    job_dir = attempt_dir.parent
    with suppress(OSError):
        job_dir.rmdir()


def _safe_export_title(value: str) -> str:
    cleaned = _SAFE_TITLE_RE.sub("_", value.strip()).strip(". ")
    return (cleaned or "Bowhead-report")[:120]


async def _discard_staged_safely(
    artifact_store: ArtifactStore,
    staged_artifacts: tuple[StagedArtifact, ...],
) -> None:
    cleanup = asyncio.create_task(
        artifact_store.discard_staged_artifacts(
            staged_artifacts,
        )
    )
    try:
        await asyncio.shield(cleanup)
    except asyncio.CancelledError:
        await cleanup
        raise


__all__ = [
    "CanvasPptxExportProcessor",
    "build_playwright_renderer_factory",
]
