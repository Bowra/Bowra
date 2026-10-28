"""Typed canonical user input persisted for one Chat Turn."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any

from bowhead_core.chat_references import InputDocument
from bowhead_core.document_revision_target import DocumentRevisionTarget


class ReferenceExecutionUnavailableError(ValueError):
    """A structured source cannot execute through the legacy text adapter."""

    def __init__(self) -> None:
        super().__init__("reference_execution_adapter_unavailable")


@dataclass(frozen=True, slots=True)
class ChatTurnUserSource:
    turn_id: str
    session_id: str
    user_id: str
    workspace_id: str
    user_text: str
    attachment_ids: tuple[str, ...]
    attachment_refs: tuple[dict[str, Any], ...]
    parts: tuple[dict[str, Any], ...] | None
    input_document: InputDocument | None = None
    document_revision_target: DocumentRevisionTarget | None = None

    def __post_init__(self) -> None:
        identities = (
            self.turn_id,
            self.session_id,
            self.user_id,
            self.workspace_id,
        )
        if any(not isinstance(value, str) or not value for value in identities):
            raise ValueError("chat Turn user source identity is invalid")
        if not isinstance(self.user_text, str):
            raise TypeError("chat Turn user source text is invalid")
        if any(not isinstance(value, str) or not value for value in self.attachment_ids):
            raise ValueError("chat Turn attachment identity is invalid")
        if any(not isinstance(value, dict) for value in self.attachment_refs):
            raise TypeError("chat Turn attachment reference is invalid")
        if self.parts is not None and any(not isinstance(value, dict) for value in self.parts):
            raise TypeError("chat Turn source part is invalid")
        if self.document_revision_target is not None:
            if not isinstance(self.document_revision_target, DocumentRevisionTarget):
                raise TypeError("chat Turn document revision target is invalid")
            target = DocumentRevisionTarget.model_validate(self.document_revision_target.model_dump(mode="python", warnings=False))
            if target.session_id != self.session_id or self.input_document is not None:
                raise ValueError("chat Turn document revision scope or mode is invalid")
            object.__setattr__(self, "document_revision_target", target)
        if self.input_document is not None:
            if not isinstance(self.input_document, InputDocument):
                raise TypeError("chat Turn input document is invalid")
            # Detach nested parameters and revalidate even a model_copy(update=...).
            # Python mode preserves invalid mutated types for strict validation;
            # omit absent resource fields to keep the validated byte budget stable.
            document = InputDocument.model_validate(self.input_document.model_dump(mode="python", exclude_none=True, warnings=False))
            if document.display_text() != self.user_text:
                raise ValueError("chat Turn input document display projection changed")
            if self.parts is not None and len(self.parts) > 1:
                raise ValueError("chat Turn input document requires one source message")
            object.__setattr__(self, "input_document", document)


def chat_turn_user_source_projection(source: ChatTurnUserSource) -> dict[str, object]:
    """Return the complete JSON projection used for strict source identity checks."""

    if not isinstance(source, ChatTurnUserSource):
        raise TypeError("chat Turn user source is invalid")
    projection: dict[str, object] = {
        "attachment_ids": list(source.attachment_ids),
        "attachment_refs": list(source.attachment_refs),
        "parts": list(source.parts) if source.parts is not None else None,
        "session_id": source.session_id,
        "turn_id": source.turn_id,
        "user_id": source.user_id,
        "user_text": source.user_text,
        "workspace_id": source.workspace_id,
    }
    # Keep every legacy source digest byte-compatible, including Terminal v1.
    if source.input_document is not None:
        projection["input_document"] = source.input_document.model_dump(mode="json", exclude_none=True)
    if source.document_revision_target is not None:
        projection["document_revision_target"] = source.document_revision_target.model_dump(mode="json")
    return projection


def snapshot_chat_turn_user_source(source: ChatTurnUserSource) -> ChatTurnUserSource:
    """Detach nested JSON values before the first persistence await."""

    if not isinstance(source, ChatTurnUserSource):
        raise TypeError("chat Turn user source is invalid")
    return ChatTurnUserSource(
        turn_id=source.turn_id,
        session_id=source.session_id,
        user_id=source.user_id,
        workspace_id=source.workspace_id,
        user_text=source.user_text,
        attachment_ids=tuple(source.attachment_ids),
        attachment_refs=tuple(copy.deepcopy(source.attachment_refs)),
        parts=(tuple(copy.deepcopy(source.parts)) if source.parts is not None else None),
        input_document=source.input_document,
        document_revision_target=source.document_revision_target,
    )


def current_attachment_positions(source: ChatTurnUserSource) -> dict[str, int]:
    """Map canonical UI references, not flattened bundle children, to ordinals.

    Multi-part messages and repeated/ambiguous references need disambiguation.
    This proves provenance only: neither an ordinal nor a filename proves role.
    """
    if source.parts is not None and len(source.parts) > 1:
        return {}
    positions: dict[str, int] = {}
    for index, ref in enumerate(source.attachment_refs, 1):
        ids = ref.get("bundleArtifactIds") or [ref.get("fileId")]
        if not isinstance(ids, list) or not ids or not all(isinstance(aid, str) and aid for aid in ids):
            return {}
        for aid in ids:
            if aid in positions:
                return {}
            positions[aid] = index
    return positions if list(positions) == list(source.attachment_ids) else {}
