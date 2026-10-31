"""Native declarations of retained files; the ordered Tool event is the receipt."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from bowhead_core._db import open_shared_ctx
from bowhead_core.chat_reference_scope import ReferenceRequestScope, require_reference_request_scope
from bowhead_core.results_artifact_policy import results_internal_artifact_sql

CONTRACT = "artifact-presentation.v1"
MAX_METADATA_BYTES = 64 * 1024
# CASE protects historical malformed JSON, including while building the index.
PRESENTATION_EVENT_SQL = """event_type='tool_result' AND CASE WHEN json_valid(payload_json) THEN
json_type(payload_json,'$.result_meta.artifact_presentation.contract')='text'
AND json_extract(payload_json,'$.result_meta.artifact_presentation.origin') IN ('explicit_tool','producer_declared')
AND json_type(payload_json,'$.result_meta.artifact_presentation.items')='array'
AND json_array_length(payload_json,'$.result_meta.artifact_presentation.items') BETWEEN 1 AND 32
AND length(json_extract(payload_json,'$.result_meta.artifact_presentation_receipt.call_key'))=64
END"""


class PresentationItem(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    artifact_id: str = Field(min_length=1, max_length=128, pattern=r"^art_[A-Za-z0-9_-]+$")
    title: str | None = Field(default=None, max_length=120)
    description: str | None = Field(default=None, max_length=256)


class PresentationArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    artifacts: list[PresentationItem] = Field(min_length=1, max_length=16)


class PresentationDeclaration(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    contract: Literal["artifact-presentation.v1"] = CONTRACT
    origin: Literal["explicit_tool", "producer_declared"]
    items: list[PresentationItem] = Field(min_length=1, max_length=32)


class PresentationUnavailable(ValueError):
    """A selected file cannot be presented under the current authority."""


class PresentationContractUnsupported(ValueError):
    """The installed reader cannot interpret a native receipt version."""


class PresentationStorageUnavailable(RuntimeError):
    """Original byte availability could not be established."""


def project_declaration(metadata: dict) -> PresentationDeclaration:
    raw = metadata.get("artifact_presentation")
    if not isinstance(raw, dict) or raw.get("contract") != CONTRACT:
        raise PresentationContractUnsupported("artifact_presentation_contract_unsupported")
    return PresentationDeclaration.model_validate(raw)


class NativePresentationResult(dict):
    """In-process provenance which JSON returned by MCP or a Skill cannot forge."""

    def __init__(self, result: dict, *, declaration: dict, receipt: dict, persisted: bool = False):
        super().__init__(result)
        self.declaration = declaration
        self.receipt = receipt
        self.persisted = persisted

    def result_metadata(self) -> dict:
        return {"artifact_presentation": self.declaration, "artifact_presentation_receipt": self.receipt}


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def normalize_items(items: list[PresentationItem]) -> list[dict]:
    unique: dict[str, dict] = {}
    for item in items:
        unique.setdefault(item.artifact_id, item.model_dump(exclude_none=True))
    normalized = list(unique.values())
    if len(_canonical(normalized).encode("utf-8")) > MAX_METADATA_BYTES:
        raise PresentationUnavailable("artifact_presentation_metadata_too_large")
    return normalized


async def _require_scope(ctx) -> None:
    store = ctx.artifact_store
    if (
        store is None
        or any(not isinstance(value, str) or not value for value in (ctx.team_id, ctx.user_id, ctx.workspace_id, ctx.session_id, ctx.turn_id))
        or ctx.eval_datarc is not None
        or ctx.artifact_presentation_call is None
        or type(ctx.execution_generation) is not int
        or ctx.execution_generation < 1
    ):
        raise PresentationUnavailable("artifact_presentation_not_allowed")
    async with open_shared_ctx(store.db_path) as db:
        await require_reference_request_scope(
            db,
            ReferenceRequestScope(team_id=ctx.team_id, user_id=ctx.user_id, workspace_id=ctx.workspace_id, session_id=ctx.session_id),
        )
        async with db.execute(
            "SELECT 1 FROM turns WHERE id=? AND session_id=? AND execution_generation=?",
            (ctx.turn_id, ctx.session_id, ctx.execution_generation),
        ) as cursor:
            if await cursor.fetchone() is None:
                raise PresentationUnavailable("artifact_presentation_not_allowed")


async def _available(ctx, artifact_id: str, *, produced: bool = False):
    store = ctx.artifact_store
    artifact = await store.get(artifact_id, check_created_by=ctx.user_id, reject_null_owner=True)
    if artifact is None or artifact.status != "ready" or artifact.workspace_id != ctx.workspace_id:
        raise PresentationUnavailable("artifact_presentation_reference_unavailable")
    if produced and (artifact.created_by_turn != ctx.turn_id or artifact.kind != "deliverable"):
        raise PresentationUnavailable("artifact_presentation_reference_unavailable")
    if artifact.producer_kind == "template_document_v1":
        from bowhead_core.documents.file_access import published_file_identity

        if await published_file_identity(store.db_path, artifact_id=artifact_id, user_id=ctx.user_id) is None:
            raise PresentationUnavailable("artifact_presentation_reference_unavailable")
    internal, parameters = results_internal_artifact_sql("a")
    async with (
        open_shared_ctx(store.db_path) as db,
        db.execute(
            f"SELECT 1 FROM artifacts a WHERE a.id=? AND NOT {internal} "
            "AND NOT EXISTS (SELECT 1 FROM artifact_purge_claims p WHERE p.artifact_id=a.id)",
            (artifact_id, *parameters),
        ) as cursor,
    ):
        if await cursor.fetchone() is None:
            raise PresentationUnavailable("artifact_presentation_reference_unavailable")
    try:
        head = await store.backend.head(artifact.path)
    except Exception as exc:
        raise PresentationStorageUnavailable("artifact_presentation_storage_unavailable") from exc
    if head is None or head.size != artifact.size:
        raise PresentationUnavailable("artifact_presentation_reference_unavailable")
    return artifact


def _receipt(ctx, tool_name: str, items: list[dict]) -> dict:
    execution_id, call_id = ctx.artifact_presentation_call
    identity = {
        "team_id": ctx.team_id,
        "user_id": ctx.user_id,
        "workspace_id": ctx.workspace_id,
        "session_id": ctx.session_id,
        "turn_id": ctx.turn_id,
        "generation": ctx.execution_generation,
        "execution_id": execution_id,
        "tool_name": tool_name,
        "call_id": call_id,
    }
    return {
        **identity,
        "call_key": hashlib.sha256(_canonical(identity).encode()).hexdigest(),
        "arguments_digest": hashlib.sha256(_canonical(items).encode()).hexdigest(),
    }


async def present_artifacts(arguments: dict, ctx) -> dict:
    try:
        if ctx.artifact_presentation_entry is not True:
            raise PresentationUnavailable("artifact_presentation_not_allowed")
        items = normalize_items(PresentationArguments.model_validate(arguments).artifacts)
        await _require_scope(ctx)
        receipt = _receipt(ctx, "present_artifacts", items)
        async with (
            open_shared_ctx(ctx.artifact_store.db_path) as db,
            db.execute(
                f"SELECT payload_json FROM turn_events WHERE turn_id=? AND {PRESENTATION_EVENT_SQL} "
                "AND CASE WHEN json_valid(payload_json) THEN "
                "json_extract(payload_json,'$.result_meta.artifact_presentation_receipt.call_key') END=?",
                (ctx.turn_id, receipt["call_key"]),
            ) as cursor,
        ):
            previous = await cursor.fetchone()
        if previous is not None:
            metadata = json.loads(previous[0])["result_meta"]
            if metadata["artifact_presentation_receipt"] != receipt:
                raise PresentationUnavailable("artifact_presentation_call_conflict")
            declaration = project_declaration(metadata).model_dump(exclude_none=True)
            if declaration["items"] != items or declaration["origin"] != "explicit_tool":
                raise PresentationUnavailable("artifact_presentation_call_conflict")
        else:
            declaration = {"contract": CONTRACT, "origin": "explicit_tool", "items": items}
        # Recheck current access and original availability even for a receipt replay.
        outputs = []
        for item in items:
            artifact = await _available(ctx, item["artifact_id"])
            outputs.append({**item, "name": artifact.filename, "mime": artifact.mime, "size": artifact.size})
        await _require_scope(ctx)
        return NativePresentationResult(
            {"ok": True, "artifacts": items, "output_artifacts": outputs}, declaration=declaration, receipt=receipt, persisted=previous is not None
        )
    except ValidationError:
        return {
            "ok": False,
            "error": "artifact_presentation_invalid_arguments",
            "retryable": False,
            "error_message": "Select 1–16 retained artifact IDs; title is limited to 120 characters and description to 256. Do not supply paths or file bytes.",
        }
    except PresentationContractUnsupported:
        return {
            "ok": False,
            "error": "artifact_presentation_contract_unsupported",
            "retryable": False,
            "error_message": "This delivery receipt requires a newer Bowhead version. Ask the administrator to update the application before reusing it.",
        }
    except (PresentationUnavailable, ValueError) as exc:
        code = str(exc) if str(exc).startswith("artifact_presentation_") else "artifact_presentation_not_allowed"
        guidance = {
            "artifact_presentation_not_allowed": "Only the current user-entry Agent with active Space access can present files. Helpers should return retained artifact IDs to that Agent.",
            "artifact_presentation_call_conflict": "This call identity already belongs to a different selection. Recover the original call, or use a new call for a changed selection.",
            "artifact_presentation_metadata_too_large": "Reduce the titles or descriptions, or split the selection into additional calls.",
        }
        return {
            "ok": False,
            "error": code,
            "retryable": False,
            "error_message": guidance.get(
                code,
                "Select an accessible retained file in the current Space. Private document drafts must finish their original review before delivery.",
            ),
        }
    except (PresentationStorageUnavailable, OSError, TimeoutError):
        return {
            "ok": False,
            "error": "artifact_presentation_storage_unavailable",
            "retryable": True,
            "error_message": "Original file availability could not be checked. Retry presenting the retained files when storage is available; do not regenerate them.",
        }


async def normalize_producer_declaration(tool_name: str, arguments: dict, result: Any, ctx) -> Any:
    """Stamp only native producers' explicit, individually retained outputs."""
    if not isinstance(result, dict) or not getattr(ctx, "artifact_presentation_call", None) or not getattr(ctx, "execution_generation", None):
        return result
    inner = arguments.get("input", {}) if tool_name == "skill_invoke" else arguments
    if not isinstance(inner, dict):
        return result
    if tool_name == "skill_invoke" and arguments.get("name") == "latex-report":
        requested = {"main.tex", "report.pdf"}
    elif inner.get("purpose") == "deliverable" and isinstance(inner.get("deliverable_outputs"), list):
        requested = {name for name in inner["deliverable_outputs"] if isinstance(name, str)}
    else:
        return result
    outputs = result.get("output_artifacts")
    if not isinstance(outputs, list):
        return result
    await _require_scope(ctx)
    items: list[PresentationItem] = []
    for output in outputs:
        if not isinstance(output, dict) or output.get("name") not in requested or output.get("data_role") == "stdout":
            continue
        try:
            item = PresentationItem(artifact_id=output.get("artifact_id"))
            artifact = await _available(ctx, item.artifact_id, produced=True)
            if artifact.filename == output["name"]:
                items.append(item)
        except (ValidationError, PresentationUnavailable):
            continue
    if not items:
        return result
    normalized = normalize_items(items)
    # Keep the existing producer outcome, including missing-output/compile errors.
    return NativePresentationResult(
        result,
        declaration=PresentationDeclaration(origin="producer_declared", items=normalized).model_dump(exclude_none=True),
        receipt=_receipt(ctx, tool_name, normalized),
    )


def register_artifact_presentation(registry) -> None:
    from bowhead_core.terminal_effect_contract import TOOL_EFFECT_READ_ONLY
    from bowhead_core.tool_registry import ToolSchema, ToolSpec

    registry.register(
        ToolSpec(
            schema=ToolSchema(
                name="present_artifacts",
                description=(
                    "Present existing retained files in this conversation without copying or regenerating them. "
                    "Use accessible artifact IDs in the current Space. This does not publish, share, edit or validate file contents. "
                    "Supply 1–16 files per call; optional title is at most 120 characters and description at most 256."
                ),
                input_schema=PresentationArguments.model_json_schema(),
            ),
            handler=present_artifacts,
            risk_level="low",
            needs_approval=False,
            # The handler only reads. The native ordered tool_result writer owns
            # the association, with no independent provider side effect.
            effect_spec=TOOL_EFFECT_READ_ONLY,
        )
    )
