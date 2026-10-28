"""Snapshot-scoped artifact references, metadata pins, and byte resolution."""

from __future__ import annotations

import csv
import hashlib
import io
import math
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiosqlite

from bowhead_core._db import open_shared_ctx
from bowhead_core.error_catalog import CodedError

if TYPE_CHECKING:
    from collections.abc import Sequence

    from bowhead_core.artifact_store import ArtifactStore
    from bowhead_core.export_snapshot import ExportSnapshot

MAX_SNAPSHOT_ASSET_BYTES = 50 * 1024 * 1024
MAX_SNAPSHOT_ASSETS_TOTAL_BYTES = 150 * 1024 * 1024
MAX_RESOLVED_CHART_ROWS = 100_000
MAX_RESOLVED_CHART_COLUMNS = 50
MAX_RESOLVED_CHART_CELLS = 500_000

_ASSET_PIN_SCHEMA = """
CREATE TABLE IF NOT EXISTS canvas_export_snapshot_assets (
    snapshot_id       TEXT NOT NULL,
    artifact_id       TEXT NOT NULL,
    section_id        TEXT NOT NULL,
    role              TEXT NOT NULL,
    mime              TEXT NOT NULL,
    size_bytes        INTEGER NOT NULL,
    content_sha256    TEXT,
    created_by        TEXT,
    scope             TEXT NOT NULL CHECK(scope IN ('session', 'user')),
    scope_session_id  TEXT,
    pinned_at         TEXT NOT NULL,
    resolved_at       TEXT,
    PRIMARY KEY(snapshot_id, artifact_id, section_id, role),
    CHECK((scope = 'session' AND scope_session_id IS NOT NULL)
       OR (scope = 'user' AND scope_session_id IS NULL)),
    FOREIGN KEY(snapshot_id) REFERENCES canvas_export_snapshots(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_export_snapshot_assets_artifact
ON canvas_export_snapshot_assets(artifact_id, snapshot_id);
"""

_IMAGE_ROLES = frozenset({"image", "design_background", "design_flattened"})
_REQUIRED_ROLES = _IMAGE_ROLES | frozenset({"chart_data"})
_ARTIFACT_QUERY_CHUNK = 400


class ExportAssetError(CodedError):
    """Stable, non-enumerating error raised by snapshot asset handling."""


@dataclass(frozen=True, slots=True)
class SnapshotAssetRef:
    artifact_id: str
    section_id: str
    role: str
    required: bool


@dataclass(frozen=True, slots=True)
class DesignFlattenExpectation:
    artifact_id: str
    canvas_id: str
    section_id: str
    section_rev: int
    size_preset: str
    background_artifact_id: str


@dataclass(frozen=True, slots=True)
class SnapshotAssetPin:
    snapshot_id: str
    artifact_id: str
    section_id: str
    role: str
    mime: str
    size_bytes: int
    content_sha256: str | None
    created_by: str | None
    scope: str
    scope_session_id: str | None
    pinned_at: str
    resolved_at: str | None


@dataclass(frozen=True, slots=True)
class ResolvedSnapshotAsset:
    artifact_id: str
    content_sha256: str
    size_bytes: int
    mime: str
    relative_path: str | None = None


@dataclass(frozen=True, slots=True)
class AssetResolutionWarning:
    section_id: str
    role: str
    code: str


@dataclass(frozen=True, slots=True)
class SnapshotAssetResolution:
    resolved: tuple[ResolvedSnapshotAsset, ...]
    warnings: tuple[AssetResolutionWarning, ...]


async def initialize_snapshot_asset_schema(db: aiosqlite.Connection) -> None:
    await db.executescript(_ASSET_PIN_SCHEMA)


def extract_snapshot_asset_refs(
    frozen_sections: Sequence[dict[str, Any]],
) -> tuple[SnapshotAssetRef, ...]:
    refs: dict[tuple[str, str, str], SnapshotAssetRef] = {}
    for section in frozen_sections:
        _extract_section_refs(section, refs)
    ordered = tuple(sorted(refs.values(), key=lambda ref: (ref.section_id, ref.role, ref.artifact_id)))
    if any(ref.required != (ref.role in _REQUIRED_ROLES) for ref in ordered):
        raise RuntimeError("snapshot asset role registry disagrees with extraction policy")
    return ordered


async def validate_asset_pins(
    db: aiosqlite.Connection,
    *,
    refs: Sequence[SnapshotAssetRef],
    requested_by: str,
    session_id: str,
    pinned_at: str,
    design_flatten_expectations: Sequence[DesignFlattenExpectation] = (),
) -> tuple[SnapshotAssetPin, ...]:
    """Validate active metadata and scope while the caller holds BEGIN IMMEDIATE."""
    if not refs:
        return ()
    artifact_ids = sorted({ref.artifact_id for ref in refs})
    metadata: dict[str, aiosqlite.Row] = {}
    try:
        for start in range(0, len(artifact_ids), _ARTIFACT_QUERY_CHUNK):
            chunk = artifact_ids[start : start + _ARTIFACT_QUERY_CHUNK]
            placeholders = ",".join("?" for _ in chunk)
            async with db.execute(
                "SELECT a.id, a.mime, a.size, a.created_by, a.scope, a.deleted_at, "
                "a.status, "
                "CASE WHEN EXISTS("
                "  SELECT 1 FROM session_attachments sa "
                "  WHERE sa.artifact_id=a.id AND sa.session_id=? AND sa.detached_at IS NULL"
                ") OR EXISTS("
                "  SELECT 1 FROM turn_attachments ta JOIN turns t ON t.id=ta.turn_id "
                "  WHERE ta.artifact_id=a.id AND t.session_id=?"
                ") OR EXISTS("
                "  SELECT 1 FROM canvases c "
                "  WHERE c.id=a.canvas_id AND c.session_id=?"
                ") THEN 1 ELSE 0 END AS in_session "
                f"FROM artifacts a WHERE a.id IN ({placeholders})",
                (session_id, session_id, session_id, *chunk),
            ) as cursor:
                for row in await cursor.fetchall():
                    metadata[str(row["id"])] = row
    except aiosqlite.OperationalError as exc:
        raise ExportAssetError("asset_missing_or_forbidden") from exc

    await _validate_design_flatten_provenance(
        db,
        refs=refs,
        expectations=design_flatten_expectations,
    )

    pins: list[SnapshotAssetPin] = []
    unique_sizes: dict[str, int] = {}
    for ref in refs:
        metadata_row = metadata.get(ref.artifact_id)
        if metadata_row is None or not _metadata_is_authorized(
            metadata_row,
            requested_by=requested_by,
        ):
            # Required assets (images / chart data) must resolve or the export is
            # incomplete → fail closed. Optional assets (source_attachment,
            # code_input/output, insight_computation/table, html/data_table),
            # whose backing reference may be a tool_call id with no artifact row
            # or a soft-deleted blob, are skipped here and surface downstream as
            # optional-asset warnings — matching resolve_snapshot_assets instead
            # of failing the whole export. `ref.required == role in _REQUIRED_ROLES`.
            if ref.required:
                raise ExportAssetError("asset_missing_or_forbidden")
            continue
        mime = str(metadata_row["mime"] or "")
        size_bytes = int(metadata_row["size"])
        scope = str(metadata_row["scope"])
        if not _mime_is_allowed(ref.role, mime) or (scope == "session" and not bool(metadata_row["in_session"])):
            if ref.required:
                raise ExportAssetError("asset_missing_or_forbidden")
            continue
        if size_bytes < 0 or size_bytes > MAX_SNAPSHOT_ASSET_BYTES:
            raise ExportAssetError(
                "asset_too_large",
                limit_bytes=MAX_SNAPSHOT_ASSET_BYTES,
            )
        unique_sizes[ref.artifact_id] = size_bytes
        pins.append(
            SnapshotAssetPin(
                snapshot_id="",
                artifact_id=ref.artifact_id,
                section_id=ref.section_id,
                role=ref.role,
                mime=mime,
                size_bytes=size_bytes,
                content_sha256=None,
                created_by=str(metadata_row["created_by"]),
                scope=scope,
                scope_session_id=session_id if scope == "session" else None,
                pinned_at=pinned_at,
                resolved_at=None,
            )
        )
    total_bytes = sum(unique_sizes.values())
    if total_bytes > MAX_SNAPSHOT_ASSETS_TOTAL_BYTES:
        raise ExportAssetError(
            "snapshot_assets_too_large",
            limit_bytes=MAX_SNAPSHOT_ASSETS_TOTAL_BYTES,
        )
    return tuple(pins)


def extract_design_flatten_expectations(
    frozen_sections: Sequence[dict[str, Any]],
    *,
    canvas_id: str,
) -> tuple[DesignFlattenExpectation, ...]:
    expectations: list[DesignFlattenExpectation] = []
    pending = list(frozen_sections)
    while pending:
        section = pending.pop()
        payload = section.get("payload")
        if not isinstance(payload, dict):
            payload = section
        if section.get("kind") == "design":
            artifact_id = _fresh_design_flatten_artifact_id(
                section=section,
                payload=payload,
            )
            section_id = section.get("id")
            section_rev = section.get("rev")
            size_preset = payload.get("size_preset")
            background = payload.get("background")
            background_artifact_id = background.get("artifact_id") if isinstance(background, dict) else None
            if isinstance(artifact_id, str) and artifact_id:
                if (
                    not isinstance(section_id, str)
                    or not isinstance(section_rev, int)
                    or isinstance(section_rev, bool)
                    or not isinstance(size_preset, str)
                    or not isinstance(background_artifact_id, str)
                ):
                    raise ExportAssetError("asset_missing_or_forbidden")
                expectations.append(
                    DesignFlattenExpectation(
                        artifact_id=artifact_id,
                        canvas_id=canvas_id,
                        section_id=section_id,
                        section_rev=section_rev,
                        size_preset=size_preset,
                        background_artifact_id=background_artifact_id,
                    )
                )
        body = payload.get("body")
        if isinstance(body, list):
            pending.extend(child for child in body if isinstance(child, dict))
    return tuple(
        sorted(
            expectations,
            key=lambda item: (item.section_id, item.artifact_id),
        )
    )


async def _validate_design_flatten_provenance(
    db: aiosqlite.Connection,
    *,
    refs: Sequence[SnapshotAssetRef],
    expectations: Sequence[DesignFlattenExpectation],
) -> None:
    flattened_refs = {ref.artifact_id: ref for ref in refs if ref.role == "design_flattened"}
    if not flattened_refs:
        return
    expected_by_artifact = {item.artifact_id: item for item in expectations}
    if (
        len(expected_by_artifact) != len(expectations)
        or len(flattened_refs) != sum(ref.role == "design_flattened" for ref in refs)
        or expected_by_artifact.keys() != flattened_refs.keys()
    ):
        raise ExportAssetError("asset_missing_or_forbidden")
    placeholders = ",".join("?" for _ in expected_by_artifact)
    try:
        async with db.execute(
            "SELECT artifact_id, canvas_id, section_id, section_rev, "
            "size_preset, background_artifact_id "
            "FROM design_flatten_exports "
            f"WHERE artifact_id IN ({placeholders})",
            tuple(sorted(expected_by_artifact)),
        ) as cursor:
            rows = await cursor.fetchall()
    except aiosqlite.OperationalError as exc:
        raise ExportAssetError("asset_missing_or_forbidden") from exc
    actual = {str(row["artifact_id"]): row for row in rows}
    for artifact_id, expectation in expected_by_artifact.items():
        row = actual.get(artifact_id)
        if row is None or not (
            str(row["canvas_id"]) == expectation.canvas_id
            and str(row["section_id"]) == expectation.section_id
            and int(row["section_rev"]) == expectation.section_rev - 1
            and str(row["size_preset"]) == expectation.size_preset
            and str(row["background_artifact_id"]) == expectation.background_artifact_id
        ):
            raise ExportAssetError("asset_missing_or_forbidden")


async def insert_asset_pins(
    db: aiosqlite.Connection,
    *,
    snapshot_id: str,
    pins: Sequence[SnapshotAssetPin],
) -> None:
    if not pins:
        return
    await db.executemany(
        "INSERT INTO canvas_export_snapshot_assets("
        "snapshot_id, artifact_id, section_id, role, mime, size_bytes, "
        "content_sha256, created_by, scope, scope_session_id, pinned_at, resolved_at"
        ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (
                snapshot_id,
                pin.artifact_id,
                pin.section_id,
                pin.role,
                pin.mime,
                pin.size_bytes,
                pin.content_sha256,
                pin.created_by,
                pin.scope,
                pin.scope_session_id,
                pin.pinned_at,
                pin.resolved_at,
            )
            for pin in pins
        ],
    )


async def list_snapshot_asset_pins(
    db_path: str,
    snapshot_id: str,
) -> tuple[SnapshotAssetPin, ...]:
    async with open_shared_ctx(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM canvas_export_snapshot_assets WHERE snapshot_id=? ORDER BY section_id, role, artifact_id",
            (snapshot_id,),
        ) as cursor:
            rows = await cursor.fetchall()
    return tuple(_row_to_pin(row) for row in rows)


async def resolve_snapshot_assets(
    *,
    db_path: str,
    snapshot_id: str,
    artifact_store: ArtifactStore,
    attempt_dir: Path | None = None,
) -> SnapshotAssetResolution:
    """Resolve only pre-authorized pins, allowing post-click soft deletion."""
    await _ensure_snapshot_resolvable(db_path, snapshot_id)
    pins = await list_snapshot_asset_pins(db_path, snapshot_id)
    by_artifact: dict[str, list[SnapshotAssetPin]] = {}
    for pin in pins:
        by_artifact.setdefault(pin.artifact_id, []).append(pin)

    resolved: list[ResolvedSnapshotAsset] = []
    warnings: list[AssetResolutionWarning] = []
    for artifact_id, artifact_pins in by_artifact.items():
        required = any(pin.role in _REQUIRED_ROLES for pin in artifact_pins)
        first = artifact_pins[0]
        try:
            artifact = await artifact_store.get(
                artifact_id,
                include_deleted=True,
                check_created_by=first.created_by,
                reject_null_owner=True,
            )
            if artifact is None or not _artifact_matches_pin(artifact, first):
                raise ExportAssetError("asset_missing_or_forbidden")
            content = await artifact_store.open_read(artifact)
            if len(content) != first.size_bytes:
                raise ExportAssetError("asset_content_changed")
        except Exception as exc:
            if required:
                if isinstance(exc, ExportAssetError):
                    raise
                raise ExportAssetError("asset_missing_or_forbidden") from exc
            warnings.extend(
                AssetResolutionWarning(
                    section_id=pin.section_id,
                    role=pin.role,
                    code="optional_asset_unavailable",
                )
                for pin in artifact_pins
            )
            continue

        digest = f"sha256:{hashlib.sha256(content).hexdigest()}"
        await _mark_asset_resolved(
            db_path=db_path,
            snapshot_id=snapshot_id,
            artifact_id=artifact_id,
            content_sha256=digest,
        )
        resolved.append(
            ResolvedSnapshotAsset(
                artifact_id=artifact_id,
                content_sha256=digest,
                size_bytes=len(content),
                mime=first.mime,
                relative_path=(
                    _write_attempt_asset(
                        attempt_dir=attempt_dir,
                        artifact_id=artifact_id,
                        mime=first.mime,
                        content=content,
                    )
                    if attempt_dir is not None
                    else None
                ),
            )
        )
    return SnapshotAssetResolution(
        resolved=tuple(resolved),
        warnings=tuple(warnings),
    )


def _write_attempt_asset(
    *,
    attempt_dir: Path,
    artifact_id: str,
    mime: str,
    content: bytes,
) -> str:
    attempt_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    if attempt_dir.is_symlink() or not stat.S_ISDIR(attempt_dir.stat().st_mode):
        raise ExportAssetError("asset_staging_invalid")
    assets_dir = attempt_dir / "assets"
    assets_dir.mkdir(mode=0o700, exist_ok=True)
    if assets_dir.is_symlink() or not stat.S_ISDIR(assets_dir.stat().st_mode):
        raise ExportAssetError("asset_staging_invalid")
    extension = {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/webp": ".webp",
        "image/gif": ".gif",
        "text/html": ".html",
        "application/xhtml+xml": ".xhtml",
        "text/csv": ".csv",
        "application/csv": ".csv",
    }.get(mime.lower(), ".bin")
    safe_name = f"{hashlib.sha256(artifact_id.encode('utf-8')).hexdigest()[:24]}{extension}"
    target = assets_dir / safe_name
    try:
        with target.open("xb") as stream:
            stream.write(content)
    except FileExistsError as exc:
        raise ExportAssetError("asset_staging_conflict") from exc
    return (Path("assets") / safe_name).as_posix()


def load_resolved_chart_rows(
    *,
    snapshot: ExportSnapshot,
    asset_resolution: SnapshotAssetResolution,
    attempt_dir: Path,
) -> dict[str, list[dict[str, Any]]]:
    """Parse full chart CSVs from immutable attempt-local asset copies."""
    expected_counts = _chart_data_expected_counts(snapshot)
    if not expected_counts:
        return {}
    resolved = {asset.artifact_id: asset for asset in asset_resolution.resolved}
    rows_by_artifact: dict[str, list[dict[str, Any]]] = {}
    for artifact_id, declared_counts in expected_counts.items():
        asset = resolved.get(artifact_id)
        if asset is None or asset.mime.lower() not in {"text/csv", "application/csv"} or asset.relative_path is None:
            raise ExportAssetError("chart_data_invalid")
        relative_path = Path(asset.relative_path)
        if relative_path.is_absolute() or ".." in relative_path.parts or not relative_path.parts or relative_path.parts[0] != "assets":
            raise ExportAssetError("chart_data_invalid")
        source_path = attempt_dir / relative_path
        if source_path.is_symlink() or not source_path.is_file() or source_path.stat().st_size != asset.size_bytes:
            raise ExportAssetError("chart_data_invalid")
        content = source_path.read_bytes()
        digest = f"sha256:{hashlib.sha256(content).hexdigest()}"
        if digest != asset.content_sha256:
            raise ExportAssetError("asset_content_changed")
        rows = _parse_chart_csv(content)
        # spec.row_count is the chart's *plotted* row count -- often a Top-N /
        # preview subset -- while data_artifact_id references the FULL query
        # result the data workbook must export. So declared < len(rows) is
        # normal and correct (e.g. a Top-10 category chart over 35 rows); the
        # earlier `!=` invariant wrongly rejected full data and rewarded the
        # very preview-truncation this path exists to avoid. Only a declared
        # count that EXCEEDS the available rows is a genuine integrity anomaly;
        # exact content parity with the pinned artifact is already enforced by
        # the sha256 digest check above.
        if any(count > len(rows) for count in declared_counts):
            raise ExportAssetError("chart_data_row_count_mismatch")
        rows_by_artifact[artifact_id] = rows
    return rows_by_artifact


def _chart_data_expected_counts(
    snapshot: ExportSnapshot,
) -> dict[str, set[int]]:
    sections = snapshot.payload.get("sections")
    pending = list(sections) if isinstance(sections, list) else []
    expected: dict[str, set[int]] = {}
    while pending:
        section = pending.pop()
        if not isinstance(section, dict):
            continue
        raw_payload = section.get("payload")
        payload = raw_payload if isinstance(raw_payload, dict) else section
        if section.get("kind") == "chart":
            spec = payload.get("spec")
            artifact_id = spec.get("data_artifact_id") if isinstance(spec, dict) else None
            row_count = spec.get("row_count") if isinstance(spec, dict) else None
            if isinstance(artifact_id, str) and artifact_id:
                counts = expected.setdefault(artifact_id, set())
                if isinstance(row_count, int) and not isinstance(row_count, bool) and row_count >= 0:
                    counts.add(row_count)
        body = payload.get("body")
        if isinstance(body, list):
            pending.extend(body)
    return expected


def _parse_chart_csv(
    content: bytes,
) -> list[dict[str, Any]]:
    try:
        text = content.decode("utf-8-sig")
        reader = csv.DictReader(
            io.StringIO(text),
            strict=True,
        )
        fieldnames = list(reader.fieldnames or [])
        if (
            not fieldnames
            or len(fieldnames) > MAX_RESOLVED_CHART_COLUMNS
            or any(not field for field in fieldnames)
            or len(fieldnames) != len(set(fieldnames))
        ):
            raise ExportAssetError("chart_data_invalid")
        rows: list[dict[str, Any]] = []
        for raw_row in reader:
            if None in raw_row:
                raise ExportAssetError("chart_data_invalid")
            row = {field: _chart_scalar(raw_row.get(field)) for field in fieldnames}
            rows.append(row)
            if len(rows) > MAX_RESOLVED_CHART_ROWS or len(rows) * len(fieldnames) > MAX_RESOLVED_CHART_CELLS:
                raise ExportAssetError("chart_data_too_large")
        return rows
    except (csv.Error, UnicodeDecodeError) as exc:
        raise ExportAssetError("chart_data_invalid") from exc


def _chart_scalar(value: str | None) -> Any:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return value
    if not math.isfinite(number):
        raise ExportAssetError("chart_data_invalid")
    if "." not in value and "e" not in value.lower():
        return int(number)
    return number


def _extract_section_refs(
    section: dict[str, Any],
    refs: dict[tuple[str, str, str], SnapshotAssetRef],
) -> None:
    section_id = section.get("id")
    kind = section.get("kind")
    if not isinstance(section_id, str) or not isinstance(kind, str):
        return
    raw_payload = section.get("payload")
    payload: dict[str, Any] = raw_payload if isinstance(raw_payload, dict) else section
    if kind == "image":
        _add_ref(refs, payload.get("artifact_id"), section_id, "image", required=True)
    elif kind == "design":
        background = payload.get("background")
        if isinstance(background, dict):
            _add_ref(refs, background.get("artifact_id"), section_id, "design_background", required=True)
        _add_ref(
            refs,
            _fresh_design_flatten_artifact_id(
                section=section,
                payload=payload,
            ),
            section_id,
            "design_flattened",
            required=True,
        )
    elif kind == "chart":
        spec = payload.get("spec")
        if isinstance(spec, dict):
            _add_ref(refs, spec.get("data_artifact_id"), section_id, "chart_data", required=True)
    elif kind == "html_artifact":
        _add_ref(refs, payload.get("artifact_id"), section_id, "html_source", required=False)
    elif kind == "data_table":
        meta = payload.get("meta")
        if isinstance(meta, dict):
            _add_ref(refs, meta.get("artifact_id"), section_id, "data_table_source", required=False)
    elif kind == "code_block":
        for key, role in (
            ("input_artifact_ids", "code_input"),
            ("output_artifact_ids", "code_output"),
            ("input_artifacts", "code_input"),
            ("output_artifacts", "code_output"),
        ):
            _add_refs_from_collection(refs, payload.get(key), section_id, role)
    elif kind == "sources":
        _add_refs_from_collection(refs, payload.get("entries"), section_id, "source_attachment")
    elif kind == "insight_card":
        evidence = payload.get("evidence")
        if isinstance(evidence, list):
            for item in evidence:
                if not isinstance(item, dict):
                    continue
                _add_ref(refs, item.get("computation_artifact_id"), section_id, "insight_computation", required=False)
                _add_ref(refs, item.get("table_artifact_id"), section_id, "insight_table", required=False)
    if kind == "section":
        body = payload.get("body")
        if isinstance(body, list):
            for nested in body:
                if isinstance(nested, dict):
                    _extract_section_refs(nested, refs)


def _fresh_design_flatten_artifact_id(
    *,
    section: dict[str, Any],
    payload: dict[str, Any],
) -> str | None:
    artifact_id = payload.get("flattened_artifact_id")
    section_rev = section.get("rev")
    flattened_source_rev = payload.get("flattened_source_rev")
    if (
        isinstance(artifact_id, str)
        and artifact_id
        and isinstance(section_rev, int)
        and not isinstance(section_rev, bool)
        and isinstance(flattened_source_rev, int)
        and not isinstance(flattened_source_rev, bool)
        and flattened_source_rev == section_rev - 1
    ):
        return artifact_id
    return None


def _add_refs_from_collection(
    refs: dict[tuple[str, str, str], SnapshotAssetRef],
    value: Any,
    section_id: str,
    role: str,
) -> None:
    if not isinstance(value, list):
        return
    for item in value:
        artifact_id = item.get("artifact_id") if isinstance(item, dict) else item
        _add_ref(refs, artifact_id, section_id, role, required=False)


def _add_ref(
    refs: dict[tuple[str, str, str], SnapshotAssetRef],
    value: Any,
    section_id: str,
    role: str,
    *,
    required: bool,
) -> None:
    if not isinstance(value, str) or not value:
        return
    ref = SnapshotAssetRef(
        artifact_id=value,
        section_id=section_id,
        role=role,
        required=required,
    )
    refs[(value, section_id, role)] = ref


def _metadata_is_authorized(
    row: aiosqlite.Row,
    *,
    requested_by: str,
) -> bool:
    return bool(row["deleted_at"] is None and row["status"] == "ready" and row["created_by"] == requested_by and row["scope"] in {"session", "user"})


def _mime_is_allowed(role: str, mime: str) -> bool:
    if not mime:
        return False
    if role in _IMAGE_ROLES:
        return mime.lower() in {"image/png", "image/jpeg", "image/webp", "image/gif"}
    if role == "html_source":
        return mime.lower() in {"text/html", "application/xhtml+xml"}
    return True


def _artifact_matches_pin(artifact: Any, pin: SnapshotAssetPin) -> bool:
    return bool(
        artifact.id == pin.artifact_id
        and artifact.created_by == pin.created_by
        and artifact.scope == pin.scope
        and artifact.mime == pin.mime
        and artifact.size == pin.size_bytes
    )


async def _mark_asset_resolved(
    *,
    db_path: str,
    snapshot_id: str,
    artifact_id: str,
    content_sha256: str,
) -> None:
    resolved_at = datetime.now(tz=UTC).isoformat(timespec="seconds")
    async with open_shared_ctx(db_path) as db:
        async with db.execute(
            "SELECT DISTINCT content_sha256 FROM canvas_export_snapshot_assets WHERE snapshot_id=? AND artifact_id=? AND content_sha256 IS NOT NULL",
            (snapshot_id, artifact_id),
        ) as cursor:
            existing = {str(row[0]) for row in await cursor.fetchall()}
        if existing and existing != {content_sha256}:
            raise ExportAssetError("asset_content_changed")
        await db.execute(
            "UPDATE canvas_export_snapshot_assets SET content_sha256=?, resolved_at=? WHERE snapshot_id=? AND artifact_id=?",
            (content_sha256, resolved_at, snapshot_id, artifact_id),
        )
        await db.commit()


async def _ensure_snapshot_resolvable(db_path: str, snapshot_id: str) -> None:
    async with (
        open_shared_ctx(db_path) as db,
        db.execute(
            "SELECT expires_at FROM canvas_export_snapshots WHERE id=?",
            (snapshot_id,),
        ) as cursor,
    ):
        row = await cursor.fetchone()
    if row is None:
        raise ExportAssetError("snapshot_not_found")
    expires_at = datetime.fromisoformat(str(row[0]))
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if expires_at <= datetime.now(tz=UTC):
        raise ExportAssetError("snapshot_expired")


def _row_to_pin(row: aiosqlite.Row) -> SnapshotAssetPin:
    return SnapshotAssetPin(
        snapshot_id=row["snapshot_id"],
        artifact_id=row["artifact_id"],
        section_id=row["section_id"],
        role=row["role"],
        mime=row["mime"],
        size_bytes=int(row["size_bytes"]),
        content_sha256=row["content_sha256"],
        created_by=row["created_by"],
        scope=row["scope"],
        scope_session_id=row["scope_session_id"],
        pinned_at=row["pinned_at"],
        resolved_at=row["resolved_at"],
    )


__all__ = [
    "AssetResolutionWarning",
    "DesignFlattenExpectation",
    "ExportAssetError",
    "MAX_SNAPSHOT_ASSET_BYTES",
    "MAX_SNAPSHOT_ASSETS_TOTAL_BYTES",
    "ResolvedSnapshotAsset",
    "SnapshotAssetPin",
    "SnapshotAssetRef",
    "SnapshotAssetResolution",
    "extract_design_flatten_expectations",
    "extract_snapshot_asset_refs",
    "initialize_snapshot_asset_schema",
    "insert_asset_pins",
    "list_snapshot_asset_pins",
    "load_resolved_chart_rows",
    "resolve_snapshot_assets",
    "validate_asset_pins",
]
