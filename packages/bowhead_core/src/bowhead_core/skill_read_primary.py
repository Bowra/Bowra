"""Prepare Skill reads from canonical source, never caller-supplied selections.

This read-only adapter neither admits a submission nor creates composition or a
worker. It closes the source-binding gap of the candidate preparation adapter.
"""

from __future__ import annotations

from bowhead_core._db import open_shared_ctx
from bowhead_core.agent_execution_binding import AgentExecutionBindingStore
from bowhead_core.chat_reference_execution import ReferencePrimaryInput, read_reference_skill_primary_in_connection
from bowhead_core.chat_reference_scope import ReferenceRequestScope, require_reference_request_scope
from bowhead_core.chat_reference_storage import ReferenceScope
from bowhead_core.chat_references import ReferenceNode, SkillResourceRef
from bowhead_core.skill_baseline_material import prepare_baseline_selected_reads, prepare_primary_skill_baseline
from bowhead_core.skill_read_extensions import prepare_extended_skill_reads, primary_extension_policy, require_current_extension_policy
from bowhead_core.skill_read_receiver import PreparedReceiverSkillReads, SkillReadReceiverError, primary_skill_read_receiver
from bowhead_core.skill_read_render import RenderedSkillReads, render_prepared_skill_reads
from bowhead_core.skill_read_resolution import SkillReadSourceResolver


async def prepare_reference_primary_skill_reads(
    sources: SkillReadSourceResolver, *, scope: ReferenceRequestScope, turn_id: str
) -> tuple[PreparedReceiverSkillReads, RenderedSkillReads]:
    """Prepare the original explicit Skill parameters for an existing primary.

    The source store and bindings share the source resolver's server-selected DB.
    All reads are current, transaction-scoped and query-only; no client factory,
    nodes, Agent definition or confirmation flag is accepted as source evidence.
    This currently supports initial parameters only, not clarification resolutions.
    """
    try:
        scope = ReferenceRequestScope.model_validate(scope.model_dump(mode="python"))
        if scope.session_id is None:
            raise ValueError("reference_session_required")
    except Exception:
        raise SkillReadReceiverError("reference_skill_receiver_scope_mismatch") from None
    bindings = AgentExecutionBindingStore(sources.db_path)
    original: ReferencePrimaryInput | None = None
    extension = None

    async def read() -> ReferencePrimaryInput:
        nonlocal extension
        try:
            async with open_shared_ctx(sources.db_path) as db:
                await db.execute("PRAGMA query_only=ON")
                await db.execute("BEGIN")
                try:
                    epoch = await require_reference_request_scope(db, scope)
                    primary = await read_reference_skill_primary_in_connection(
                        db, scope=ReferenceScope(**scope.model_dump(), policy_epoch=epoch), turn_id=turn_id, bindings=bindings
                    )
                    current = await primary_extension_policy(db, primary, primary_skill_read_receiver(primary, scope, turn_id))
                    if original is not None and current != extension:
                        raise SkillReadReceiverError("reference_skill_receiver_changed")
                    extension = current
                finally:
                    await db.rollback()
        except Exception:
            raise SkillReadReceiverError("reference_skill_receiver_unavailable") from None
        # The first read chooses source parameters. A later read cannot switch
        # them to a different source/execution before the receiver's own checks.
        if original is not None and (
            primary.source != original.source or primary.plan != original.plan or primary.binding.execution_id != original.binding.execution_id
        ):
            raise SkillReadReceiverError("reference_skill_receiver_changed") from None
        return primary

    original = await read()
    document = original.source.input_document
    assert document is not None  # required by the canonical reader
    selections = [node for node in document.nodes if isinstance(node, ReferenceNode) and isinstance(node.resource, SkillResourceRef)]
    baseline = await prepare_primary_skill_baseline(sources, scope=scope, turn_id=turn_id, primary_factory=read)
    prepared = (
        await prepare_extended_skill_reads(sources, baseline, [node.resource for node in selections], extension)
        if extension is not None and selections
        else await prepare_baseline_selected_reads(baseline, [node.resource for node in selections])
    )
    if prepared.extension is not None:
        async with open_shared_ctx(sources.db_path) as db:
            await db.execute("PRAGMA query_only=ON")
            await db.execute("BEGIN")
            await require_current_extension_policy(db, prepared.receiver, prepared.closure, prepared.extension)
    return prepared, render_prepared_skill_reads(prepared, selections)
