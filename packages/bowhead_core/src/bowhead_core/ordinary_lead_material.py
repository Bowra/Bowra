"""Native, call-local material evidence for the retained ordinary Lead loop.

Observation never grants execution or changes a Tool result. Missing evidence
refuses continuation; it must not turn an already completed call into a retry.
"""

from __future__ import annotations

import copy
import hashlib
import os
import re
from dataclasses import asdict
from pathlib import Path

import anyio
from bowhead_skill.index import substitute_args
from bowhead_skill.loader import load_skill

from bowhead_core._db import open_shared_ctx
from bowhead_core.agents._generated.tool_kinds import TOOL_RISKS
from bowhead_core.agents._generated.tool_renames import canonical_tool_name
from bowhead_core.chat_reference_scope import ReferenceRequestScope
from bowhead_core.chat_references import SkillResourceRef
from bowhead_core.ordinary_lead_continuation import OrdinaryContinuationUnavailable, OrdinaryQuestionUnavailable, canonical_json, content_hash
from bowhead_core.skill_read_material import _bundle_hash, _capture
from bowhead_core.skill_read_resolution import SkillReadSourceProof, build_reference_skill_sources
from bowhead_core.skill_reference_metadata import BuiltinSkillMetadata, BuiltinSkillMetadataIndex
from bowhead_skill.catalog import builtin_catalog_dir

# Built-in Tools whose Bowhead manifest risk is ``read`` settle as a plain
# observation: the retained result is the evidence. Tools with a stronger
# native proof (attachment, skill, sandbox) keep that proof and still refuse
# when it is missing. Team, MCP and personal Tools have no manifest risk.
_PROOF_BEARING_READS = frozenset({"attachment_read", "skill_read", "skill_list", "skill_invoke"})
RETAINED_READ_TOOLS = frozenset(name for name, risk in TOOL_RISKS.items() if risk == "read") - _PROOF_BEARING_READS


def retained_read_tool(name: str) -> bool:
    return canonical_tool_name(name) in RETAINED_READ_TOOLS


def material_observations(ctx):
    """Select original continuation evidence or context-only reader observations."""
    state = getattr(ctx, "ordinary_lead_state", None)
    if state is not None and state.checkpoint is None:
        return state
    return getattr(ctx, "context_source_observations", None)


def collecting(ctx) -> bool:
    return material_observations(ctx) is not None and getattr(ctx, "ordinary_call_identity", None) is not None


def note_known_rejection(ctx, tool: str, arguments: dict, result: dict, *, error: str | None = None) -> None:
    if collecting(ctx) and tool in {"skill_read", "skill_list", "python_exec", "skill_invoke"}:
        material_observations(ctx).tool_materials[ctx.ordinary_call_identity] = {
            "tool": tool,
            "arguments_hash": content_hash(arguments),
            "result_hash": content_hash(result),
            "error": error,
            "kind": "rejected",
            "skills": [],
        }


def note_attachment_read(ctx, arguments: dict, result: dict, artifact) -> None:
    """Record only the authorized native reader's successful extraction."""
    if collecting(ctx):
        material_observations(ctx).tool_materials[ctx.ordinary_call_identity] = {
            "tool": "attachment_read",
            "arguments_hash": content_hash(arguments),
            "result_hash": content_hash(result),
            "error": None,
            "kind": "attachment_read",
            "skills": [],
            "attachment": {
                "artifact_id": artifact.id,
                "content_hash": result["content_hash"],
                "metadata_hash": _artifact_metadata_hash(artifact, contract="ordinary-lead-loop.v8"),
            },
        }


async def _revalidate_attachment_read(ctx, record: dict, *, contract: str, read_bytes: bool) -> None:
    from bowhead_core.attachment_read import _original_attachment, _read_original_bytes

    expected = record["attachment"]
    try:
        if read_bytes:
            artifact, digest, _ = await _read_original_bytes(ctx, expected["artifact_id"], expected_hash=expected["content_hash"])
        else:
            # Read current database facts while the caller's final consumption
            # transaction excludes writes. No parsing or object I/O here.
            artifact, digest = await _original_attachment(ctx, expected["artifact_id"])
        if digest != expected["content_hash"] or _artifact_metadata_hash(artifact, contract=contract) != expected["metadata_hash"]:
            raise OrdinaryContinuationUnavailable()
    except (ValueError, OSError) as exc:
        raise OrdinaryContinuationUnavailable() from exc


def _scope(ctx) -> ReferenceRequestScope:
    return ReferenceRequestScope(team_id=ctx.team_id, user_id=ctx.user_id, workspace_id=ctx.workspace_id, session_id=ctx.session_id)


def _proof_json(proof: SkillReadSourceProof) -> dict:
    return {**asdict(proof), "scope": proof.scope.model_dump(mode="json"), "resource": proof.resource.model_dump(mode="json")}


def _proof(value: dict) -> SkillReadSourceProof:
    return SkillReadSourceProof(
        **{**value, "scope": ReferenceRequestScope.model_validate(value["scope"]), "resource": SkillResourceRef.model_validate(value["resource"])}
    )


def _sources(ctx, resource: SkillResourceRef, name: str, *, catalog=None):
    catalog = catalog or ctx.skill_catalog
    index = None
    root = builtin_catalog_dir()
    if resource.source_kind == "builtin":
        spec = catalog.get(name) if catalog is not None else None
        if spec is None:
            raise OrdinaryContinuationUnavailable()
        relative = spec.source_dir.resolve().relative_to(root.resolve()).as_posix()
        index = BuiltinSkillMetadataIndex((BuiltinSkillMetadata(name, name, "", resource.version, None, relative),), "ordinary-locator.v1", False)
    return build_reference_skill_sources(ctx.session_store.db_path, scope=_scope(ctx), builtin_index=lambda: index, builtin_root=root)


async def _capture_source(ctx, *, name: str, root: Path, kind: str, identity: str, catalog=None) -> tuple[dict, dict[str, bytes]]:
    files = await anyio.to_thread.run_sync(_capture, root)
    resource = SkillResourceRef(kind="skill", source_kind=kind, id=identity, version=_bundle_hash(files))
    sources = _sources(ctx, resource, name, catalog=catalog)
    package = await sources.prepare_runtime_package(resource, scope=_scope(ctx))
    if package.proof.name != name or package.material.files() != files:
        raise OrdinaryContinuationUnavailable()
    return _proof_json(package.proof), files


async def revalidate_skills(ctx, proofs: list[dict]) -> None:
    for value in proofs:
        proof = _proof(value)
        if proof.scope != _scope(ctx):
            raise OrdinaryContinuationUnavailable()
        sources = _sources(ctx, proof.resource, proof.name)
        # Hash the current package, then authorize in the current transaction.
        # An unrelated policy-epoch change is not a new source or a new grant.
        await sources.prepare_runtime_package(proof.resource, scope=proof.scope)
        async with open_shared_ctx(ctx.session_store.db_path) as db:
            await db.execute("BEGIN")
            await sources.revalidate_retained_in_connection(db, proof)


async def revalidate_material_authority_in_connection(ctx, db, material: dict, *, contract: str = "ordinary-lead-loop.v3") -> None:
    """Recheck metadata while the final consumption transaction excludes writes.

    File bytes were checked immediately before this transaction. Store readers
    here only read the shared database; they perform no file/network I/O.
    """
    artifacts = []
    for record in material.get("tools", []):
        if record["kind"] == "attachment_read":
            await _revalidate_attachment_read(ctx, record, contract=contract, read_bytes=False)
        for value in record["skills"]:
            proof = _proof(value)
            if proof.scope != _scope(ctx):
                raise OrdinaryContinuationUnavailable()
            await _sources(ctx, proof.resource, proof.name).revalidate_retained_in_connection(db, proof)
        artifacts.extend(record.get("artifacts", []))
    if "canvas" in material:
        expected = material["canvas"]
        projection = await ctx.canvas_store.read_retained_projection(session_id=ctx.session_id, turn_id=ctx.turn_id) if ctx.canvas_store else None
        if (projection is None) != (expected is None):
            raise OrdinaryContinuationUnavailable()
        if expected is not None:
            if _canvas_hash(projection) != expected["projection_hash"]:
                raise OrdinaryContinuationUnavailable()
            artifacts.extend(expected["artifacts"])
    for expected in artifacts:
        art = await ctx.artifact_store.get(expected["artifact_id"], check_created_by=ctx.user_id, reject_null_owner=True, connection=db)
        if art is None or _artifact_metadata_hash(art, contract=contract) != expected["metadata_hash"]:
            raise OrdinaryContinuationUnavailable()


def _artifact_metadata_hash(art, *, contract: str) -> str:
    if contract not in {
        "ordinary-lead-loop.v1",
        "ordinary-lead-loop.v2",
        "ordinary-lead-loop.v3",
        "ordinary-lead-loop.v4",
        "ordinary-lead-loop.v5",
        "ordinary-lead-loop.v6",
        "ordinary-lead-loop.v7",
        "ordinary-lead-loop.v8",
        "ordinary-lead-loop.v9",
        "ordinary-lead-loop.v10",
        "ordinary-lead-loop.v11",
        "ordinary-lead-loop.v12",
        "ordinary-lead-loop.v13",
    }:
        raise OrdinaryContinuationUnavailable()
    metadata = asdict(art)
    metadata.pop("last_used_at", None)  # A read's access timestamp is not content.
    if contract in {
        "ordinary-lead-loop.v3",
        "ordinary-lead-loop.v4",
        "ordinary-lead-loop.v5",
        "ordinary-lead-loop.v6",
        "ordinary-lead-loop.v7",
        "ordinary-lead-loop.v8",
        "ordinary-lead-loop.v9",
        "ordinary-lead-loop.v10",
        "ordinary-lead-loop.v11",
        "ordinary-lead-loop.v12",
        "ordinary-lead-loop.v13",
    }:
        # Classification can settle after the output observer. These labels
        # do not change content or provenance; actual deletion still invalidates
        # the proof. Historical checkpoints retain their exact original digest.
        for field in ("category", "category_source", "retention_hint"):
            metadata.pop(field, None)
    return content_hash(metadata)


async def artifact_evidence(ctx, artifact_id: str, *, output: bool, contract: str = "ordinary-lead-loop.v3") -> dict:
    store = ctx.artifact_store
    art = await store.get(artifact_id, check_created_by=ctx.user_id, reject_null_owner=True)
    if art is None or art.status != "ready" or art.workspace_id != ctx.workspace_id or not 0 <= art.size <= 50 * 1024 * 1024:
        raise OrdinaryContinuationUnavailable()
    if art.created_by_turn != ctx.turn_id:
        if output:
            raise OrdinaryContinuationUnavailable()
        from bowhead_core.session_store import _read_chat_turn_user_source_in_tx

        async with open_shared_ctx(ctx.session_store.db_path) as db:
            source = await _read_chat_turn_user_source_in_tx(
                db, turn_id=ctx.turn_id, expected_user_id=ctx.user_id, expected_workspace_id=ctx.workspace_id
            )
        if source is None or artifact_id not in source.attachment_ids:
            raise OrdinaryContinuationUnavailable()
        ready = await store.get_ready_uploads_for_chat([artifact_id], user_id=ctx.user_id, workspace_id=ctx.workspace_id)
        if artifact_id not in ready:
            raise OrdinaryContinuationUnavailable()
    content = await store.backend.read_bounded(art.path, limit=art.size)
    if len(content) != art.size:
        raise OrdinaryContinuationUnavailable()
    return {
        "artifact_id": artifact_id,
        "metadata_hash": _artifact_metadata_hash(art, contract=contract),
        "size": art.size,
        "sha256": hashlib.sha256(content).hexdigest(),
        "output": output,
    }


async def revalidate_tool_materials(ctx, records: list[dict], *, contract: str = "ordinary-lead-loop.v3") -> None:
    for record in records:
        await revalidate_skills(ctx, record["skills"])
        if record["kind"] == "attachment_read":
            await _revalidate_attachment_read(ctx, record, contract=contract, read_bytes=True)
        if record["kind"] in {"sandbox", "uncertain_sandbox"}:
            for expected in record["artifacts"]:
                if await artifact_evidence(ctx, expected["artifact_id"], output=expected["output"], contract=contract) != expected:
                    raise OrdinaryContinuationUnavailable()


async def canvas_evidence(ctx, *, contract: str = "ordinary-lead-loop.v3") -> dict | None:
    if ctx.canvas_store is None:
        return None
    projection = await ctx.canvas_store.read_retained_projection(session_id=ctx.session_id, turn_id=ctx.turn_id)
    if projection is None:
        return None
    canvas = projection["canvas"]
    if (canvas["user_id"], canvas["workspace_id"], canvas["session_id"]) != (ctx.user_id, ctx.workspace_id, ctx.session_id) or canvas[
        "status"
    ] == "archived":
        raise OrdinaryContinuationUnavailable()
    from bowhead_core.artifact_references import _ARTIFACT_ID_RE

    # Reuse conservative dependency enumeration, then apply actual ownership
    # and source checks. A textual ID supplies no permission of its own.
    touched = [row for row in projection["revisions"] if row["by_turn_id"] == ctx.turn_id]
    ids = sorted(set(_ARTIFACT_ID_RE.findall(canonical_json(touched))))
    if len(ids) > 64:
        raise OrdinaryContinuationUnavailable()
    artifacts = [await artifact_evidence(ctx, artifact_id, output=False, contract=contract) for artifact_id in ids]
    return {"canvas_id": canvas["id"], "projection_hash": _canvas_hash(projection), "artifacts": artifacts}


def _canvas_hash(projection: dict) -> str:
    for section in projection["sections"]:
        # Ordinary pause finalization adjusts placement without a content
        # revision. Retain current placement; never restore these display fields.
        # Payload, revision, order, ownership, quarantine and supersession stay
        # in the digest, as does the complete edit ledger.
        for field in ("hidden_for_chat_mode", "surface_rationale", "updated_at"):
            section.pop(field, None)
    return content_hash(projection)


class OrdinarySkillObserver:
    def __init__(self, catalog):
        self.catalog = catalog

    @staticmethod
    def active(ctx) -> bool:
        return collecting(ctx)

    async def read_rejected(self, arguments, result, ctx) -> None:
        """Retain a native bundle validation failure with no returned material."""
        if collecting(ctx):
            material_observations(ctx).pending_skill_reads.pop(ctx.ordinary_call_identity, None)
            try:
                note_known_rejection(ctx, "skill_read", arguments, result)
            except Exception:
                material_observations(ctx).tool_materials.pop(ctx.ordinary_call_identity, None)

    async def reading(self, spec, arguments, ctx) -> None:
        if not collecting(ctx):
            return
        state = material_observations(ctx)
        key = ctx.ordinary_call_identity
        state.pending_skill_reads.pop(key, None)
        try:
            if spec != await anyio.to_thread.run_sync(load_skill, spec.source_dir):
                raise OrdinaryContinuationUnavailable()
            assigned = getattr(ctx, "assigned_skill_material", None)
            if assigned is not None:
                if not assigned.active or assigned.spec is not spec:
                    raise OrdinaryContinuationUnavailable()
                files = await anyio.to_thread.run_sync(_capture, spec.source_dir)
                proof = assigned.proof
                if _bundle_hash(files) != proof.resource.version or proof.scope != _scope(ctx):
                    raise OrdinaryContinuationUnavailable()
                retained = _proof_json(proof)
            else:
                if self.catalog.get(spec.name) is spec:
                    kind, identity, root = "builtin", "builtin:" + spec.name, spec.source_dir
                else:
                    record = await ctx.skill_instance_store.get(user_id=ctx.user_id, name=spec.name)
                    if record is not None and record.source_type in {"user_upload", "user_generated"}:
                        kind, identity, root = "user", record.id, Path(record.source_path)
                    else:
                        record = await ctx.team_skill_store.get_by_name(ctx.team_id, spec.name, only_enabled=True)
                        kind, identity, root = "team", record.id, Path(record.source_path)
                    if root.resolve() != spec.source_dir.resolve():
                        raise OrdinaryContinuationUnavailable()
                retained, files = await _capture_source(ctx, name=spec.name, root=root, kind=kind, identity=identity, catalog=self.catalog)
            state.pending_skill_reads[key] = (retained, files, dict(arguments))
        except Exception:
            # Keep the native call's behavior, but never certify a fallback.
            state.pending_skill_reads.pop(key, None)

    async def listed(self, entries, arguments, ctx) -> None:
        if not collecting(ctx):
            return
        state = material_observations(ctx)
        key = ctx.ordinary_call_identity
        state.tool_materials.pop(key, None)
        try:
            proofs = []
            for entry in entries:
                if entry.source_path is None or not entry.source_id:
                    raise OrdinaryContinuationUnavailable()
                proof, _ = await _capture_source(
                    ctx,
                    name=entry.name,
                    root=entry.source_path,
                    kind="builtin" if entry.origin == "catalog" else entry.origin,
                    identity=entry.source_id,
                    catalog=self.catalog,
                )
                proofs.append(proof)
            state.tool_materials[key] = {
                "kind": "skill_list",
                "skills": proofs,
                "tool": "skill_list",
                "arguments_hash": content_hash(arguments),
                "error": None,
            }
        except Exception:
            state.tool_materials.pop(key, None)

    async def invoking(self, spec, script, inputs, ctx) -> None:
        if not collecting(ctx):
            return
        state, key = material_observations(ctx), ctx.ordinary_call_identity
        state.tool_materials.pop(key, None)
        try:
            if (
                spec.name != "python_sandbox"
                or script != "runner.py"
                or inputs.get("_skill_dir")
                or os.environ.get("BOWHEAD_SANDBOX_BACKEND", "executor").lower() != "executor"
            ):
                return
            await self.reading(spec, {}, ctx)
            pending = state.pending_skill_reads.pop(key, None)
            if pending is None:
                return
            proof, _, _ = pending
            actual_inputs = inputs.get("inputs") or {}
            if not isinstance(actual_inputs, dict) or len(actual_inputs) > 64:
                return
            artifacts, expected = [], []
            for name, item in actual_inputs.items():
                evidence = await artifact_evidence(ctx, item["artifact_id"], output=False)
                artifacts.append(evidence)
                expected.append({"key": name, **{field: evidence[field] for field in ("artifact_id", "size", "sha256")}})
            sanitized = {name: value for name, value in inputs.items() if name not in {"_artifact_fetch_token", "_sandbox_executor_token"}}
            state.tool_materials[key] = {
                "kind": "sandbox",
                "skills": [proof],
                "artifacts": artifacts,
                "normalized_input_hash": content_hash(sanitized),
                "expected_inputs": expected,
                "error": None,
            }
        except Exception:
            state.tool_materials.pop(key, None)

    async def executed(self, result, ctx) -> None:
        if not collecting(ctx):
            return
        state, key = material_observations(ctx), ctx.ordinary_call_identity
        record = state.tool_materials.get(key)
        if record is None or record["kind"] != "sandbox":
            return
        try:
            envelope = result.result_json
            execution = envelope["execution"]
            if (
                not result.ok
                or result.exit_code != 0
                or envelope.get("ok") is not True
                or execution.get("contract") != "sandbox-execution.v1"
                or execution.get("settled") is not True
                or execution.get("runtime") not in {"runc", "runsc"}
                or execution.get("network") != "none"
                or re.fullmatch(r"sha256:[0-9a-f]{64}", execution.get("image_id", "")) is None
                or re.fullmatch(r"bowhead-sandbox-[0-9a-f]{24}", execution.get("id", "")) is None
                or envelope.get("input_artifacts") != record.pop("expected_inputs")
            ):
                raise OrdinaryContinuationUnavailable()
            record["execution"] = copy.deepcopy(execution)
        except Exception:
            state.tool_materials.pop(key, None)

    async def finished(self, tool, arguments, result, ctx) -> None:
        if not collecting(ctx):
            return
        state, key = material_observations(ctx), ctx.ordinary_call_identity
        try:
            if tool == "skill_read":
                pending = state.pending_skill_reads.pop(key, None)
                if pending is None or not isinstance(result, dict) or "error" in result or result.get("ok") is False:
                    return
                proof, files, actual_arguments = pending
                if actual_arguments != arguments:
                    raise OrdinaryContinuationUnavailable()
                from bowhead_skill.loader import split_skill_markdown

                _, body = split_skill_markdown(files["SKILL.md"].decode())
                if result.get("file"):
                    body = files[result["file"]].decode()
                elif result.get("ref"):
                    body = files["references/" + result["ref"]].decode()
                else:
                    raw = arguments.get("args") or ""
                    body = substitute_args(body, raw if isinstance(raw, list) else str(raw))
                if result.get("body") != body or ("body_raw" in result and result["body_raw"] != files["SKILL.md"].decode()):
                    raise OrdinaryContinuationUnavailable()
                state.tool_materials[key] = {
                    "kind": "skill_read",
                    "skills": [proof],
                    "tool": tool,
                    "arguments_hash": content_hash(arguments),
                    "error": None,
                }
            record = state.tool_materials.get(key)
            if record is not None and record["kind"] == "sandbox":
                if not isinstance(result, dict) or result.get("ok") is not True or "execution" not in record:
                    raise OrdinaryContinuationUnavailable()
                if tool not in {"python_exec", "skill_invoke"} or (tool == "skill_invoke" and arguments.get("name") != "python_sandbox"):
                    raise OrdinaryContinuationUnavailable()
                outputs = result.get("output_artifacts")
                if not isinstance(outputs, list) or len(outputs) > 64:
                    raise OrdinaryContinuationUnavailable()
                for item in outputs:
                    evidence = await artifact_evidence(ctx, item["artifact_id"], output=True)
                    record["artifacts"].append(evidence)
                if sum(item["size"] for item in record["artifacts"]) > 64 * 1024 * 1024:
                    raise OrdinaryContinuationUnavailable()
                record.update(tool=tool, arguments_hash=content_hash(arguments), result_hash=content_hash(result))
                await revalidate_tool_materials(ctx, [record])
            if record is not None and record["kind"] in {"skill_read", "skill_list"}:
                # No re-resolution by name: the native observation retained IDs.
                ctx_copy = copy.copy(ctx)
                ctx_copy.skill_catalog = self.catalog
                await revalidate_skills(ctx_copy, record["skills"])
                record["result_hash"] = content_hash(result)
        except Exception:
            state.tool_materials.pop(key, None)


def retained_tool_materials(helper, state, *, collaboration: bool = False) -> list[dict]:
    records = []
    for child in helper.nested:
        if child.tool_name == "subagent_return" or (collaboration and child.tool_name == "agent_message"):
            continue
        record = state.tool_materials.get((helper.result["subagent_id"], child.call_id))
        if record is None and retained_read_tool(child.tool_name) and child.ended_at:
            record = {
                "kind": "read_result",
                "skills": [],
                "tool": child.tool_name,
                "arguments_hash": content_hash(child.arguments),
                "result_hash": content_hash(child.result),
                "error": child.error,
            }
        if record is None:
            raise OrdinaryQuestionUnavailable(
                f"No question was created because the {child.tool_name} result (call {child.call_id}) cannot be retained for a durable pause. "
                "The earlier results remain available in this conversation. Continue with them or explain which user input is still needed."
            )
        if any(
            record.get(field) != value
            for field, value in {
                "tool": child.tool_name,
                "arguments_hash": content_hash(child.arguments),
                "result_hash": content_hash(child.result),
                "error": child.error,
            }.items()
        ):
            raise OrdinaryContinuationUnavailable()
        records.append({**record, "call_id": child.call_id})
    return records
