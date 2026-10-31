"""Explicit membership actions; neither classification nor Tool arguments are authority."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from typing import Literal


def explicit_ingestion_enabled() -> bool:
    return os.environ.get("FEATURE_KNOWLEDGE_EXPLICIT_INGESTION", "1") == "1"


def management_enabled() -> bool:
    return os.environ.get("FEATURE_KNOWLEDGE_MANAGEMENT_PLUGIN", "1") == "1"


class KnowledgeWriteError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class KnowledgeAction:
    """Created by original UI/request admission, never deserialized from Tool input."""

    action_ref: str
    origin: Literal["kb_ui", "original_request", "admin_import"]
    actor_user_id: str
    team_id: str
    workspace_id: str

    def require(self, *, user_id: str, team_id: str, addition: bool = True) -> None:
        if addition and not explicit_ingestion_enabled():
            raise KnowledgeWriteError("knowledge_ingestion_admission_closed")
        if (
            not self.actor_user_id
            or not self.team_id
            or not self.action_ref
            or len(self.action_ref) > 256
            or (self.actor_user_id, self.team_id) != (user_id, team_id)
        ):
            raise KnowledgeWriteError("knowledge_user_action_required")


def canonical_hash(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
