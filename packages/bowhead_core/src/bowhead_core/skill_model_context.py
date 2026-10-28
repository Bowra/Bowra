"""Mandatory preparation evidence, distinct from physical sends and billing.

No prompt/parameter bytes or guessed invocation IDs are stored here. Each
logical input has one immutable fact; retries compare it rather than count uses.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from bowhead_router.invocation import canonical_transport_digest
from bowhead_router.types import LLMRequest

from bowhead_core._db import open_shared_ctx
from bowhead_core.chat_reference_storage import _row
from bowhead_core.reference_mcp_prompt_model import ReferenceMcpPromptModelAccess
from bowhead_core.skill_read_access import ReferenceSkillReadAccess
from bowhead_core.skill_read_model_input import RequiredSkillModelInput
from bowhead_core.tool_registry import ToolExecutionContext


@dataclass(frozen=True, slots=True)
class ModelContextReceipt:
    turn_id: str
    context_id: str
    execution_id: str
    generation: int
    subagent_id: str
    composition_digest: str
    input_hash: str
    required_content_hash: str


def _input_hash(request: LLMRequest) -> str:
    # This describes logical assembled input, not target/sampling settings or
    # physical wire bytes. Router model switches preserve this input identity.
    return "sha256:" + canonical_transport_digest(
        {
            "contract": "reference-model-context.v1",
            "system": request.system,
            "messages": [asdict(m) for m in request.messages],
            "tools": [asdict(t) for t in request.tools],
            "response_format": request.response_format,
        }
    )


async def record_model_context(
    *,
    request: LLMRequest,
    access: ReferenceSkillReadAccess | ReferenceMcpPromptModelAccess,
    ctx: ToolExecutionContext,
    content: str,
    content_hash: str,
) -> ModelContextReceipt:
    """Authorize and durably save the assembled context before a physical send.

    Commit failure is a local stop, even if the commit may have succeeded. The
    immutable fact authorizes neither another dispatch nor a runtime recovery.
    """
    if type(access) not in (ReferenceSkillReadAccess, ReferenceMcpPromptModelAccess) or not isinstance(ctx, ToolExecutionContext):
        raise ValueError("reference_model_context_unavailable")
    if type(request.input_context_id) is not str or not re.fullmatch(r"ric_[0-9a-f]{32}", request.input_context_id):
        raise ValueError("reference_model_context_unavailable")
    context_id = request.input_context_id
    input_hash = _input_hash(request)
    if type(access) is ReferenceSkillReadAccess:
        await ctx.ensure_reference_skill_context()

    def require_receiver():
        if type(access) is ReferenceMcpPromptModelAccess:
            access.require_receiver(ctx)
            return
        access.require_model_receiver(ctx)

    require_receiver()
    async with open_shared_ctx(access.source_db_path) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            if type(access) is ReferenceMcpPromptModelAccess:
                composition, original = await access.read_in_connection(db, ctx=ctx)
            else:
                composition, rendered, _ = await access.read_model_material_in_connection(db, ctx=ctx)
                original = RequiredSkillModelInput.from_rendered(rendered)
            if (content, content_hash) != (original.content, original.content_hash):
                raise ValueError("reference_model_context_unavailable")
            expected = {
                "turn_id": access.turn_id,
                "context_id": context_id,
                "schema_version": 1,
                "kind": "context_assembled",
                "resolution_seq": 1,
                "execution_id": access.execution_id,
                "created_generation": access.generation,
                "subagent_id": access.subagent_id,
                "composition_digest": composition.digest,
                "input_hash": input_hash,
                "required_content_hash": content_hash,
                "retention_state": "available",
            }
            prior = await _row(db, "SELECT * FROM chat_reference_model_contexts WHERE turn_id=? AND context_id=?", (access.turn_id, context_id))
            if prior is not None:
                if any(prior[key] != value for key, value in expected.items()):
                    raise ValueError("reference_model_context_conflict")
            else:
                values = {**expected, "created_at": datetime.now(UTC).isoformat()}
                await db.execute(
                    "INSERT INTO chat_reference_model_contexts (" + ",".join(values) + ") VALUES (" + ",".join("?" for _ in values) + ")",
                    tuple(values.values()),
                )
            await access.require_worker_in_connection(db)
            require_receiver()
            if request.input_context_id != context_id or _input_hash(request) != input_hash:
                raise ValueError("reference_model_context_changed")
            await db.commit()
            return ModelContextReceipt(
                access.turn_id,
                context_id,
                access.execution_id,
                access.generation,
                access.subagent_id,
                composition.digest,
                input_hash,
                content_hash,
            )
        except BaseException:
            await db.rollback()
            raise
