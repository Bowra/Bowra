"""Recover interrupted CI steps using durable operation identities, not latest state."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location("agent_ci_client", ROOT / "scripts/agent-ci/run.py")
assert SPEC is not None and SPEC.loader is not None
CLIENT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CLIENT)


class Server:
    def __init__(self, checkpoint):
        self.checkpoint = checkpoint
        self.prepared = False
        self.compared = False
        self.lost_prepare_response = True
        self.mutations = []
        self.readiness = "ready"
        self.released = False
        self.proven = True
        self.gate_reads = 0
        self.failure_summary = {
            "required_failed": 2,
            "introduced": 1,
            "also_failing_on_live": 0,
            "not_run_on_live": 1,
            "owner_url": "https://bowhead.example.com/agents/manage/?agent_id=osa_1&section=release",
        }

    def call(self, path, *, query=None, body=None, key=None):
        if body is not None:
            durable = json.loads(self.checkpoint.read_text())
            assert key in durable["keys"].values()
            self.mutations.append(path)
            if path.endswith("/version-proposals"):
                assert durable["proposal_baseline"]["working_copy_id"] == "awc_1"
                return {"proposal_id": "acp_1"}
            if path.endswith("/prepare"):
                assert durable["prepare"] == body
                self.prepared = True
                if self.lost_prepare_response:
                    self.lost_prepare_response = False
                    raise CLIENT.Outcome("runtime_failure_resume_checkpoint", 5)
                return {}
            assert path.endswith("/comparisons")
            assert durable["compare"] == body
            self.compared = True
            return {}
        if path.endswith("/version-context"):
            return {
                "automation_protocol_version": 2,
                "principal_id": "oak_1",
                "working_copy": {"working_copy_id": "awc_1", "working_copy_row_version": 2, "base_release_id": "arl_1"},
                "preparation_blockers": [],
                "evaluation_context": {"workspace_id": "ws_1", "context_hash": "sha256:context", "policy": {"revision_id": None}},
            }
        if path.endswith("/comparison-operation"):
            assert query["operation_key_hash"] == CLIENT.operation_hash("oak_1", json.loads(self.checkpoint.read_text())["keys"]["compare"])
            return {
                "execution": {"id": "aex_1", "plan_id": "aep_1", "status": "completed"} if self.compared else None,
                "client_intent_verified": self.proven,
                "repository_context_verified": self.proven,
            }
        if path.endswith("/gate"):
            self.gate_reads += 1
            return {"failure_summary": self.failure_summary}
        assert path.endswith("/automation-state")
        return {
            "proposal": {"status": "accepted", "accepted_working_copy_id": "awc_1", "accepted_working_copy_row_version": 2},
            "preparation": {"candidate_id": "arc_1"} if self.prepared else None,
            "candidate": {
                "candidate_id": "arc_1",
                "candidate_row_version": 1,
                "candidate_hash": "sha256:candidate",
                "plan_id": "aep_1",
                "plan_hash": "sha256:plan",
                "control_row_version": 1,
                "state": "activated" if self.released else "prepared",
                "release_id": "arl_exact" if self.released else None,
                "gate_applicable": True,
                "readiness": self.readiness,
                "snapshot_execution_id": "aex_1" if self.compared else None,
            }
            if self.prepared
            else None,
        }


def arguments():
    return SimpleNamespace(
        agent_id="osa_1", grant_id="avag_1", repository="acme/agent", ref="refs/heads/main", commit_sha="a" * 40, timeout=1, wait_release=False
    )


@pytest.mark.parametrize(("readiness", "status"), [("ready", 0), ("ready_with_risks", 3), ("blocked", 4), ("unknown", 4)])
def test_resume_after_committed_prepare_and_lost_response_preserves_exact_operation(tmp_path, readiness, status):
    path = tmp_path / "checkpoint.json"
    payload = {
        "operations": [{"op": "replace", "path": "/listing/description", "value": "Private example content"}],
        "atomic_groups": [{"id": "listing", "operation_indexes": [0], "depends_on": []}],
    }
    identity = {"payload_hash": CLIENT.digest(payload)}
    checkpoint = CLIENT.Checkpoint(path, identity)
    server = Server(path)
    server.readiness = readiness
    with pytest.raises(CLIENT.Outcome) as lost:
        CLIENT.run(arguments(), server, checkpoint, payload)
    assert lost.value.status == 5
    resumed = CLIENT.Checkpoint(path, identity)
    with pytest.raises(CLIENT.Outcome) as finished:
        CLIENT.run(arguments(), server, resumed, payload)
    assert finished.value.status == status
    assert sum(path.endswith("/prepare") for path in server.mutations) == 1
    assert sum(path.endswith("/comparisons") for path in server.mutations) == 1
    assert "Private example content" not in path.read_text()
    assert path.stat().st_mode & 0o077 == 0
    server.released = True
    with pytest.raises(CLIENT.Outcome) as released:
        CLIENT.run(arguments(), server, CLIENT.Checkpoint(path, identity), payload)
    assert released.value.name == "released"
    assert json.loads(path.read_text())["release_id"] == "arl_exact"
    with pytest.raises(CLIENT.Outcome) as conflict:
        CLIENT.Checkpoint(path, {"payload_hash": "changed"})
    assert conflict.value.name == "checkpoint_intent_conflict"


def test_legacy_execution_cannot_be_reported_as_verified_success(tmp_path):
    path = tmp_path / "checkpoint.json"
    payload = {
        "operations": [{"op": "replace", "path": "/listing/description", "value": "Reviewed"}],
        "atomic_groups": [{"id": "listing", "operation_indexes": [0], "depends_on": []}],
    }
    server = Server(path)
    server.proven = False
    server.lost_prepare_response = False
    with pytest.raises(CLIENT.Outcome) as outcome:
        CLIENT.run(arguments(), server, CLIENT.Checkpoint(path, {"payload_hash": CLIENT.digest(payload)}), payload)
    assert outcome.value.name == "legacy_execution_requires_review"
    assert outcome.value.status == 4


@pytest.mark.parametrize("owner_url", ["https://bowhead.example.com/agents/manage/?agent_id=osa_1&section=release", None])
def test_blocked_gate_reports_failure_summary_and_owner_link(tmp_path, owner_url):
    path = tmp_path / "checkpoint.json"
    payload = {
        "operations": [{"op": "replace", "path": "/listing/description", "value": "Reviewed"}],
        "atomic_groups": [{"id": "listing", "operation_indexes": [0], "depends_on": []}],
    }
    server = Server(path)
    server.lost_prepare_response = False
    server.readiness = "blocked"
    server.failure_summary = {**server.failure_summary, "owner_url": owner_url}
    with pytest.raises(CLIENT.Outcome) as outcome:
        CLIENT.run(arguments(), server, CLIENT.Checkpoint(path, {"payload_hash": CLIENT.digest(payload)}), payload)
    assert outcome.value.name == "evaluation_blocked_or_unknown"
    assert outcome.value.details == {"failure_summary": server.failure_summary}
    assert outcome.value.message == (
        "Required cases failing: 2 (introduced by this candidate: 1, also failing on Live: 0, not run on Live: 1). "
        f"Review the cases at {owner_url or 'the Agent osa_1 Release tab'}."
    )


def test_ready_gate_does_not_read_failure_summary(tmp_path):
    path = tmp_path / "checkpoint.json"
    payload = {
        "operations": [{"op": "replace", "path": "/listing/description", "value": "Reviewed"}],
        "atomic_groups": [{"id": "listing", "operation_indexes": [0], "depends_on": []}],
    }
    server = Server(path)
    server.lost_prepare_response = False
    with pytest.raises(CLIENT.Outcome) as outcome:
        CLIENT.run(arguments(), server, CLIENT.Checkpoint(path, {"payload_hash": CLIENT.digest(payload)}), payload)
    assert outcome.value.name == "ready_awaiting_human_release"
    assert server.gate_reads == 0


def test_blocked_gate_names_unmet_points_by_id_only():
    summary = {
        "required_failed": 1,
        "introduced": 1,
        "also_failing_on_live": 0,
        "not_run_on_live": 0,
        "unmet_points": [{"case_key": "quality.1", "point_id": "p_3f2a"}, {"case_key": "quality.2", "point_id": "p_77aa"}],
        "owner_url": None,
    }
    assert CLIENT.failure_summary_line(summary, "osa_1") == (
        "Required cases failing: 1 (introduced by this candidate: 1, also failing on Live: 0, not run on Live: 0). "
        "Unmet points: quality.1/p_3f2a, quality.2/p_77aa. Review the cases at the Agent osa_1 Release tab."
    )
