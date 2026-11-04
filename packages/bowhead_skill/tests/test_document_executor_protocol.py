"""Durable fixed-worker dispatch with an external Docker boundary."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit
RUN_ID = "a" * 64
IMAGE_ID = "sha256:" + "b" * 64


@pytest.fixture
def executor(tmp_path, monkeypatch):
    path = Path(__file__).resolve().parents[3] / "deploy/sandbox-executor/document_runs.py"
    spec = importlib.util.spec_from_file_location("document_runs_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for key, value in {"IMAGE": "test/worker:1", "MEMORY_MB": "2048", "TMPFS_MB": "256", "PIDS": "128", "MAX_TIMEOUT": "120"}.items():
        monkeypatch.setenv("BOWHEAD_SANDBOX_" + key, value)
    inputs = tmp_path / ("document-" + RUN_ID) / "input"
    inputs.mkdir(parents=True)
    (inputs.parent / "output").mkdir()
    (inputs / "control.json").write_bytes(b"{}")
    (inputs / "input-0").write_bytes(b"source")
    payload = {"run_id": RUN_ID, "control_sha256": hashlib.sha256(b"{}").hexdigest(), "expected_image_id": IMAGE_ID}
    calls = []
    state = {"Status": "running", "ExitCode": 0}

    def docker(*args):
        calls.append(args)
        if args[:2] == ("image", "inspect"):
            return json.dumps([{"Id": IMAGE_ID, "Config": {"Labels": {"bowhead.document.profile": module.PROFILE}}}])
        if args[0] == "inspect":
            return json.dumps(
                [
                    {
                        "Image": IMAGE_ID,
                        "Config": {
                            "Labels": {
                                "bowhead.document.run": RUN_ID,
                                "bowhead.document.control": payload["control_sha256"],
                            }
                        },
                        "State": state,
                    }
                ]
            )
        return ""

    monkeypatch.setattr(module, "_docker", docker)
    return module, module.DocumentRuns(tmp_path), payload, calls, state


def test_start_uses_fixed_isolation_and_duplicate_replays_after_restart(executor, tmp_path):
    module, runs, payload, calls, _ = executor
    first = runs.start(payload)
    assert first["status"] == "running"
    create = next(args for args in calls if args[0] == "create")
    assert all(
        arg in create for arg in ("--network=none", "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges", "1000:1000", IMAGE_ID)
    )
    assert create[create.index("--control-sha256") + 1] == payload["control_sha256"]
    assert module.DocumentRuns(tmp_path).start(payload) == first
    assert sum(args[0] == "create" for args in calls) == 1


@pytest.mark.parametrize(
    "label,expected",
    [
        (None, ["pptx-grounded-plan-v4"]),
        ("pptx-grounded-plan-v4,pptx-grounded-plan-v5", ["pptx-grounded-plan-v4", "pptx-grounded-plan-v5"]),
        ("", None),
        ("pptx-grounded-plan-v99", None),
        ("pptx-grounded-plan-v5,pptx-grounded-plan-v5", None),
    ],
)
def test_readiness_advertises_only_reviewed_image_planners(executor, monkeypatch, label, expected):
    module, runs, _, _, _ = executor
    original = module._docker

    def inspect(*args):
        raw = original(*args)
        if args[:2] == ("image", "inspect") and label is not None:
            parsed = json.loads(raw)
            parsed[0]["Config"]["Labels"]["bowhead.document.planners"] = label
            return json.dumps(parsed)
        return raw

    monkeypatch.setattr(module, "_docker", inspect)
    if expected is None:
        with pytest.raises(RuntimeError, match="document_worker_profile_unavailable"):
            runs.readiness()
    else:
        assert runs.readiness()["planner_profiles"] == expected


def test_unknown_start_never_creates_another_container(executor, monkeypatch):
    module, runs, payload, calls, _ = executor
    original = module._docker

    def fail_start(*args):
        if args[0] == "start":
            raise RuntimeError("lost reply")
        return original(*args)

    monkeypatch.setattr(module, "_docker", fail_start)
    assert runs.start(payload)["status"] == "unknown"
    assert runs.start(payload)["status"] == "unknown"
    assert sum(args[0] == "create" for args in calls) == 1
    assert runs.get(RUN_ID)["status"] == "running"


@pytest.mark.parametrize("key,value", [("image", "other"), ("command", "sh"), ("memory", 4096), ("workdir", "/tmp")])
def test_caller_cannot_override_fixed_worker(executor, key, value):
    _, runs, payload, calls, _ = executor
    with pytest.raises(ValueError, match="document_run_control_invalid"):
        runs.start({**payload, key: value})
    assert calls == []


@pytest.mark.parametrize("key,value", [("MEMORY_MB", "512"), ("TMPFS_MB", "64"), ("PIDS", "50"), ("MAX_TIMEOUT", "30")])
def test_admin_resource_caps_are_not_silently_bypassed(executor, monkeypatch, key, value):
    _, runs, payload, calls, _ = executor
    monkeypatch.setenv("BOWHEAD_SANDBOX_" + key, value)
    with pytest.raises(RuntimeError, match="document_runtime_resources_insufficient"):
        runs.start(payload)
    assert calls == []


def test_completion_remains_recoverable_after_container_removal(executor, tmp_path):
    module, runs, payload, calls, state = executor
    runs.start(payload)
    state["Status"] = "exited"
    result = runs.get(RUN_ID)
    assert result["status"] == "completed"
    assert ("rm", result["container"]) in calls
    assert module.DocumentRuns(tmp_path).get(RUN_ID) == result


def test_cancel_never_removes_a_container_with_different_identity(executor, monkeypatch):
    module, runs, payload, calls, _ = executor
    runs.start(payload)
    original = module._docker

    def different_container(*args):
        if args[0] == "inspect":
            return json.dumps([{"Image": IMAGE_ID, "Config": {"Labels": {}}, "State": {"Status": "running"}}])
        return original(*args)

    monkeypatch.setattr(module, "_docker", different_container)
    result = runs.cancel(RUN_ID)
    assert result["status"] == "cancelled" and result["removal_pending"] is True
    assert not any(args[0] == "rm" for args in calls)


def test_symlink_input_rejects_dispatch(executor, tmp_path):
    _, runs, payload, calls, _ = executor
    path = tmp_path / ("document-" + RUN_ID) / "input" / "input-0"
    path.unlink()
    path.symlink_to(tmp_path / ("document-" + RUN_ID) / "input" / "control.json")
    with pytest.raises(ValueError, match="document_inputs_invalid"):
        runs.start(payload)
    assert not any(args[0] == "create" for args in calls)


def test_terminal_scratch_expires_but_dispatch_identity_survives(executor, tmp_path, monkeypatch):
    module, runs, payload, calls, state = executor
    first = runs.start(payload)
    state["Status"] = "exited"
    result = runs.get(RUN_ID)
    directory = tmp_path / ("document-" + RUN_ID)
    runs.sweep()
    assert directory.is_dir()
    monkeypatch.setattr(module.time, "time", lambda: first["created_at"] + 86401)
    runs.sweep()
    runs.sweep()
    assert not directory.exists()
    assert module.DocumentRuns(tmp_path).start(payload) == result
    assert sum(args[0] == "create" for args in calls) == 1


def test_terminal_removal_recovers_after_lost_response(executor, monkeypatch):
    module, runs, payload, _, state = executor
    runs.start(payload)
    state["Status"] = "exited"
    original = module._docker

    def lost_remove(*args):
        if args[0] == "rm":
            raise RuntimeError("Lost removal response")
        return original(*args)

    monkeypatch.setattr(module, "_docker", lost_remove)
    assert runs.get(RUN_ID)["removal_pending"] is True
    monkeypatch.setattr(module, "_docker", original)
    assert not runs.get(RUN_ID).get("removal_pending")
