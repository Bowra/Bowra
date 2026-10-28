"""Read-only KB health observations. These facts do not authorize AI use.

Batch 2 deliberately has no generation writer, hash backfill or eligibility gate.
Legacy chunk counts and successful HEAD requests cannot prove source binding.
"""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal, Protocol

import anyio
from pydantic import BaseModel

from bowhead_core.artifact_reconcile import HEALTH_MIN_PRESENT_RATIO, HEALTH_SAMPLE

if TYPE_CHECKING:
    from bowhead_core.artifact_store import ArtifactBackend
    from bowhead_core.kb.schemas import KbFileDTO


def health_observation_enabled() -> bool:
    return os.environ.get("FEATURE_KB_TRUST_V2", "0").strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class SourceRecord:
    path: str
    status: str


class IndexObservation(BaseModel):
    state: Literal["unknown", "processing", "missing_or_corrupt"] = "unknown"
    reason: Literal[
        "not_checked",
        "processing",
        "index_unavailable",
        "collection_missing",
        "chunk_count_mismatch",
        "empty_index",
        "sample_unreadable",
        "legacy_chunks_readable",
        "inspection_limit",
    ] = "not_checked"
    observed_chunks: int | None = None
    count_limited: bool = False


class KbHealthObservation(BaseModel):
    file_id: str
    file_version: int
    checked_at: str
    source_health: Literal["present", "confirmed_absent", "temporarily_unavailable", "unchecked"]
    source_reason: Literal[
        "head_present",
        "head_absent",
        "storage_unverified",
        "probe_failed",
        "artifact_unavailable",
        "upload_pending",
    ]
    index: IndexObservation
    binding_state: Literal["unverified", "verified"] = "unverified"
    evidence_state: Literal["unknown"] = "unknown"
    # Never return eligible/paused from an observation-only implementation.
    eligibility_enforced: bool = False
    use_state: Literal["eligible", "warning", "paused"] | None = None
    use_reason: str | None = None
    generation_id: str | None = None
    primary_action: Literal["check", "restore", "rebuild"] = "check"


class IndexReader(Protocol):
    async def inspect_active_index(self, file: KbFileDTO, *, limit: int = 1000) -> IndexObservation: ...


async def bounded_blob_probe(backend: ArtifactBackend, path: str, *, timeout: float = 3.0) -> str:
    try:
        with anyio.fail_after(timeout):
            from bowhead_core.kb.source_integrity import source_head

            _, head = await source_head(backend, path)
            return "present" if head is not None else "absent"
    except Exception:  # Storage errors never prove absence.
        return "unknown"


async def check_storage_health(backend: ArtifactBackend, paths: Sequence[str], *, timeout: float = 3.0) -> bool:
    """Reuse artifact probe/threshold, but no samples means no proof of health."""
    paths = paths[:HEALTH_SAMPLE]
    if not paths:
        return False
    results: list[str] = []
    limiter = anyio.CapacityLimiter(4)

    async def check(path: str) -> None:
        async with limiter:
            results.append(await bounded_blob_probe(backend, path, timeout=timeout))

    async with anyio.create_task_group() as group:
        for path in paths:
            group.start_soon(check, path)
    return sum(state == "present" for state in results) / len(paths) >= HEALTH_MIN_PRESENT_RATIO


async def inspect_file_health(
    file: KbFileDTO,
    *,
    source: SourceRecord | None,
    backend: ArtifactBackend,
    storage_health: Callable[[], Awaitable[bool]],
    index_reader: IndexReader | None = None,
    timeout: float = 3.0,
) -> KbHealthObservation:
    source_state = "unchecked"
    source_reason = "artifact_unavailable"
    if source and source.status == "pending_upload":
        source_reason = "upload_pending"
    elif source and source.path and source.status in {"ready", "missing"}:
        state = await bounded_blob_probe(backend, source.path, timeout=timeout)
        if state == "present":
            source_state, source_reason = "present", "head_present"
        elif state == "unknown":
            source_state, source_reason = "temporarily_unavailable", "probe_failed"
        else:
            try:
                with anyio.fail_after(timeout * 2):
                    healthy = await storage_health()
            except Exception:  # noqa: BLE001 - no proof of storage health, never assert absence
                healthy = False
            source_state = "confirmed_absent" if healthy else "unchecked"
            source_reason = "head_absent" if healthy else "storage_unverified"

    index = IndexObservation()
    if file.parse_status in {"pending", "queued", "awaiting_upload", "parsing", "chunking", "embedding"}:
        index = IndexObservation(state="processing", reason="processing")
    elif index_reader is not None:
        try:
            with anyio.fail_after(timeout):
                index = await index_reader.inspect_active_index(file)
        except Exception:  # noqa: BLE001 - sanitized diagnostic, no backend paths or raw errors
            index = IndexObservation(reason="index_unavailable")
    return KbHealthObservation(
        file_id=file.file_id,
        file_version=file.version,
        checked_at=datetime.now(UTC).isoformat(),
        source_health=source_state,
        source_reason=source_reason,
        index=index,
    )
