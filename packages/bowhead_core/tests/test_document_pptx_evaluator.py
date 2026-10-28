"""Offline scoring cannot become the draft compiler's semantic authority."""

from __future__ import annotations

import importlib
import json
from dataclasses import asdict
from pathlib import Path

import pytest
from test_document_pptx_plan import case as case

pytestmark = pytest.mark.integration


@pytest.fixture
def evaluation(case, tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[3] / "scripts"))
    evaluator = importlib.import_module("evaluate_pptx_planning")
    (tmp_path / "source.pptx").write_bytes(case[0])
    fixture = {
        "id": "draft-case",
        "instruction": case[1],
        "data": [{"name": item.name, "value": json.loads(item.content)} for item in case[2]],
        "expected": {"action": "propose", "changes": {"Quarter one": "Quarter two", "42": "53", "17": "21"}},
    }
    # The evaluator freezes JSON using its own canonical input encoding.
    data = tuple(evaluator.JsonEvidenceSource(item["name"], json.dumps(item["value"], ensure_ascii=False).encode()) for item in fixture["data"])
    context = evaluator.prepare_planning_context(case[0], case[1], data)
    (tmp_path / "case.json").write_text(json.dumps(fixture))
    (tmp_path / "context.json").write_text(json.dumps(asdict(context)))
    (tmp_path / "response.json").write_text(
        json.dumps({"content": json.dumps(case[4]), "finish_reason": "stop", "configuration_unchanged": True, "raw_usage": {}})
    )
    return evaluator, tmp_path


def test_scored_candidate_keeps_unreviewed_manifest(evaluation):
    evaluator, directory = evaluation
    result = evaluator.score(directory)
    manifest = json.loads((directory / "draft.json").read_text())
    assert result["passed"] and result["candidate_created"]
    assert manifest["state"] == "unrendered" and manifest["semantic_status"] == "unapproved"
    assert json.loads(json.dumps(result["edit_receipt"])) == manifest["edit_receipt"]


def test_wrong_offline_answer_does_not_block_or_approve_private_compilation(evaluation):
    evaluator, directory = evaluation
    case = json.loads((directory / "case.json").read_text())
    case["expected"]["changes"]["42"] = "999"
    (directory / "case.json").write_text(json.dumps(case))
    result = evaluator.score(directory)
    assert not result["passed"] and result["candidate_created"]
    assert result["error"] == "semantic_values_or_scope_mismatch"
    manifest = json.loads((directory / "draft.json").read_text())
    assert manifest["semantic_status"] == "unapproved"
    assert next(change["after"] for change in manifest["changes"] if change["before"] == "42") == "53"


def test_repeated_scoring_cannot_overwrite_candidate_evidence(evaluation):
    evaluator, directory = evaluation
    evaluator.score(directory)
    original = (directory / "candidate.pptx").read_bytes()
    with pytest.raises(FileExistsError):
        evaluator.score(directory)
    assert (directory / "candidate.pptx").read_bytes() == original
