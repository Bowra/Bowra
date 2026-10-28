"""Positive-evidence classifier for B1a-safe Canvas export copy.

Database ``contract_version`` continues to identify the export engine (1/2).
Only a completed job whose output metadata, manifest metadata, and manifest
body form the closed B1a evidence triple is projected as API version 3.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from bowhead_core._db import open_shared_ctx
from bowhead_core.artifact_store import Artifact, ArtifactStore

TRUST_COPY_CONTRACT = "B1a-v1"
EXPORT_COPY_CONTRACT_VERSION = 3
CURRENT_MANIFEST_PRODUCER = "canvas_export_manifest_b1a_v1"
MAX_COPY_MANIFEST_BYTES = 8 * 1024 * 1024
COPY_MANIFEST_CACHE_SIZE = 256
COPY_MANIFEST_CACHE_TTL_SECONDS = 60.0

CURRENT_OUTPUT_PRODUCERS: dict[tuple[str, str], str] = {
    ("legacy", "pdf"): "canvas_export_b1a_v1_pdf_legacy",
    ("legacy", "pptx"): "canvas_export_b1a_v1_pptx_legacy",
    ("legacy_clone", "pptx"): "canvas_export_b1a_v1_pptx_clone",
    ("legacy", "xlsx"): "canvas_export_b1a_v1_xlsx_legacy",
    # docx has only the legacy path: V2/durable **deliberately excludes** docx (`routes/canvas.py`
    # has no docx in `_EXPORT_PARITY_CONTRACTS`; the V2 endpoint first rejects it with
    # a clean 400 invalid_format_profile), so ("durable","docx") can never reach here;
    # adding it would be misleading dead configuration.
    ("legacy", "docx"): "canvas_export_b1a_v1_docx_legacy",
    ("durable", "pdf"): "canvas_export_b1a_v1_pdf_v2",
    ("durable", "pptx"): "canvas_export_b1a_v1_pptx_v2",
    ("durable", "xlsx"): "canvas_export_b1a_v1_xlsx_v2",
}
CURRENT_OUTPUT_PRODUCER_VALUES = frozenset(CURRENT_OUTPUT_PRODUCERS.values())

_FORMAT_METADATA = {
    "pdf": ("application/pdf", ".pdf"),
    "pptx": (
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ".pptx",
    ),
    "xlsx": (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".xlsx",
    ),
    "docx": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".docx",
    ),
}


@dataclass(frozen=True, slots=True)
class _ManifestCacheValue:
    expires_at: float
    content_sha256: str
    payload: dict[str, Any]


_manifest_cache: OrderedDict[tuple[str, int, str], _ManifestCacheValue] = OrderedDict()


@dataclass(frozen=True, slots=True)
class _ArtifactJobEvidence:
    id: str
    fmt: str
    status: str
    contract_version: int
    artifact_id: str | None
    manifest_artifact_id: str | None


def output_producer_for(*, engine: Literal["legacy", "legacy_clone", "durable"], fmt: str) -> str:
    try:
        return CURRENT_OUTPUT_PRODUCERS[(engine, fmt)]
    except KeyError as exc:
        raise ValueError("unsupported export copy producer") from exc


def current_manifest_payload_matches(payload: object, *, job_id: str, fmt: str) -> bool:
    return (
        isinstance(payload, dict)
        and payload.get("trust_copy_contract") == TRUST_COPY_CONTRACT
        and payload.get("job_id") == job_id
        and payload.get("format") == fmt
    )


def evict_export_copy_manifest(artifact_id: str | None = None) -> None:
    if artifact_id is None:
        _manifest_cache.clear()
        return
    for key in tuple(_manifest_cache):
        if key[0] == artifact_id:
            _manifest_cache.pop(key, None)


def output_metadata_matches_format(artifact: Artifact, fmt: str) -> bool:
    expected = _FORMAT_METADATA.get(fmt)
    if expected is None:
        return False
    mime, suffix = expected
    return artifact.mime == mime and Path(artifact.filename).suffix.lower() == suffix


def _engine_contract_version(value: object) -> int:
    # bool is an int subclass and stringified integers are common malformed DTO
    # inputs. Neither may promote a legacy/unknown row into a current contract.
    return value if type(value) is int and value in {1, 2} else 1


def _expected_output_producers(job: Any) -> frozenset[str]:
    fmt = getattr(job, "fmt", None)
    engine = _engine_contract_version(getattr(job, "contract_version", None))
    if engine == 1:
        values = {
            CURRENT_OUTPUT_PRODUCERS.get(("legacy", fmt)),
            CURRENT_OUTPUT_PRODUCERS.get(("legacy_clone", fmt)),
        }
    elif engine == 2:
        values = {CURRENT_OUTPUT_PRODUCERS.get(("durable", fmt))}
    else:
        values = set()
    return frozenset(value for value in values if value is not None)


async def _read_manifest_payload(
    artifact_store: ArtifactStore,
    manifest: Artifact,
) -> dict[str, Any] | None:
    if manifest.size <= 0 or manifest.size > MAX_COPY_MANIFEST_BYTES:
        return None
    key = (manifest.id, manifest.size, manifest.created_at)
    now = time.monotonic()
    cached = _manifest_cache.get(key)
    if cached is not None and cached.expires_at > now:
        _manifest_cache.move_to_end(key)
        return cached.payload
    _manifest_cache.pop(key, None)
    try:
        content = await artifact_store.open_read(manifest)
        if len(content) != manifest.size or len(content) > MAX_COPY_MANIFEST_BYTES:
            return None
        parsed = json.loads(content)
    except Exception:
        return None
    if not isinstance(parsed, dict):
        return None
    value = _ManifestCacheValue(
        expires_at=now + COPY_MANIFEST_CACHE_TTL_SECONDS,
        content_sha256=hashlib.sha256(content).hexdigest(),
        payload=parsed,
    )
    _manifest_cache[key] = value
    _manifest_cache.move_to_end(key)
    while len(_manifest_cache) > COPY_MANIFEST_CACHE_SIZE:
        _manifest_cache.popitem(last=False)
    return parsed


async def classify_export_copy_versions(
    jobs: list[Any],
    *,
    artifact_store: ArtifactStore,
) -> dict[str, int]:
    """Return API copy versions for jobs, fail-closing every anomaly to 1/2."""

    versions = {str(job.id): _engine_contract_version(getattr(job, "contract_version", None)) for job in jobs}
    candidates = [
        job
        for job in jobs
        if getattr(job, "status", None) == "done"
        and isinstance(getattr(job, "artifact_id", None), str)
        and isinstance(getattr(job, "manifest_artifact_id", None), str)
        and getattr(job, "fmt", None) in _FORMAT_METADATA
    ]
    artifact_ids = [artifact_id for job in candidates for artifact_id in (job.artifact_id, job.manifest_artifact_id)]
    try:
        artifacts = {artifact.id: artifact for artifact in await artifact_store.get_many(artifact_ids)}
    except Exception:
        return versions
    for job in candidates:
        output = artifacts.get(job.artifact_id)
        manifest = artifacts.get(job.manifest_artifact_id)
        if output is None or manifest is None:
            continue
        if (
            output.producer_kind not in _expected_output_producers(job)
            or output.manifest_artifact_id != manifest.id
            or not output_metadata_matches_format(output, job.fmt)
            or manifest.producer_kind != CURRENT_MANIFEST_PRODUCER
            or manifest.manifest_artifact_id is not None
            or manifest.mime != "application/json"
            or not manifest.filename.lower().endswith(".manifest.json")
        ):
            continue
        payload = await _read_manifest_payload(artifact_store, manifest)
        if payload is None:
            continue
        if current_manifest_payload_matches(payload, job_id=job.id, fmt=job.fmt):
            versions[str(job.id)] = EXPORT_COPY_CONTRACT_VERSION
    return versions


async def classify_export_artifact_versions(
    artifacts: list[Artifact],
    *,
    artifact_store: ArtifactStore,
) -> dict[str, int]:
    """Batch-classify export outputs for Files/Report Data surfaces."""

    artifact_ids = [artifact.id for artifact in artifacts]
    if not artifact_ids:
        return {}
    jobs: list[_ArtifactJobEvidence] = []
    async with open_shared_ctx(artifact_store.db_path) as db:
        for start in range(0, len(artifact_ids), 400):
            chunk = artifact_ids[start : start + 400]
            placeholders = ",".join("?" for _ in chunk)
            try:
                cursor = await db.execute(
                    "SELECT id,fmt,status,contract_version,artifact_id,manifest_artifact_id "
                    f"FROM canvas_export_jobs WHERE artifact_id IN ({placeholders})",  # noqa: S608
                    chunk,
                )
            except Exception:
                return {}
            for row in await cursor.fetchall():
                jobs.append(
                    _ArtifactJobEvidence(
                        id=str(row[0]),
                        fmt=str(row[1]),
                        status=str(row[2]),
                        contract_version=_engine_contract_version(row[3]),
                        artifact_id=str(row[4]) if row[4] is not None else None,
                        manifest_artifact_id=str(row[5]) if row[5] is not None else None,
                    )
                )
    jobs_by_artifact: dict[str, list[_ArtifactJobEvidence]] = {}
    for job in jobs:
        if job.artifact_id is not None:
            jobs_by_artifact.setdefault(job.artifact_id, []).append(job)
    unique_jobs = [group[0] for group in jobs_by_artifact.values() if len(group) == 1]
    versions = await classify_export_copy_versions(unique_jobs, artifact_store=artifact_store)
    return {job.artifact_id: versions[job.id] for job in unique_jobs if job.artifact_id is not None and job.id in versions}


__all__ = [
    "CURRENT_MANIFEST_PRODUCER",
    "CURRENT_OUTPUT_PRODUCERS",
    "CURRENT_OUTPUT_PRODUCER_VALUES",
    "EXPORT_COPY_CONTRACT_VERSION",
    "MAX_COPY_MANIFEST_BYTES",
    "TRUST_COPY_CONTRACT",
    "classify_export_copy_versions",
    "classify_export_artifact_versions",
    "current_manifest_payload_matches",
    "evict_export_copy_manifest",
    "output_metadata_matches_format",
    "output_producer_for",
]
