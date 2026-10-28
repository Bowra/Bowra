"""Evidence boundaries shared by independently callable authoring validators."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal


def candidate_digest(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def authoring_validation_report(
    *,
    target: dict | None,
    candidate_hash: str,
    structure: Literal["passed", "failed", "warning"],
    dependencies: Literal["passed", "blocked", "not_run"],
    dependency_fingerprint: str | None = None,
    supports_candidate_evaluation: bool = False,
) -> dict:
    """Describe actual checks without upgrading them to behavioral evidence."""
    return {
        "kind": "agent_authoring_validation",
        "schema_version": 1,
        "original_target": target,
        "candidate_hash": candidate_hash,
        "checks": {
            "structure": {"status": structure},
            "dependencies": {"status": dependencies, "fingerprint": dependency_fingerprint},
            "safety_check": {"status": "not_run"},
            "import_inspection": {"status": "not_run"},
            "business_trial": {"status": "not_run"},
        },
        "executed": False,
        "execution_id": None,
        "release_authorized": False,
        "trial_support": {
            "status": "requires_accepted_candidate_and_owner_consent" if supports_candidate_evaluation else "no_standalone_executor",
            "subject_kind": "immutable_agent_candidate" if supports_candidate_evaluation else None,
            "entry_tool": "agent_ci_request" if supports_candidate_evaluation else None,
            "owner_case_effect_policy": {"mode": "no_effects"} if supports_candidate_evaluation else None,
            "owner_case_fixture_ref": {} if supports_candidate_evaluation else None,
            "source_validation_is_execution_evidence": False,
        },
    }
