"""Compile an unreviewed, original-package PPTX candidate inside the worker.

This is a private draft operation, not semantic approval or publication. The
caller must authorize and freeze inputs before invoking it in an isolated worker.
Rendering, durable ownership/revision fencing and user confirmation remain
separate requirements. No caller-supplied review or edit scope is trusted here.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Literal

from bowhead_core.documents.package_io import OFFICE_MIMES, OfficePackageError, inspect_office_package
from bowhead_core.documents.pptx_plan import PROFILE as PLANNER_PROFILE
from bowhead_core.documents.pptx_plan import JsonEvidenceSource, prepare_planning_context, review_planning_response
from bowhead_core.documents.pptx_text import TextEditReceipt, TextEditScope, TextReplacement, apply_pptx_text_update, inventory_pptx_text

PROFILE = "ordinary-pptx-draft-v1"


@dataclass(frozen=True)
class DraftEvidence:
    """A reread source pointer or canonical user quotation, not a model label."""

    kind: Literal["data", "instruction"]
    source_name: str | None
    source_sha256: str
    pointer: str | None
    quote_start: int | None
    value: str


@dataclass(frozen=True)
class DraftChange:
    """Human-readable location, before/after and complete supporting evidence."""

    slot_id: str
    slide: int
    shape_name: str
    cell: tuple[int, int] | None
    paragraph: int
    before: str
    after: str
    evidence: tuple[DraftEvidence, ...]


@dataclass(frozen=True)
class RetainedText:
    """Keep unsupported text visible instead of implying it was updated."""

    slot_id: str
    slide: int
    shape_name: str
    text: str
    reason: str


@dataclass(frozen=True)
class DraftManifest:
    """Content identity only; a hash is never a confirmation credential."""

    context_digest: str
    proposal_digest: str
    edit_receipt: TextEditReceipt
    changes: tuple[DraftChange, ...]
    retained_slot_ids: tuple[str, ...]
    retained_unsupported_text: tuple[RetainedText, ...]
    profile: str = PROFILE
    state: str = "unrendered"
    semantic_status: str = "unapproved"

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.to_json().encode()).hexdigest()


@dataclass(frozen=True)
class CompiledDraft:
    candidate: bytes
    manifest: DraftManifest


def compile_pptx_draft(
    source: bytes,
    instruction: str,
    data: tuple[JsonEvidenceSource, ...],
    response: str,
    *,
    expected_context_digest: str,
    profile: str = PLANNER_PROFILE,
) -> CompiledDraft:
    """Recheck raw inputs/proposal and limit execution to exact text leaves.

    The current private compiler rejects complex slide objects, opaque embedded
    files and external relationships. It never drops them to make a deck pass.
    Blocked text may remain unchanged, with its limitation in the manifest.
    Even a mechanically valid draft can contain an incorrect business binding;
    the manifest exposes source pointers without claiming semantic correctness.
    """
    info = inspect_office_package(source, filename="source.pptx", declared_mime=OFFICE_MIMES[".pptx"])
    inventory = inventory_pptx_text(source)
    if info.opaque_embedding_count or info.external_relationship_count or inventory.unsupported_objects:
        raise OfficePackageError("draft_template_unsupported")
    review = review_planning_response(source, instruction, data, response, expected_context_digest=expected_context_digest, profile=profile)
    if review.action != "propose":
        raise OfficePackageError("draft_requires_proposal")
    if not review.changes:
        raise OfficePackageError("draft_has_no_changes")

    # Rebuild source evidence from the same immutable context. Model-provided
    # summaries, approval flags, addresses and computed values are never read.
    context = json.loads(prepare_planning_context(source, instruction, data, profile=profile).payload)
    facts = {fact["id"]: fact for fact in context["facts"]}
    slots = {slot.slot_id: slot for slot in inventory.slots}
    instruction_sha256 = hashlib.sha256(instruction.encode()).hexdigest()

    def evidence(ref: str) -> DraftEvidence:
        if ref.startswith("instruction:"):
            _, start, size = ref.split(":")
            return DraftEvidence("instruction", None, instruction_sha256, None, int(start), instruction[int(start) : int(start) + int(size)])
        fragment = ref.split(":") if ref.startswith("fact:") else None
        fact = facts[fragment[1] if fragment else ref]
        origin = context["sources"][fact["source"]]
        start = int(fragment[2]) if fragment else None
        value = fact["value"][start : start + int(fragment[3])] if fragment else fact["value"]
        return DraftEvidence("data", origin["name"], origin["sha256"], fact["path"], start, value)

    changes = tuple(
        DraftChange(
            change.slot_id,
            change.slide,
            slots[change.slot_id].shape_name,
            slots[change.slot_id].cell,
            slots[change.slot_id].paragraph,
            change.before,
            change.after,
            tuple(evidence(ref) for ref in change.evidence_refs),
        )
        for change in review.changes
    )
    # This scope grants only private candidate construction. It is not the
    # approval required to publish either this source or a rendered PDF.
    scope = TextEditScope(info.sha256, tuple(change.slot_id for change in changes))
    replacements = tuple(TextReplacement(change.slot_id, change.after) for change in changes)
    candidate, receipt = apply_pptx_text_update(source, scope, replacements)
    retained = tuple(
        RetainedText(slot.slot_id, slot.slide, slot.shape_name, slot.text, slot.blocked_reason)
        for slot in inventory.slots
        if slot.blocked_reason is not None
    )
    return CompiledDraft(
        candidate, DraftManifest(review.context_digest, review.proposal_digest, receipt, changes, review.retained_slot_ids, retained)
    )
