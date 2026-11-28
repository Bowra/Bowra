"""Shared source/index gate for new KB use. Authorization precedes all I/O."""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

import anyio

from bowhead_core.kb.generations import KbGenerationStore, now_iso
from bowhead_core.kb.health import check_storage_health
from bowhead_core.kb.knowledge_base_access import named_knowledge_base_id
from bowhead_core.kb.permission import KbPermission, ScopeContext
from bowhead_core.kb.schemas import KbFileDTO
from bowhead_core.kb.source_integrity import full_source_hash, source_head

_LIMITER: anyio.lowlevel.RunVar[anyio.CapacityLimiter] = anyio.lowlevel.RunVar("kb_source_checks")

FileAuthorizer = Callable[[KbFileDTO], Awaitable[bool]]
#: Resolves the caller's knowledge base levels in the check's context; None when unavailable.
LevelResolver = Callable[[], Awaitable[Mapping[str, int] | None]]


@dataclass(frozen=True)
class KbUseDecision:
    state: str
    reason: str
    generation_id: str | None
    checked_at: str

    @property
    def allowed(self) -> bool:
        return self.state in {"eligible", "warning"}

    @property
    def warning(self) -> str | None:
        return self.reason if self.state == "warning" else None


class KbUseEligibility:
    def __init__(self, files: Any, artifacts: Any, index: Any) -> None:
        self.files, self.artifacts, self.index = files, artifacts, index
        self.generations = KbGenerationStore(files._db_path)

    async def check(
        self,
        file_id: str,
        scope: ScopeContext,
        authorize: FileAuthorizer,
        *,
        expected_generation: str | None = None,
        resolve_levels: LevelResolver | None = None,
    ) -> KbUseDecision:
        """Decide one file for new use.

        ``resolve_levels`` supplies the caller's knowledge base levels when ``scope`` carries none
        and the file is in a named knowledge base; it runs at most once per check. Without it such
        files are refused (fail-closed).
        """
        try:
            limiter = _LIMITER.get()
        except LookupError:
            limiter = anyio.CapacityLimiter(4)
            _LIMITER.set(limiter)
        try:
            with anyio.fail_after(3):
                await limiter.acquire()
        except TimeoutError:
            return KbUseDecision("paused", "source_check_busy", expected_generation, now_iso())
        try:
            result = await self._check(file_id, scope, authorize, expected_generation=expected_generation, resolve_levels=resolve_levels)
            from bowhead_core import metrics

            metrics.inc("kb.use_check", labels={"state": result.state, "reason": result.reason})
            return result
        finally:
            limiter.release()

    async def _check(
        self,
        file_id: str,
        scope: ScopeContext,
        authorize: FileAuthorizer,
        *,
        expected_generation: str | None = None,
        resolve_levels: LevelResolver | None = None,
    ) -> KbUseDecision:
        def decision(state: str, reason: str, generation_id: str | None = None) -> KbUseDecision:
            return KbUseDecision(state, reason, generation_id, now_iso())

        async def scoped(current: KbFileDTO) -> ScopeContext:
            nonlocal scope, resolve_levels
            if resolve_levels is not None and scope.knowledge_levels is None and named_knowledge_base_id(current):
                levels, resolve_levels = await resolve_levels(), None
                if levels is not None:
                    scope = dataclasses.replace(scope, knowledge_levels=levels)
            return scope

        file = await self.files.get(file_id)
        if file is not None:
            scope = await scoped(file)
        if not file or file.soft_deleted_at or file.team_id != scope.team_id or not KbPermission.can_read(file, scope) or not await authorize(file):
            return decision("paused", "permission_or_file_unavailable")
        generation_id = file.active_generation_id
        if not generation_id or (expected_generation is not None and expected_generation != generation_id):
            return decision("paused", "generation_unverified")
        generation = await self.generations.get(generation_id)
        if not generation or generation["state"] != "published" or generation["file_id"] != file_id:
            return decision("paused", "generation_unverified")
        try:
            with anyio.fail_after(5):
                valid = self.index is not None and await self.index.verify_generation(file, generation_id, generation["expected_chunk_count"])
            if not valid:
                return decision("paused", "index_unavailable", generation_id)
        except Exception:
            return decision("paused", "index_unavailable", generation_id)

        state, reason = "eligible", "verified_binding"
        try:
            binding = json.loads(file.source_binding_json or "{}")
        except (ValueError, TypeError):
            binding = {}
        if not isinstance(binding, dict):
            binding = {}
        previously_bound = (
            binding.get("sha256") == generation["source_sha256"] and binding.get("artifact_id") == file.artifact_id and not file.source_absent
        )
        try:
            with anyio.fail_after(3):
                artifact = await self.artifacts.get(file.artifact_id)
                if not artifact or artifact.status not in {"ready", "missing"}:
                    return decision("paused", "source_unavailable", generation_id)
                path, head = await source_head(self.artifacts.backend, artifact.path)
            if head is None:
                previously_bound = False
                with anyio.fail_after(6):
                    recent = await self.artifacts.list_recent_ready_paths(limit=20)
                    healthy = await check_storage_health(self.artifacts.backend, [p for _, p in recent])
                if healthy:
                    await self.generations.mark_absent(file)
                return decision("paused", "source_missing" if healthy else "source_unavailable", generation_id)
            identity = head.identity
            if not previously_bound or not identity or binding.get("identity") != identity:
                previously_bound = False
                with anyio.fail_after(30):
                    sha256 = await full_source_hash(self.artifacts.backend, path)
                    _, after = await source_head(self.artifacts.backend, path)
                if sha256 != generation["source_sha256"]:
                    await self.generations.mark_absent(file)
                    return decision("paused", "source_changed", generation_id)
                if after is None or after.identity != identity:
                    return decision("paused", "source_changed_during_check", generation_id)
                if not await self.generations.bind_source(file, identity or "", sha256):
                    return decision("paused", "generation_changed", generation_id)
        except Exception:
            if not previously_bound:
                return decision("paused", "source_unavailable", generation_id)
            state, reason = "warning", "source_temporarily_unavailable"

        current = await self.files.get(file_id)
        if current is not None:
            scope = await scoped(current)
        if (
            not current
            or current.soft_deleted_at
            or current.team_id != scope.team_id
            or not KbPermission.can_read(current, scope)
            or not await authorize(current)
        ):
            return decision("paused", "permission_or_file_unavailable")
        if current.active_generation_id != generation_id or current.artifact_id != file.artifact_id or current.source_absent:
            return decision("paused", "generation_changed", generation_id)
        return decision(state, reason, generation_id)
