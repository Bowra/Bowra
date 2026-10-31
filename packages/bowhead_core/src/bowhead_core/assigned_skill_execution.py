"""Execute published Agent Skill grants from an exact, call-local package copy.

This preserves existing operation and source-use gates. It does not migrate
legacy `use` grants, open script flags, or restore a reference/approval lease.
"""

from __future__ import annotations

import copy
import json
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any

import anyio
from bowhead_skill.loader import SkillSpec, load_skill
from bowhead_skill.runtime_scope import (
    SkillUnavailableError,
    assigned_skill_operation_denial,
    reference_skill_active,
    skill_operation_denial,
    skill_operation_for_tool,
    skill_script_runtime_denial,
    skill_unavailable,
)

from bowhead_core._db import open_shared_ctx
from bowhead_core.access_control import AccessControlStore
from bowhead_core.agent_dependency_compiler import ExactSkillGrant, _revision_token, parse_exact_skill_grants, skill_bundle_revision
from bowhead_core.chat_reference_scope import ReferenceRequestScope, read_reference_access_context_in_connection
from bowhead_core.chat_references import SkillResourceRef, _bounded_json
from bowhead_core.skill_instance_store import SkillInstanceStore
from bowhead_core.skill_read_resolution import ResolvedSkillPackage, SkillReadResolutionError, SkillReadSourceProof, SkillReadSourceResolver
from bowhead_core.skill_reference_metadata import BuiltinSkillMetadata, BuiltinSkillMetadataIndex, _package_revision
from bowhead_core.user_store import UserStore
from bowhead_skill.catalog import SkillCatalog, builtin_catalog_dir


def _denied(name: str, tool_name: str, reason: str = "source_changed", cause: str | None = None) -> dict:
    return skill_unavailable(name, skill_operation_for_tool(tool_name), reason, cause=cause)  # type: ignore[arg-type]


def _resolution_denial(exc: SkillReadResolutionError, name: str, tool_name: str) -> dict:
    """Map a source-resolution failure to its specific reason.

    A failure without a specific reason is reported as ``source_changed``, the
    only Skill denial marked retryable.
    """
    return _denied(name, tool_name, exc.reason or "source_changed", exc.cause)


def _version_changed(name: str) -> dict:
    # Read by the model (tool result) and the step row. Agents follow the current
    # Skill version, so a mismatch means it was edited while this call loaded it.
    return {
        "ok": False,
        "error_kind": "assigned_skill_version_changed",
        "error": f"Skill '{name}' was edited while this call was loading it. Retry the call to use the new version.",
    }


async def _current_revision(grant: ExactSkillGrant, db_path: Path, scope: ReferenceRequestScope) -> str | None:
    """The revision a published Agent follows for an editable Skill.

    Team Skills follow the latest approved bundle and personal Skills their
    owner's current bundle, so editing a Skill no longer silently disables
    every Agent that uses it. This only selects the version: the resolver
    still checks status, approval, visibility and use, and capture verifies
    every byte against it. None keeps the published revision.
    """
    if grant.kind == "team":
        record = await UserStore(db_path).get_team_skill(scope.team_id, grant.id)
        return _revision_token(record.approved_sha256) if record is not None else None
    record = await SkillInstanceStore(db_path).get_by_id(user_id=scope.user_id, skill_id=grant.id)
    if record is None or not record.source_path:
        return None
    try:
        return await anyio.to_thread.run_sync(skill_bundle_revision, record.source_path)
    except (OSError, ValueError):
        return None


def _scope(ctx: Any) -> ReferenceRequestScope:
    return ReferenceRequestScope(team_id=ctx.team_id, user_id=ctx.user_id, workspace_id=ctx.workspace_id, session_id=ctx.session_id or None)


def _name(tool_name: str, arguments: dict) -> str:
    return "python_sandbox" if tool_name == "python_exec" else str(arguments.get("name") or "").strip()


def _load_copy(root: Path, files: dict[str, bytes]) -> SkillSpec:
    # Paths and the full package hash were checked by PreparedSkillPackage.files.
    for relative, content in files.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    return load_skill(root)


@dataclass
class _Material:
    spec: SkillSpec = field(repr=False)
    source_kind: str
    sources: SkillReadSourceResolver = field(repr=False)
    proof: SkillReadSourceProof = field(repr=False)
    grants: tuple[ExactSkillGrant, ...] = field(repr=False)
    turn_id: str
    tool_name: str
    active: bool = True
    reference_authorizer: Callable[[str, dict, Any], Awaitable[None]] | None = field(default=None, repr=False)

    async def authorize_tool(self, tool_name: str, arguments: dict, ctx: Any) -> None:
        allowed_tools = {self.tool_name}
        if self.tool_name == "python_exec":
            allowed_tools.add("skill_invoke")  # The existing flat-to-nested adapter.
        if (
            not self.active
            or ctx.assigned_skill_material is not self
            or ctx.skill_runtime_grants != self.grants
            or _scope(ctx) != self.proof.scope
            or ctx.turn_id != self.turn_id
            or reference_skill_active(ctx) != (self.reference_authorizer is not None)
            or tool_name not in allowed_tools
            or _name(tool_name, arguments) != self.proof.name
        ):
            raise ValueError("assigned_skill_receiver_changed")
        if denial := skill_operation_denial(ctx, tool_name, arguments):
            raise SkillUnavailableError(denial)
        if self.reference_authorizer is None:
            try:
                await self.sources.revalidate(self.proof)
            except SkillReadResolutionError as exc:
                raise SkillUnavailableError(_resolution_denial(exc, self.proof.name, tool_name)) from None
            if getattr(ctx, "reference_helper_skill_scope", None) is not None:
                await ctx.ensure_reference_skill_context()
        else:
            # The reference authorizer rechecks the complete original baseline
            # and optional closure, including exact source identity/current use.
            # Its versioned epoch contract cannot pass a second legacy cache
            # equality check; ordinary assigned calls retain that strict fence.
            # This full-baseline check ends with the live worker fence. No
            # further database await may outlive that lease before dispatch.
            await self.reference_authorizer(tool_name, arguments, ctx)
        # Flags can change while current authority is read. Observe them after
        # the last await, at the actual dispatch boundary.
        if tool_name != "skill_read" and (
            denial := skill_script_runtime_denial(self.proof.name, uploaded=self.source_kind != "builtin", command=bool(arguments.get("command")))
        ):
            raise SkillUnavailableError(denial)


async def execute_assigned_skill(handler: Callable[..., Awaitable[Any]], arguments: dict, ctx: Any, *, catalog: SkillCatalog, tool_name: str) -> Any:
    """Capture before dispatch; clean up on success, rejection and cancellation.

    Invocations still use the original handler and sandbox transport. This copy
    is not installed into either catalog and cannot become another call's lease.
    """
    temporary = None
    material = None
    entered_handler = False
    name = ""
    try:
        # Detach client data and the receiver before the first asynchronous read.
        _bounded_json(arguments, max_depth=20, max_bytes=1024 * 1024)
        arguments = json.loads(json.dumps(arguments, ensure_ascii=False, allow_nan=False))
        ctx = copy.copy(ctx)
        grants = ctx.skill_runtime_grants
        name = _name(tool_name, arguments)
        if type(grants) is not tuple or any(type(grant) is not ExactSkillGrant for grant in grants):
            return _denied(name, tool_name)
        if parse_exact_skill_grants([grant.to_canonical_dict() for grant in grants]) != grants:
            return _denied(name, tool_name)
        if tool_name not in {"skill_read", "skill_invoke", "python_exec"} or reference_skill_active(ctx):
            return _denied(name, tool_name)
        if denial := skill_operation_denial(ctx, tool_name, arguments):
            return denial
        grant = next((item for item in grants if item.name == name), None)
        permission = "read" if tool_name == "skill_read" else "invoke"
        if grant is None or permission not in grant.permissions or grant.distribution != "reference":
            read_only = grant is not None and permission not in grant.permissions and "read" in grant.permissions
            return _denied(name, tool_name, "not_granted", "read_only" if read_only else None)
        scope = _scope(ctx)
        access_store = ctx.access_control_store
        if type(access_store) is not AccessControlStore:
            return _denied(name, tool_name)
        db_path = Path(access_store.db_path)
        instance_path = getattr(getattr(ctx, "skill_instance_store", None), "db_path", db_path)
        if Path(instance_path).resolve() != db_path.resolve():
            return _denied(name, tool_name)

        async def access_factory():
            async with open_shared_ctx(db_path) as db:
                await db.execute("PRAGMA query_only=ON")
                await db.execute("BEGIN")
                try:
                    return await read_reference_access_context_in_connection(db, scope=scope, access_store=access_store)
                finally:
                    await db.rollback()

        root = builtin_catalog_dir()
        index = None
        revision = grant.revision
        if grant.kind == "builtin":
            installed = catalog.get(name)
            if installed is None or grant.id != "builtin:" + name:
                return _denied(name, tool_name, "not_visible")
            relative = installed.source_dir.resolve().relative_to(root.resolve()).as_posix()
            # Builtins ship with the application and no team can edit them, so an
            # Agent follows the installed revision. Pinning the publish-time hash
            # broke every revisioned Agent on any release that touched the
            # package, even a comment. Capture still verifies every byte against
            # this revision, so a mid-call file change is rejected.
            revision = await anyio.to_thread.run_sync(_package_revision, installed.source_dir)
            index = BuiltinSkillMetadataIndex((BuiltinSkillMetadata(name, name, "", revision, None, relative),), "assigned-locator.v1", False)
        else:
            revision = await _current_revision(grant, db_path, scope) or revision
        sources = SkillReadSourceResolver(
            db_path, access_factory=access_factory, builtin_index=lambda: index, builtin_root=root, honor_member_preference=False
        )
        resource = SkillResourceRef(kind="skill", source_kind=grant.kind, id=grant.id, version=revision)
        package = await sources.prepare_runtime_package(resource, scope=scope)
        if package.proof.name != name:
            return _denied(name, tool_name)
        files = package.material.files()
        temporary = tempfile.TemporaryDirectory(prefix="bowhead-assigned-skill-")
        spec = await anyio.to_thread.run_sync(partial(_load_copy, Path(temporary.name), files))
        material = _Material(spec, grant.kind, sources, package.proof, grants, ctx.turn_id, tool_name)
        ctx.assigned_skill_material = material
        if denial := await assigned_skill_operation_denial(ctx, tool_name, arguments):
            return denial
        entered_handler = True
        result = await handler(arguments, ctx)
        if tool_name == "skill_read":
            if denial := await assigned_skill_operation_denial(ctx, tool_name, arguments):
                return denial
        return result
    except Exception as exc:
        if entered_handler:
            raise  # Preserve the original handler/unknown-outcome contract.
        if isinstance(exc, SkillReadResolutionError) and str(exc) == "reference_skill_version_changed":
            return _version_changed(name)
        if isinstance(exc, SkillReadResolutionError):
            return _resolution_denial(exc, name, tool_name)
        if isinstance(exc, SkillUnavailableError):
            return exc.result
        return _denied(name, tool_name)
    finally:
        if material is not None:
            material.active = False
        if temporary is not None:
            with anyio.CancelScope(shield=True):
                await anyio.to_thread.run_sync(temporary.cleanup)


async def dispatch_retained_reference_skill(
    handler: Callable[..., Awaitable[Any]],
    arguments: dict,
    ctx: Any,
    *,
    sources: SkillReadSourceResolver,
    package: ResolvedSkillPackage,
    grants: tuple[ExactSkillGrant, ...],
    tool_name: str,
    authorizer: Callable[[str, dict, Any], Awaitable[None]],
) -> Any:
    """Use an already retained package under a live reference worker's authority.

    The caller resolves the original composition; no current filesystem source
    is opened and no source/operation permission is added here.
    """
    temporary = None
    material = None
    entered_handler = False
    try:
        _bounded_json(arguments, max_depth=20, max_bytes=1024 * 1024)
        arguments = json.loads(json.dumps(arguments, ensure_ascii=False, allow_nan=False))
        ctx = copy.copy(ctx)
        if not reference_skill_active(ctx) or ctx.skill_runtime_grants != grants:
            raise ValueError("reference_skill_receiver_changed")
        files = package.material.files()
        temporary = tempfile.TemporaryDirectory(prefix="bowhead-reference-skill-")
        spec = await anyio.to_thread.run_sync(partial(_load_copy, Path(temporary.name), files))
        material = _Material(
            spec, package.proof.resource.source_kind, sources, package.proof, grants, ctx.turn_id, tool_name, reference_authorizer=authorizer
        )
        ctx.assigned_skill_material = material
        await material.authorize_tool(tool_name, arguments, ctx)
        entered_handler = True
        return await handler(arguments, ctx)
    except Exception:
        if entered_handler:
            raise
        return {"ok": False, "error_kind": "reference_skill_unavailable", "error": "The exact Skill invocation context is unavailable."}
    finally:
        if material is not None:
            material.active = False
        if temporary is not None:
            with anyio.CancelScope(shield=True):
                await anyio.to_thread.run_sync(temporary.cleanup)
