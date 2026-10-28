"""Request-local role proposals. Grounding is not semantic approval or byte freeze."""

from __future__ import annotations

from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator
from pydantic_core import PydanticCustomError

from bowhead_core.chat_turn_source import ChatTurnUserSource, current_attachment_positions

Identity = Annotated[str, Field(min_length=1, max_length=128)]


class DocumentRequestError(ValueError):
    """Stable domain code; messages never include user text or file contents."""

    def __init__(self, code: str, *, field: str | None = None) -> None:
        self.code = code
        self.field = field
        super().__init__(code)


class _FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")


class DocumentScope(_FrozenModel):
    team_id: Identity
    workspace_id: Identity
    user_id: Identity

    def sql_params(self) -> tuple[str, str, str]:
        return self.team_id, self.workspace_id, self.user_id


class DocumentModelSelection(_FrozenModel):
    """Server-resolved planner identity, not model-authored Tool arguments."""

    provider: Annotated[str, Field(min_length=1)]
    model: Annotated[str, Field(min_length=1)]


class UserQuote(_FrozenModel):
    # Models can copy a short unique quote without counting Chinese/emoji.
    # An optional trusted offset disambiguates repeated text: Python Unicode
    # code points, not JS UTF-16. No normalization or stripping negation.
    start: Annotated[int, Field(ge=0)] | None = None
    text: Annotated[str, Field(min_length=1, max_length=8000)]

    def verify(self, user_text: str, *, field: str = "evidence") -> None:
        if not self.text.strip():
            raise DocumentRequestError("document_evidence_mismatch", field=field)
        start = self.start if self.start is not None else user_text.find(self.text)
        if start < 0 or user_text[start : start + len(self.text)] != self.text:
            raise DocumentRequestError("document_evidence_mismatch", field=field)
        if self.start is None and user_text.find(self.text, start + 1) >= 0:
            raise DocumentRequestError("document_evidence_ambiguous", field=field)


class InputRoleArguments(_FrozenModel):
    artifact_id: Identity
    role: Literal["template", "reference", "data"]
    evidence: UserQuote


class InputRoleProposal(InputRoleArguments):
    # None means an existing pin in this exact session, NOT a guessed ordinal,
    # library-wide search, successful template family, or authorization grant.
    current_message_position: Annotated[int, Field(ge=1)] | None = Field(
        default=None,
        description="For a current-message file, copy its provided 1-based position. For an existing session pin, omit this field. Never infer pin order.",
    )


class DocumentProposal(_FrozenModel):
    action: Literal["generate"] = "generate"
    evidence: UserQuote
    inputs: Annotated[tuple[InputRoleProposal, ...], Field(min_length=1, max_length=32)]
    requested_outputs: tuple[Literal["source", "pdf"], ...] = ("source",)

    @model_validator(mode="after")
    def validate_shape(self) -> Self:
        if len({item.artifact_id for item in self.inputs}) != len(self.inputs):
            raise PydanticCustomError("document_duplicate_input", "duplicate document input")
        if sum(item.role == "template" for item in self.inputs) != 1:
            raise PydanticCustomError("document_template_count", "one template proposal is required")
        if self.requested_outputs not in (("source",), ("source", "pdf")):
            raise PydanticCustomError("document_output_selection", "generate requires source, optionally followed by pdf")
        return self


class DocumentGenerationArguments(DocumentProposal):
    """Generation requires the request quote as well as every input quote."""

    action: Literal["generate"]
    inputs: Annotated[
        tuple[InputRoleArguments, ...],
        Field(
            min_length=1,
            max_length=32,
            description="Use each artifact only once with one role and exactly one template. A separate data file is optional when the user supplies the values.",
        ),
    ]


def resolve_proposal_positions(proposal: DocumentProposal, source: ChatTurnUserSource) -> DocumentProposal:
    """Derive transport metadata from canonical references, never model hints."""
    positions = current_attachment_positions(source)
    if source.attachment_ids and not positions:
        raise DocumentRequestError("document_reference_ambiguous")
    resolved = proposal.model_copy(
        update={"inputs": tuple(item.model_copy(update={"current_message_position": positions.get(item.artifact_id)}) for item in proposal.inputs)}
    )
    verify_proposal_source(resolved, source)
    return resolved


class DocumentQuestionArguments(_FrozenModel):
    action: Literal["list_questions"]


class DocumentAnswerArguments(_FrozenModel):
    action: Literal["answer"]
    job_id: Identity
    evidence: UserQuote


class DocumentRevisionArguments(_FrozenModel):
    action: Literal["revise"]
    evidence: UserQuote


# Advertisements and parsing share the same action-specific required fields.
DOCUMENT_TOOL_ARGUMENTS = TypeAdapter(
    Annotated[
        DocumentGenerationArguments | DocumentQuestionArguments | DocumentAnswerArguments | DocumentRevisionArguments, Field(discriminator="action")
    ]
)


def document_tool_schema() -> dict[str, Any]:
    """Expose fields directly while retaining the action-specific validator.

    Root-union-only advertisements have produced stringified nested arguments.
    Root properties make the same types directly visible without expanding
    every nested reference or accepting JSON strings in place of objects.
    """
    schema = DOCUMENT_TOOL_ARGUMENTS.json_schema()
    properties: dict[str, Any] = {}
    actions: list[str] = []
    for branch in schema["oneOf"]:
        fields = schema["$defs"][branch["$ref"].rsplit("/", 1)[-1]]["properties"]
        actions.append(fields["action"]["const"])
        for name, field in fields.items():
            if name != "action":
                if name in properties and properties[name] != field:
                    raise ValueError("document_tool_property_conflict")
                properties[name] = field
    return {"type": "object", **schema, "properties": {"action": {"type": "string", "enum": actions}, **properties}}


def verify_proposal_source(proposal: DocumentProposal, source: ChatTurnUserSource) -> None:
    """Check quotations and explicit current-message positions without inference.

    The caller must load source from the canonical store and separately authorize
    every artifact. A grounded role remains a suggestion for later inventory and
    binding validation; this function cannot detect a model misreading a quote.
    """
    if source.parts is not None and len(source.parts) > 1:
        raise DocumentRequestError("document_reference_ambiguous")
    proposal.evidence.verify(source.user_text)
    positions = current_attachment_positions(source)
    for index, item in enumerate(proposal.inputs):
        item.evidence.verify(source.user_text, field=f"inputs.{index}.evidence")
        if item.artifact_id in source.attachment_ids:
            if item.current_message_position is None or positions.get(item.artifact_id) != item.current_message_position:
                raise DocumentRequestError("document_reference_ambiguous")
        elif item.current_message_position is not None:
            raise DocumentRequestError("document_reference_ambiguous")
