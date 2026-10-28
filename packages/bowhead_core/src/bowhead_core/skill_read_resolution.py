"""Exact Skill origin/use-state resolution for reference material preparation.

The context factory is a trusted server dependency (canonical request scope),
never request data; permission facts are read on one transaction. Source
permission is necessary but does not authorize an
Agent receiver, dependency closure or temporary extension. No runtime handle or
permission lease is returned; dispatch must recheck current effective authority.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from functools import partial
from pathlib import Path, PurePosixPath

import aiosqlite
import anyio

from bowhead_core._db import open_shared_ctx
from bowhead_core.access_control import AccessContext, AccessControlStore
from bowhead_core.chat_reference_scope import ReferenceRequestScope, read_reference_access_context_in_connection
from bowhead_core.chat_references import SkillResourceRef
from bowhead_core.resource_access import builtin_capability_ids_for_skill
from bowhead_core.share_service import grants_for_viewer, is_visible
from bowhead_core.skill_instance_store import SkillInstanceStore
from bowhead_core.skill_package_material import (
    CapturedSkillPackage,
    PreparedSkillPackage,
    SkillPackageMaterialError,
    capture_skill_package,
    prepare_skill_package,
)
from bowhead_core.skill_read_material import PreparedSkillRead, SkillReadMaterialError, prepare_skill_read
from bowhead_core.skill_reference_metadata import BuiltinSkillMetadataIndex
from bowhead_core.user_store import UserStore
from bowhead_skill.catalog import builtin_catalog_dir

AccessFactory = Callable[[], Awaitable[AccessContext]]
IndexFactory = Callable[[], BuiltinSkillMetadataIndex | None]


def build_reference_skill_sources(
    db_path: str | Path, *, scope: ReferenceRequestScope, builtin_index: IndexFactory, builtin_root: Path | None = None
) -> SkillReadSourceResolver:
    """Server scope -> fresh principals; installed metadata never grants use."""
    captured = ReferenceRequestScope.model_validate(scope.model_dump(mode="python"))
    access = AccessControlStore(db_path)

    async def current_access() -> AccessContext:
        async with open_shared_ctx(str(db_path)) as db:
            await db.execute("BEGIN")
            return await read_reference_access_context_in_connection(db, scope=captured, access_store=access)

    return SkillReadSourceResolver(db_path, access_factory=current_access, builtin_index=builtin_index, builtin_root=builtin_root)


class SkillReadResolutionError(ValueError):
    """Finite internal error; no private path, name, schema or account details.

    ``str(error)`` stays the stable internal code. ``reason`` (a ``skill_unavailable``
    reason) and ``cause`` (a finer wording hint) let Agent callers explain the
    failure; both are None when the branch has nothing more specific to say.
    """

    def __init__(self, code: str, *, reason: str | None = None, cause: str | None = None):
        super().__init__(code)
        self.reason = reason
        self.cause = cause


def _fail(code: str = "reference_skill_source_unavailable", *, reason: str | None = None, cause: str | None = None) -> None:
    raise SkillReadResolutionError(code, reason=reason, cause=cause) from None


def _digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class SkillReadSourceProof:
    scope: ReferenceRequestScope
    resource: SkillResourceRef
    name: str
    policy_epoch: int
    trust_basis: str
    source_state_digest: str


@dataclass(frozen=True, slots=True)
class ResolvedSkillRead:
    proof: SkillReadSourceProof
    material: PreparedSkillRead = field(repr=False)


@dataclass(frozen=True, slots=True)
class ResolvedSkillPackage:
    proof: SkillReadSourceProof
    material: PreparedSkillPackage = field(repr=False)


@dataclass(frozen=True, slots=True)
class ResolvedSkillRuntimePackage:
    proof: SkillReadSourceProof
    material: CapturedSkillPackage = field(repr=False)


@dataclass(frozen=True, slots=True)
class _Source:
    proof: SkillReadSourceProof
    path: Path = field(repr=False)


class SkillReadSourceResolver:
    def __init__(
        self,
        db_path: str | Path,
        *,
        access_factory: AccessFactory,
        builtin_index: IndexFactory | None = None,
        builtin_root: Path | None = None,
        honor_member_preference: bool = True,
    ):
        # A member's "turn off this Skill for me" preference shapes their own
        # Skill use. An Agent's declared Skill (an assigned grant) is not theirs
        # to turn off, so that resolver passes False; admin use decisions and
        # every source/approval check still apply.
        self.honor_member_preference = honor_member_preference
        # All stores read the same server-selected database. No initialize DDL or
        # separate default team/workspace source is created for this adapter.
        self.db_path = Path(db_path)
        self.users = UserStore(db_path)
        self.instances = SkillInstanceStore(db_path)
        self.access = AccessControlStore(db_path)
        self.access_factory = access_factory
        # This provider returns installed in-memory metadata; it must not rebuild
        # the index or read files when invoked inside the transaction kernel.
        self.builtin_index = builtin_index
        self.builtin_root = builtin_root if builtin_root is not None else builtin_catalog_dir()

    async def prepare(self, resource: SkillResourceRef, *, scope: ReferenceRequestScope, require_declaration: bool = False) -> ResolvedSkillRead:
        try:
            resource = SkillResourceRef.model_validate(resource.model_dump(mode="json"))
            scope = ReferenceRequestScope.model_validate(scope.model_dump(mode="json"))
            before = await self._resolve(resource, scope)
            material = await anyio.to_thread.run_sync(
                partial(prepare_skill_read, before.path, resource=resource, expected_name=before.proof.name, require_declaration=require_declaration)
            )
            after = await self._resolve(resource, scope)
            if before != after:
                _fail("reference_skill_source_changed")
            return ResolvedSkillRead(before.proof, material)
        except (SkillReadResolutionError, SkillReadMaterialError):
            raise
        except Exception:
            _fail()

    async def _resolve(self, resource: SkillResourceRef, scope: ReferenceRequestScope) -> _Source:
        # The request factory remains a scope fence, outside the read transaction.
        # Permission decisions themselves use fresh principals on that transaction.
        request_context = await self.access_factory()
        async with open_shared_ctx(self.db_path) as db:
            await db.execute("PRAGMA query_only=ON")
            await db.execute("BEGIN")
            try:
                source = await self._resolve_in_connection(db, resource, scope)
                if not isinstance(request_context, AccessContext) or (
                    request_context.team_id,
                    request_context.user_id,
                    request_context.workspace_id,
                    request_context.policy_epoch,
                ) != (scope.team_id, scope.user_id, scope.workspace_id, source.proof.policy_epoch):
                    _fail("reference_scope_unavailable")
            finally:
                await db.rollback()
        if await self.access.get_policy_epoch(scope.team_id) != source.proof.policy_epoch:
            _fail("reference_skill_source_changed")
        # Filesystem checks/capture stay outside every database transaction.
        if resource.source_kind == "builtin" and not source.path.resolve().is_relative_to(self.builtin_root.resolve()):
            _fail()
        return source

    async def prepare_package(self, resource: SkillResourceRef, *, scope: ReferenceRequestScope) -> ResolvedSkillPackage:
        """Prepare a baseline package under the same current exact source fence.

        Capturing executable bytes is not invoke authorization. The receiver,
        separate operation grant, sandbox and live ownership remain mandatory.
        """
        try:
            resource = SkillResourceRef.model_validate(resource.model_dump(mode="python", warnings="error"))
            scope = ReferenceRequestScope.model_validate(scope.model_dump(mode="python", warnings="error"))
            before = await self._resolve(resource, scope)
            material = await anyio.to_thread.run_sync(partial(prepare_skill_package, before.path, resource=resource, expected_name=before.proof.name))
            after = await self._resolve(resource, scope)
            if before != after:
                _fail("reference_skill_source_changed")
            return ResolvedSkillPackage(before.proof, material)
        except (SkillReadResolutionError, SkillPackageMaterialError):
            raise
        except Exception:
            _fail()

    async def prepare_runtime_package(self, resource: SkillResourceRef, *, scope: ReferenceRequestScope) -> ResolvedSkillRuntimePackage:
        """Current exact source for a call-local copy, without a storage envelope.

        The same filesystem/package bounds apply. Reference persistence continues
        to require prepare_package and its stricter encoded-content budget.
        """
        try:
            resource = SkillResourceRef.model_validate(resource.model_dump(mode="python", warnings="error"))
            scope = ReferenceRequestScope.model_validate(scope.model_dump(mode="python", warnings="error"))
            before = await self._resolve(resource, scope)
            material = await anyio.to_thread.run_sync(partial(capture_skill_package, before.path, resource=resource, expected_name=before.proof.name))
            after = await self._resolve(resource, scope)
            if before != after:
                _fail("reference_skill_source_changed")
            return ResolvedSkillRuntimePackage(before.proof, material)
        except (SkillReadResolutionError, SkillPackageMaterialError):
            raise
        except Exception:
            _fail()

    async def _resolve_in_connection(
        self, db: aiosqlite.Connection, resource: SkillResourceRef, scope: ReferenceRequestScope, *, retained: SkillReadSourceProof | None = None
    ) -> _Source:
        """Read source metadata/use facts; no connection ownership or file IO."""
        ctx = await read_reference_access_context_in_connection(db, scope=scope, access_store=self.access)
        epoch = ctx.policy_epoch
        state = {"context": ctx.as_dict()}
        owner = None
        team_skill_id = None
        if resource.source_kind == "builtin":
            index = self.builtin_index() if self.builtin_index is not None else None
            found = [item for item in index.items if item.id == resource.id] if index is not None else []
            if len(found) != 1:
                _fail(reason="not_visible")
            item = found[0]
            if item.revision != resource.version:
                _fail("reference_skill_version_changed")
            relative = PurePosixPath(item.relative_path)
            if relative.is_absolute() or ".." in relative.parts or relative.as_posix() != item.relative_path:
                _fail(reason="not_visible")
            path = self.builtin_root.joinpath(*relative.parts)
            name = item.name
            enabled = True
            if self.honor_member_preference:
                preference = await self.instances.get(user_id=scope.user_id, name=name, connection=db)
                enabled = preference is None or preference.source_type != "builtin" or preference.enabled
            if not enabled:
                _fail(reason="not_visible", cause="personal_disabled")
            state.update(origin="builtin", name=name, path=str(path), revision=item.revision, enabled=enabled)
            trust = "installed_builtin"
        elif resource.source_kind == "user":
            # The requester owns a personal resource. Agent authors and
            # other resource kinds with the same name cannot supply it.
            record = await self.instances.get_by_id(user_id=scope.user_id, skill_id=resource.id, connection=db)
            if (
                record is None
                or record.user_id != scope.user_id
                or record.source_type not in {"user_upload", "user_generated"}
                or not record.source_path
            ):
                _fail(reason="not_visible")
            if not record.enabled:
                _fail(reason="not_visible", cause="personal_disabled")
            if record.import_pending:
                _fail(reason="not_approved", cause="import_pending")
            name, path, owner = record.name, Path(record.source_path), record.user_id
            state.update(
                origin=record.source_type,
                id=record.id,
                name=name,
                path=str(path),
                owner=owner,
                enabled=record.enabled,
                import_status=record.import_status,
                updated_at=record.updated_at,
            )
            trust = "personal_owner_current_bytes"
        else:
            record = await self.users.get_team_skill(scope.team_id, resource.id, only_enabled=False, connection=db)
            if record is None or record.team_id != scope.team_id or not record.source_path:
                _fail(reason="not_visible")
            if record.status != "enabled":
                _fail(reason="disabled_by_team")
            if record.approval != "approved":
                # A previously approved Skill whose edit returned it to review.
                _fail(reason="pending_reapproval" if record.approved_sha256 else "not_approved")
            shared = await grants_for_viewer(
                self.access, team_id=scope.team_id, user_id=scope.user_id, workspace_id=scope.workspace_id, kind="team_skill", connection=db
            )
            if not is_visible(
                owner_id=record.created_by, visibility=record.visibility, resource_id=record.id, viewer_id=scope.user_id, granted_ids=shared
            ):
                _fail(reason="not_visible", cause="not_shared")
            approved = record.approved_sha256
            if approved:
                approved = approved if approved.startswith("sha256:") else "sha256:" + approved
                if approved != resource.version:
                    _fail("reference_skill_version_changed")
            elif record.visibility == "team_wide":
                _fail(reason="not_approved")
            name, path, owner, team_skill_id = record.name, Path(record.source_path), record.created_by, record.id
            if self.honor_member_preference:
                preference = await self.instances.get(user_id=scope.user_id, name=f"team:{record.id}", connection=db)
                if preference is None:
                    legacy = await self.instances.get(user_id=scope.user_id, name=name, connection=db)
                    preference = legacy if legacy is not None and legacy.source_type == "team_shared" else None
                if preference is not None and not preference.enabled:
                    _fail(reason="not_visible", cause="personal_disabled")
            state.update(
                origin="team",
                id=record.id,
                name=name,
                path=str(path),
                owner=owner,
                status=record.status,
                approval=record.approval,
                approved=approved,
                visibility=record.visibility,
                approved_at=record.approved_at,
                approved_by=record.approved_by,
                enabled=True,
            )
            if record.visibility != "team_wide":
                state["updated_at"] = record.updated_at
            trust = "team_approved_sha256" if approved else "creator_scoped_current_bytes"
        targets = [("team_skill", team_skill_id)] if team_skill_id else [("skill", name)]
        targets.extend(("builtin_capability", cid) for cid in builtin_capability_ids_for_skill(name))
        for kind, identity in targets:
            decision = await self.access.explain_decision(ctx, kind, identity, "use", connection=db)
            owner_exempt = kind == "team_skill" and owner == scope.user_id and decision.get("reason") == "not_in_allowlist"
            # Explicit reference inputs require actual current use at every
            # boundary, including baseline reads. Legacy diagnostic rollout
            # settings cannot grant access to this dedicated execution path.
            if not decision.get("allowed") and not owner_exempt:
                _fail(reason="use_denied")
        proof = SkillReadSourceProof(scope, resource, name, epoch, trust, _digest(state))
        if retained is not None:
            # All decisions above use the CURRENT actor and policy epoch. Only
            # the comparison digest normalizes the historical cache revision;
            # every role/group and every source/trust field must still match.
            if type(retained.policy_epoch) is not int or not 0 <= retained.policy_epoch <= epoch:
                _fail("reference_skill_source_changed")
            comparison = {**state, "context": {**state["context"], "policy_epoch": retained.policy_epoch}}
            if replace(proof, policy_epoch=retained.policy_epoch, source_state_digest=_digest(comparison)) != retained:
                _fail("reference_skill_source_changed")
        return _Source(proof, path)

    async def revalidate(self, proof: SkillReadSourceProof) -> None:
        """Recheck a trusted preparation's source facts; never extend its authority.

        Used after preparing other closure members. This checks current stores,
        not current package bytes, and cannot authorize a receiver or dispatch.
        """
        try:
            resource = SkillResourceRef.model_validate(proof.resource.model_dump(mode="json"))
            scope = ReferenceRequestScope.model_validate(proof.scope.model_dump(mode="json"))
            current = await self._resolve(resource, scope)
            if current.proof != proof:
                _fail("reference_skill_source_changed")
        except SkillReadResolutionError:
            raise
        except Exception:
            _fail()

    async def describe_in_connection(
        self, db: aiosqlite.Connection, resource: SkillResourceRef, *, scope: ReferenceRequestScope
    ) -> SkillReadSourceProof:
        """Current source metadata/use in the caller transaction, without files."""
        try:
            resource = SkillResourceRef.model_validate(resource.model_dump(mode="python", warnings="error"))
            scope = ReferenceRequestScope.model_validate(scope.model_dump(mode="python", warnings="error"))
            return (await self._resolve_in_connection(db, resource, scope)).proof
        except SkillReadResolutionError:
            raise
        except Exception:
            _fail()

    async def revalidate_in_connection(self, db: aiosqlite.Connection, proof: SkillReadSourceProof) -> None:
        """Recheck prepared source facts in the caller's active transaction.

        Does not call the request context factory or inspect package files. The
        caller owns the connection and must bind the proof/material to its exact
        input/receiver, then recheck authority at subsequent execution boundaries.
        """
        try:
            resource = SkillResourceRef.model_validate(proof.resource.model_dump(mode="python", warnings="error"))
            scope = ReferenceRequestScope.model_validate(proof.scope.model_dump(mode="python", warnings="error"))
            current = await self._resolve_in_connection(db, resource, scope)
            if current.proof != proof:
                _fail("reference_skill_source_changed")
        except SkillReadResolutionError:
            raise
        except Exception:
            _fail()

    async def revalidate_retained_in_connection(self, db: aiosqlite.Connection, proof: SkillReadSourceProof) -> None:
        """Authorize now, then compare the unchanged source across cache epochs.

        Caller must independently restore the immutable selected receiver,
        complete material and current published extension policy. Old proof bytes
        remain unchanged. No path, source kind, name, trust, role or group drift
        is ignored; a new epoch never supplies an old or new permission grant.
        """
        try:
            resource = SkillResourceRef.model_validate(proof.resource.model_dump(mode="python", warnings="error"))
            scope = ReferenceRequestScope.model_validate(proof.scope.model_dump(mode="python", warnings="error"))
            await self._resolve_in_connection(db, resource, scope, retained=proof)
        except SkillReadResolutionError:
            raise
        except Exception:
            _fail()
