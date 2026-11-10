"""Retain ordinary Session inputs without extending the canonical user selection."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from bowhead_core.chat_reference_attachment_snapshot import (
    MAX_LEAD_ATTACHMENT_TOTAL_BYTES,
    reopen_prepared_attachment_snapshot,
    seal_prepared_attachment_snapshot,
)
from bowhead_core.ordinary_lead_continuation import OrdinaryContinuationUnavailable
from bowhead_core.ordinary_lead_material import _artifact_metadata_hash
from bowhead_core.sqlite_domains import domain_database_path

if TYPE_CHECKING:
    import aiosqlite

    from bowhead_core.artifact_store import Artifact
    from bowhead_core.chat_turn_source import ChatTurnUserSource
    from bowhead_core.tool_registry import ToolExecutionContext

# These dictionaries are the existing serialized model-context and material
# envelopes. Their closed shape is checked here and by the original decoder.
Context = dict[str, Any]
CONTRACT = "ordinary-lead-loop.v10"
_PIN_FIELDS = ("filename", "mime", "size", "kind", "inject", "origin_query", "source")


class RetainedInputCapacityExceeded(OrdinaryContinuationUnavailable):
    """The real inputs exceed the existing optional-pause representation."""


def _context_ids(context: Context, key: str) -> list[str]:
    items = context.get(key)
    if not isinstance(items, list) or any(
        not isinstance(item, dict) or not isinstance(item.get("artifact_id"), str) or not item["artifact_id"] for item in items
    ):
        raise OrdinaryContinuationUnavailable()
    return [item["artifact_id"] for item in items]


def retained_ids(source: ChatTurnUserSource, context: Context, *, observed_ids: tuple[str, ...] | None = None) -> list[str]:
    """Require every parsed item to belong to the original selection or snapshot."""
    parsed = _context_ids(context, "parsed")
    pins = _context_ids(context, "pinned")
    if observed_ids is not None:
        supplied = [
            item["artifact_id"]
            for item in context["parsed"]
            if item["artifact_id"] in source.attachment_ids or item.get("extracted_text") or item.get("image_base64") or item.get("inventory")
        ]
        actual = list(dict.fromkeys([*supplied, *observed_ids]))
        if len(parsed) != len(set(parsed)) or len(pins) != len(set(pins)) or not set(source.attachment_ids) <= set(parsed):
            raise OrdinaryContinuationUnavailable()
        if len(set(actual) | set(source.attachment_ids)) > 32:
            raise RetainedInputCapacityExceeded()
        sizes = [item.get("size") for item in context["parsed"] if item["artifact_id"] in actual]
        if any(type(size) is not int or size < 0 for size in sizes):
            raise OrdinaryContinuationUnavailable()
        if sum(sizes) > MAX_LEAD_ATTACHMENT_TOTAL_BYTES:
            raise RetainedInputCapacityExceeded()
        return [identity for identity in actual if identity not in source.attachment_ids]
    if (
        len(parsed) != len(set(parsed))
        or len(pins) != len(set(pins))
        or len(set(parsed) | set(pins) | set(source.attachment_ids)) > 32
        or not set(source.attachment_ids) <= set(parsed) <= set(source.attachment_ids) | set(pins)
    ):
        raise OrdinaryContinuationUnavailable()
    sizes = [item.get("size") for item in context["parsed"]]
    if any(type(size) is not int or size < 0 for size in sizes) or sum(sizes) > MAX_LEAD_ATTACHMENT_TOTAL_BYTES:
        raise OrdinaryContinuationUnavailable()
    return [identity for identity in pins if identity not in source.attachment_ids]


def validate_shape(proof: Context, context: Context) -> None:
    _context_ids(context, "pinned")
    parsed = _context_ids(context, "parsed")
    if not isinstance(proof, dict) or set(proof) != {"artifacts", "attachments"} or not isinstance(proof["artifacts"], list):
        raise OrdinaryContinuationUnavailable()
    identities = []
    for item in proof["artifacts"]:
        if (
            not isinstance(item, dict)
            or set(item) != {"artifact_id", "provenance", "metadata_hash"}
            or not isinstance(item["provenance"], str)
            or item["provenance"] not in {"upload", "session_output"}
            or not isinstance(item["artifact_id"], str)
            or not 1 <= len(item["artifact_id"]) <= 128
            or not isinstance(item["metadata_hash"], str)
            or len(item["metadata_hash"]) != 64
            or any(char not in "0123456789abcdef" for char in item["metadata_hash"])
        ):
            raise OrdinaryContinuationUnavailable()
        identities.append(item["artifact_id"])
    if not 1 <= len(identities) <= 32 or len(set(identities)) != len(identities):
        raise OrdinaryContinuationUnavailable()
    parsed_ids = [identity for identity in parsed if identity in identities]
    snapshot = proof["attachments"]
    if parsed_ids:
        if (
            not isinstance(snapshot, dict)
            or not isinstance(snapshot.get("files"), list)
            or any(not isinstance(item, dict) for item in snapshot["files"])
            or [item.get("artifact_id") for item in snapshot["files"]] != parsed_ids
        ):
            raise OrdinaryContinuationUnavailable()
    elif snapshot is not None:
        raise OrdinaryContinuationUnavailable()


async def session_input_artifact(ctx: ToolExecutionContext, db: aiosqlite.Connection, identity: str) -> tuple[Artifact, str]:
    """Reauthorize Session provenance; the caller supplies the original selection."""
    # Read through the caller so its content-authority lease protects final consumption.
    store = ctx.artifact_store
    if (
        store is None
        or Path(domain_database_path(store.db_path, "core")).resolve() != Path(domain_database_path(ctx.session_store.db_path, "core")).resolve()
    ):
        raise OrdinaryContinuationUnavailable()
    matches = await store.list_for_session(session_id=ctx.session_id, user_id=ctx.user_id, artifact_id=identity, limit=1, connection=db)
    if len(matches) != 1:
        raise OrdinaryContinuationUnavailable()
    art = matches[0]
    if art.workspace_id != ctx.workspace_id or art.status != "ready":
        raise OrdinaryContinuationUnavailable()
    # The retained-context envelope does not retain bundle-root provenance.
    # Do not authorize a flattened child as an independent historical upload.
    async with db.execute(
        "SELECT 1 FROM chat_attachment_bundle_members WHERE bundle_artifact_id=? OR child_artifact_id=? LIMIT 1", (identity, identity)
    ) as cursor:
        if await cursor.fetchone() is not None:
            raise OrdinaryContinuationUnavailable()
    if art.kind == "user_upload":
        ready = await store.get_ready_uploads_for_chat([identity], user_id=ctx.user_id, workspace_id=ctx.workspace_id, connection=db)
        if identity not in ready:
            raise OrdinaryContinuationUnavailable()
        provenance = "upload"
    else:
        async with db.execute("SELECT 1 FROM turns WHERE id=? AND session_id=?", (art.created_by_turn, ctx.session_id)) as cursor:
            if not art.created_by_turn or art.created_by_turn == ctx.turn_id or await cursor.fetchone() is None:
                raise OrdinaryContinuationUnavailable()
        provenance = "session_output"
    return art, provenance


async def prepared_input_artifact(
    ctx: ToolExecutionContext, db: aiosqlite.Connection, identity: str, pins: list[dict], *, prepared_ids: tuple[str, ...] | None = None
) -> Artifact:
    """Match the original selection independently of bounded model metadata."""
    identities = _context_ids({"pinned": pins}, "pinned")
    if identities.count(identity) > 1:
        raise OrdinaryContinuationUnavailable()
    if prepared_ids is not None:
        if not isinstance(prepared_ids, tuple) or prepared_ids.count(identity) != 1:
            raise OrdinaryContinuationUnavailable()
    elif identities.count(identity) != 1:
        raise OrdinaryContinuationUnavailable()
    artifact, _ = await session_input_artifact(ctx, db, identity)
    if identity in identities:
        pin = pins[identities.index(identity)]
        if any(pin.get(key) != getattr(artifact, key) for key in _PIN_FIELDS):
            raise OrdinaryContinuationUnavailable()
    return artifact


async def capture_context(
    ctx: ToolExecutionContext, db: aiosqlite.Connection, source: ChatTurnUserSource, context: Context, *, observed_ids: tuple[str, ...] | None = None
) -> Context:
    evidence = []
    identities = retained_ids(source, context, observed_ids=observed_ids)
    pins = {item["artifact_id"]: item for item in context["pinned"]}
    for identity in identities:
        art, provenance = await session_input_artifact(ctx, db, identity)
        if observed_ids is None and any(pins[identity].get(key) != getattr(art, key) for key in _PIN_FIELDS):
            raise OrdinaryContinuationUnavailable()
        evidence.append({"artifact_id": identity, "provenance": provenance, "metadata_hash": _artifact_metadata_hash(art, contract=CONTRACT)})
    parsed_ids = [item["artifact_id"] for item in context["parsed"] if item["artifact_id"] in identities]
    proof = {"artifacts": evidence, "attachments": await seal_prepared_attachment_snapshot(parsed_ids, ctx, "") if parsed_ids else None}
    validate_shape(proof, context)
    return proof


async def validate_context(
    ctx: ToolExecutionContext,
    db: aiosqlite.Connection,
    source: ChatTurnUserSource,
    proof: Context,
    context: Context,
    *,
    read_bytes: bool,
    observed_ids: tuple[str, ...] | None = None,
) -> None:
    validate_shape(proof, context)
    if retained_ids(source, context, observed_ids=observed_ids) != [item["artifact_id"] for item in proof["artifacts"]]:
        raise OrdinaryContinuationUnavailable()
    for item in proof["artifacts"]:
        art, provenance = await session_input_artifact(ctx, db, item["artifact_id"])
        if provenance != item["provenance"] or _artifact_metadata_hash(art, contract=CONTRACT) != item["metadata_hash"]:
            raise OrdinaryContinuationUnavailable()
    snapshot = proof["attachments"]
    if snapshot is not None:
        if read_bytes:
            await reopen_prepared_attachment_snapshot([item["artifact_id"] for item in snapshot["files"]], ctx, snapshot)
        else:
            # Confirmation identity can be revoked independently of artifact
            # metadata. Repeat its read-only check inside final consumption.
            from bowhead_core.documents.upload_confirmation import confirmed_content_hash

            for item in snapshot["files"]:
                if item.get("content_deferred"):
                    art, _ = await session_input_artifact(ctx, db, item["artifact_id"])
                    if (
                        await confirmed_content_hash(ctx.artifact_store, art, user_id=ctx.user_id, workspace_id=ctx.workspace_id)
                        != item["content_hash"]
                    ):
                        raise OrdinaryContinuationUnavailable()


async def observed_session_inputs(ctx: ToolExecutionContext, db: aiosqlite.Connection, tools: list[dict]) -> tuple[str, ...]:
    """Keep earlier native inputs; current-Turn outputs retain their producer proof."""
    from bowhead_core.ordinary_lead_material import retained_file_input_ids

    identities = []
    for identity in retained_file_input_ids(tools):
        art = await ctx.artifact_store.get(identity, check_created_by=ctx.user_id, reject_null_owner=True, connection=db)
        if art is None:
            raise OrdinaryContinuationUnavailable()
        if art.created_by_turn != ctx.turn_id:
            identities.append(identity)
    return tuple(identities)
