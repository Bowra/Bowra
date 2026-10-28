"""Document checkpoints cannot be mistaken for a legacy Lead restart."""

import json
from copy import deepcopy
from pathlib import Path

import pytest
from bowhead_core.clarification_resume_context import (
    DocumentResumeContext,
    LegacyResumeContext,
    UnsupportedResumeContext,
    decode_resume_context,
)
from bowhead_core.clarification_store import resume_context_is_readable, stamp_resume_context
from bowhead_core.document_chat_continuation import DocumentPlanningAttemptRef, DocumentResumeReservation, DocumentTurnOwner


def document_snapshot() -> dict:
    return json.loads((Path(__file__).resolve().parents[3] / "contracts/chat-documents/planning-resume-v3.json").read_text())


def test_exact_checkpoint_is_readable_without_granting_execution() -> None:
    snapshot = document_snapshot()
    decoded = decode_resume_context(snapshot)
    assert isinstance(decoded, DocumentResumeContext)
    assert decoded.model_dump(by_alias=True) == snapshot
    assert stamp_resume_context(snapshot) == snapshot
    assert resume_context_is_readable(snapshot)
    snapshot["continuation"]["checkpoint"]["job_id"] = "djob_mutated"
    assert decoded.continuation.checkpoint.job_id == "djob_checkpoint"


def test_resume_refs_keep_creation_identity_separate_from_current_worker() -> None:
    decoded = decode_resume_context(document_snapshot())
    reservation = DocumentResumeReservation(card_id="card_original", checkpoint=decoded.continuation.checkpoint, generation=4)
    worker = DocumentTurnOwner(
        **{key: getattr(decoded, key) for key in ("team_id", "user_id", "workspace_id", "session_id", "turn_id")},
        generation=4,
        owner_id="resumed_worker",
    )
    attempt = DocumentPlanningAttemptRef(job_id=reservation.checkpoint.job_id, attempt_no=2, worker=worker)
    assert attempt.worker.generation == reservation.generation
    assert decoded.continuation.creation_generation == 2
    with pytest.raises(ValueError):
        DocumentResumeReservation(card_id="card_original", checkpoint=reservation.checkpoint, generation=True)


@pytest.mark.parametrize("version", [None, 0, 1, 2])
def test_legacy_snapshots_preserve_fields_without_accepting_document_authority(version) -> None:
    snapshot = {"user_text": "Original request", "legacy_state": {"items": [1]}}
    if version is not None:
        snapshot["__schema_version"] = version
    decoded = decode_resume_context(snapshot)
    assert isinstance(decoded, LegacyResumeContext)
    assert decoded.snapshot == snapshot
    assert resume_context_is_readable(snapshot)
    snapshot["legacy_state"]["items"].append(2)
    assert decoded.snapshot["legacy_state"]["items"] == [1]
    snapshot["continuation"] = document_snapshot()["continuation"]
    with pytest.raises(UnsupportedResumeContext):
        decode_resume_context(snapshot)
    assert not resume_context_is_readable(snapshot)


@pytest.mark.parametrize(
    "path,value",
    [
        (("__schema_version",), 4),
        (("__schema_version",), "3"),
        (("__schema_version",), 3.0),
        (("continuation", "kind"), "ordinary_lead"),
        (("continuation", "version"), True),
        (("continuation", "creation_generation"), 1.0),
        (("continuation", "creation_owner_id"), ""),
        (("continuation", "runtime_snapshot_digest"), "missing"),
        (("continuation", "checkpoint", "attempt_no"), 3),
        (("continuation", "checkpoint", "attempt_no"), True),
        (("continuation", "checkpoint", "job_id"), "s_wrong_kind"),
        (("continuation", "checkpoint", "source_hash"), "A" * 64),
        (("continuation", "checkpoint", "checkpoint_digest"), None),
        (("workspace_id",), ""),
        (("team_id",), None),
        (("user_text",), ""),
        (("question_round",), {"contract": "effect-free-questions.v1"}),
        (("continuation", "checkpoint", "credentials"), "untrusted"),
    ],
)
def test_invalid_checkpoint_is_rejected_before_any_legacy_fallback(path, value) -> None:
    snapshot = deepcopy(document_snapshot())
    target = snapshot
    for field in path[:-1]:
        target = target[field]
    target[path[-1]] = value
    with pytest.raises(UnsupportedResumeContext, match="clarification_resume_unavailable"):
        decode_resume_context(snapshot)
    assert not resume_context_is_readable(snapshot)
