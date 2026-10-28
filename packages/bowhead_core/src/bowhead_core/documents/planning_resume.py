"""Rebase retained inventory with an exact canonical answer, without reparsing."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from bowhead_core.documents.executor import WorkerInput
from bowhead_core.documents.package_io import OfficePackageError
from bowhead_core.documents.pptx_plan import JsonEvidenceSource, PlanningContext, _facts, _utf8, planning_system_prompt


@dataclass(frozen=True)
class DocumentAnswerContext:
    instruction: str
    context: PlanningContext
    evidence: WorkerInput


def prepare_answer_context(context: PlanningContext, *, question: str, answer: str, external_input_count: int) -> DocumentAnswerContext:
    """The caller authenticates the original resolved card and retained inputs.

    The derived JSON uses the existing worker input contract. Its contents and
    hash are reconstructible from immutable card/attempt records, not a model.
    """
    if (
        not isinstance(question, str)
        or question != question.strip()
        or not 1 <= len(question) <= 200
        or not isinstance(answer, str)
        or answer != answer.strip()
        or not 1 <= len(answer) <= 256
        or type(external_input_count) is not int
        or not 0 <= external_input_count <= 6
    ):
        raise OfficePackageError("planning_input_limit")
    try:
        payload = json.loads(context.payload)
    except (ValueError, TypeError) as exc:
        raise OfficePackageError("planning_context_changed") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("instruction"), str):
        raise OfficePackageError("planning_context_changed")
    system = planning_system_prompt(payload.get("profile"))
    if hashlib.sha256(_utf8(system + "\n" + context.payload)).hexdigest() != context.digest:
        raise OfficePackageError("planning_context_changed")
    original = payload["instruction"]
    instruction = original + "\n\nClarification question: " + question + "\nUser answer: " + answer
    if len(instruction) > 8000:
        raise OfficePackageError("planning_input_limit")
    if not isinstance(payload.get("sources"), list) or not isinstance(payload.get("facts"), list):
        raise OfficePackageError("planning_context_changed")
    evidence = {}
    if external_input_count == 0:
        fallback = JsonEvidenceSource("User request", json.dumps({"request": original}, ensure_ascii=False).encode())
        expected_sources = [{"name": fallback.name, "sha256": hashlib.sha256(fallback.content).hexdigest()}]
        if payload["sources"] != expected_sources or payload["facts"] != _facts((fallback,)):
            raise OfficePackageError("planning_context_changed")
        # Preserve the original request fact; adding a real JSON input means
        # the worker will no longer synthesize its no-data fallback.
        evidence["request"] = original
        sources, facts = [], []
    else:
        if len(payload["sources"]) != external_input_count:
            raise OfficePackageError("planning_context_changed")
        sources, facts = payload["sources"], payload["facts"]
    evidence["clarification"] = {"question": question, "answer": answer}
    content = _utf8(json.dumps(evidence, ensure_ascii=False, separators=(",", ":"), allow_nan=False))
    evidence_source = JsonEvidenceSource("Document clarification.json", content)
    added = _facts((evidence_source,))
    if len(facts) + len(added) > 512:
        raise OfficePackageError("planning_input_limit")
    offset, source_index = len(facts), len(sources)
    payload["instruction"] = instruction
    payload["sources"] = [*sources, {"name": evidence_source.name, "sha256": hashlib.sha256(content).hexdigest()}]
    payload["facts"] = [*facts, *[{**fact, "id": f"f{offset + index}", "source": source_index} for index, fact in enumerate(added)]]
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    if len(_utf8(encoded)) > 64_000:
        raise OfficePackageError("planning_input_limit")
    return DocumentAnswerContext(
        instruction,
        PlanningContext(encoded, hashlib.sha256(_utf8(system + "\n" + encoded)).hexdigest()),
        WorkerInput(evidence_source.name, "data", content),
    )
