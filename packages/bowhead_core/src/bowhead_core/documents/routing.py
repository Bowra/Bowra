"""Read compatibility for historical encrypted document-routing snapshots.

New turns do not classify document intent. These fields preserve saved card bytes
and source provenance only; they never select tools or constrain execution.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

from bowhead_core.chat_turn_source import chat_turn_user_source_projection
from bowhead_core.documents.request import UserQuote
from bowhead_core.ordinary_lead_continuation import content_hash

if TYPE_CHECKING:
    from bowhead_core.chat_turn_source import ChatTurnUserSource


class DocumentRoutingSnapshot(BaseModel):
    """Frozen historical wire shape, not a current routing decision."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    source_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    mode: Literal["template", "ordinary"]
    evidence: str = Field(min_length=1, max_length=8000)
    inspection_quote: str | None = Field(default=None, min_length=1, max_length=8000)
    question_quote: str | None = Field(default=None, min_length=1, max_length=8000)

    def verify_source(self, source: ChatTurnUserSource) -> None:
        """Check retained provenance without interpreting user intent."""
        if self.source_hash != content_hash(chat_turn_user_source_projection(source)):
            raise ValueError("document_routing_source_changed")
        for field in ("evidence", "inspection_quote", "question_quote"):
            quote = getattr(self, field)
            if quote is not None:
                UserQuote(text=quote).verify(source.user_text, field=field)
        if self.mode == "ordinary" and (self.inspection_quote is not None or self.question_quote is not None):
            raise ValueError("document_routing_invalid")
