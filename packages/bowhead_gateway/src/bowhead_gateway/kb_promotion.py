"""Explicit promotion of existing eligible bytes through one membership authority.

Classification remains advisory. Only a server-admitted original user action
can create membership; generated files also require explicit manual addition.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from bowhead_core.artifact_store import Artifact

log = logging.getLogger(__name__)

PromotionTrigger = Literal["auto", "manual"]

#: Agent-generated content **never** enters KB automatically (Verified Intelligence's
#: self-reference contamination defense). Require explicit user confirmation.
AGENT_PRODUCED_KINDS: frozenset[str] = frozenset({"deliverable", "canvas_export"})

#: Uploaded files are eligible only for explicit manual promotion.
AUTO_INDEXABLE_KINDS: frozenset[str] = frozenset({"user_upload"})

#: Artifact kinds explicitly promotable from Files. Runtime data must not become a trusted source,
#: even when its MIME can be parsed.
MANUAL_PROMOTABLE_KINDS: frozenset[str] = AGENT_PRODUCED_KINDS | AUTO_INDEXABLE_KINDS


class PromotionSkipped(Exception):
    """晋升未发生（不是错误，是规则）。`reason` 供日志/审计。"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _kb_allowed_mimes() -> frozenset[str]:
    from bowhead_gateway.routes.kb import _ALLOWED_MIMES

    return frozenset(_ALLOWED_MIMES)


def is_kb_promotable(artifact: Artifact) -> bool:
    """Return whether Files may offer the explicit KB promotion action."""
    kind = getattr(artifact, "kind", None) or "tool_data"
    return kind in MANUAL_PROMOTABLE_KINDS and (artifact.mime or "") in _kb_allowed_mimes()


def should_auto_index(artifact: Artifact) -> tuple[bool, str]:
    """Compatibility predicate: classification never authorizes KB membership."""
    return False, "explicit_knowledge_action_required"


async def promote_artifact_to_kb(
    *,
    artifact: Artifact,
    files_store: Any,
    scope: str = "personal",
    scope_id: str = "",
    workspace_id: str = "",
    team_id: str = "",
    user_id: str,
    trigger: PromotionTrigger,
    worker: Any | None = None,
    action: Any | None = None,
) -> str:
    """Admit membership over existing bytes and process it once.

    Only a server-bound original user action can create a membership. Replays
    compare the source and destination and never revive a retired action.
    """
    kind = getattr(artifact, "kind", None) or "tool_data"
    # Guard here rather than in callers, automatically constraining every new call site.
    if trigger == "auto" and kind in AGENT_PRODUCED_KINDS:
        raise PromotionSkipped("agent_produced_cannot_auto_index")
    if (artifact.mime or "") not in _kb_allowed_mimes():
        raise PromotionSkipped(f"mime_not_parsable:{artifact.mime}")

    from bowhead_core.kb.ingestion import canonical_hash

    if not is_kb_promotable(artifact):
        raise PromotionSkipped("artifact_kind_not_promotable")
    if trigger != "manual" or action is None:
        raise PromotionSkipped("explicit_knowledge_action_required")
    action.require(user_id=user_id, team_id=team_id)
    fingerprint = canonical_hash(
        {"id": artifact.id, "created_at": artifact.created_at, "path": artifact.path, "size": artifact.size, "mime": artifact.mime}
    )
    file_id, created, _ = await files_store.create_admitted(
        action=action,
        source_fingerprint=fingerprint,
        artifact_id=artifact.id,
        filename=artifact.filename,
        mime_type=artifact.mime,
        size_bytes=artifact.size,
        scope=scope,
        scope_id=scope_id,
        workspace_id=workspace_id,
        team_id=team_id,
        uploaded_by=user_id,
        use_llm_marker=False,
        doc_type_id=None,
    )
    log.info(
        "kb_promotion.created aid=%s file_id=%s trigger=%s scope=%s kind=%s",
        artifact.id,
        file_id,
        trigger,
        scope,
        kind,
    )
    if worker is not None and created:
        try:
            await worker.parse_and_index(file_id)
        except Exception as e:  # noqa: BLE001 - Parse failure must not roll back the created row.
            log.warning("kb_promotion.parse_failed file_id=%s error=%s", file_id, e)
    return file_id
