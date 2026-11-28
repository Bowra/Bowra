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


GB = 1024**3


@pytest.mark.parametrize(
    ("info", "configured", "slots", "capture_cpus", "view_cpus"),
    [
        (f"4 {14 * GB}", "", 2, 1, 2),  # 116: captures as before; previews get the two unreserved CPUs
        (f"8 {32 * GB}", "", 6, 1, 2),
        (f"16 {64 * GB}", "", 8, 1, 2),
        (f"8 {8 * GB}", "", 2, 2, 2),  # memory-bound: the spare CPUs go to captures
        (f"2 {4 * GB}", "", 2, 1, 1),  # a small host keeps the earlier two
        (f"8 {32 * GB}", "3", 3, 2, 2),
        (f"8 {32 * GB}", "0", 6, 1, 2),
        (f"8 {32 * GB}", "many", 6, 1, 2),
        (None, "", 2, 1, 1),
    ],
)
def test_document_capacity_follows_the_host(executor, monkeypatch, capsys, info, configured, slots, capture_cpus, view_cpus):
    module, *_ = executor
    original = module._docker

    def docker(*args):
        if args[0] == "info":
            if info is None:
                raise RuntimeError("document_docker_unavailable")
            return info
        return original(*args)

    monkeypatch.setattr(module, "_docker", docker)
    monkeypatch.setenv("BOWHEAD_DOCUMENT_RUN_SLOTS", configured)
    capacity = module.host_capacity()
    assert (capacity["slots"], capacity["capture_cpus"], capacity["view_cpus"]) == (slots, capture_cpus, view_cpus)
    assert module.host_capacity() is capacity  # measured once per process
    assert ("BOWHEAD_DOCUMENT_RUN_SLOTS ignored" in capsys.readouterr().err) is (configured in {"0", "many"})


def test_runs_take_a_free_slot_and_captures_get_the_spare_cpus(executor, tmp_path, monkeypatch):
    module, runs, payload, _, _ = executor
    labels = {
        "bowhead.document.profile": module.PROFILE,
        "bowhead.document.authoring": module.AUTHORING_PROFILE,
        "bowhead.document.authoring.contract": module.AUTHORING_CONTRACT,
        "bowhead.document.authoring.html-view": "html-view-v1",
        "bowhead.document.authoring.results-preview": "pptx-results-preview-v1",
    }
    active: list[str] = []
    creates: list[tuple[str, ...]] = []
    original = module._docker

    def docker(*args):
        if args[0] == "info":
            return f"8 {32 * GB}"
        if args[:2] == ("image", "inspect"):
            return json.dumps([{"Id": IMAGE_ID, "Config": {"Labels": labels}}])
        if args[0] == "ps":
            return "\n".join(active)
        if args[0] == "create":
            creates.append(args)
        return original(*args)

    monkeypatch.setattr(module, "_docker", docker)
    monkeypatch.setenv("BOWHEAD_DOCUMENT_RUN_SLOTS", "3")
    assert runs.readiness(module.AUTHORING_PROFILE)["document_slots"] == 3

    def request(run_id: str, operation: str) -> dict:
        inputs = tmp_path / ("document-" + run_id) / "input"
        inputs.mkdir(parents=True, exist_ok=True)
        (inputs.parent / "output").mkdir(exist_ok=True)
        control = json.dumps({"profile": module.AUTHORING_PROFILE, "operation": operation}).encode()
        (inputs / "control.json").write_bytes(control)
        (inputs / "input-0").write_bytes(b"source")
        return {**payload, "run_id": run_id, "control_sha256": hashlib.sha256(control).hexdigest(), "profile": module.AUTHORING_PROFILE}

    capture = request(RUN_ID, "html_view")
    active[:] = ["a", "b", "c"]
    with pytest.raises(RuntimeError, match="document_runtime_busy"):
        runs.start(capture)
    active[:] = ["a", "b"]
    assert runs.start(capture)["status"] == "running"
    assert runs.start(request("b" * 64, "execute"))["status"] == "running"
    active[:] = ["a"]
    # A Results preview runs one at a time per application, so it gets the unreserved CPUs (docx design §5.3).
    assert runs.start(request("c" * 64, "view"))["status"] == "running"
    assert [next(arg for arg in create if arg.startswith("--cpus=")) for create in creates] == ["--cpus=2.0", "--cpus=1.0", "--cpus=2.0"]


@pytest.mark.parametrize("labelled", [True, False])
def test_html_view_runs_only_on_an_image_that_declares_it(executor, tmp_path, monkeypatch, labelled):
    # Visual self-check slice B: the HTML/SVG capture is offered by the image label alone.
    module, runs, payload, calls, _ = executor
    labels = {
        "bowhead.document.profile": module.PROFILE,
        "bowhead.document.authoring": module.AUTHORING_PROFILE,
        "bowhead.document.authoring.contract": module.AUTHORING_CONTRACT,
        **({"bowhead.document.authoring.html-view": "html-view-v1"} if labelled else {}),
    }
    original = module._docker

    def docker(*args):
        if args[:2] == ("image", "inspect"):
            return json.dumps([{"Id": IMAGE_ID, "Config": {"Labels": labels}}])
        return original(*args)

    monkeypatch.setattr(module, "_docker", docker)
    readiness = runs.readiness(module.AUTHORING_PROFILE)
    assert ("html_view_contract" in readiness) is labelled
    control = json.dumps({"profile": module.AUTHORING_PROFILE, "operation": "html_view"}).encode()
    inputs = tmp_path / ("document-" + RUN_ID) / "input"
    (inputs / "control.json").write_bytes(control)
    (inputs / "input-1").write_bytes(b"transform")
    request = {**payload, "control_sha256": hashlib.sha256(control).hexdigest(), "profile": module.AUTHORING_PROFILE}
    if labelled:
        assert runs.start(request)["status"] == "running"
        create = next(args for args in calls if args and args[0] == "create")
        assert "--network=none" in create and "--read-only" in create and "--pids-limit=128" in create
    else:
        with pytest.raises(ValueError, match="document_run_control_invalid"):
            runs.start(request)


@pytest.mark.parametrize("labelled", [True, False])
def test_word_pages_render_only_on_an_image_that_declares_them(executor, tmp_path, monkeypatch, labelled):
    """docx inline edit design §5.1: the Word profile offers `view` when the image declares the preview."""
    module, runs, payload, calls, _ = executor
    labels = {
        "bowhead.document.profile": module.PROFILE,
        "bowhead.document.word-authoring": module.WORD_PROFILE,
        "bowhead.document.word-authoring.contract": module.WORD_CONTRACT,
        **({"bowhead.document.word-authoring.results-preview": "docx-results-preview-v1"} if labelled else {}),
    }
    original = module._docker

    def docker(*args):
        if args[0] == "info":
            return f"4 {14 * GB}"
        if args[:2] == ("image", "inspect"):
            return json.dumps([{"Id": IMAGE_ID, "Config": {"Labels": labels}}])
        return original(*args)

    monkeypatch.setattr(module, "_docker", docker)
    assert (runs.readiness(module.WORD_PROFILE).get("results_preview_contract") == "docx-results-preview-v1") is labelled
    control = json.dumps({"profile": module.WORD_PROFILE, "operation": "view"}).encode()
    inputs = tmp_path / ("document-" + RUN_ID) / "input"
    (inputs / "control.json").write_bytes(control)
    request = {**payload, "control_sha256": hashlib.sha256(control).hexdigest(), "profile": module.WORD_PROFILE}
    if labelled:
        assert runs.start(request)["status"] == "running"
        create = next(args for args in calls if args and args[0] == "create")
        assert "--cpus=2.0" in create and "--network=none" in create
    else:
        with pytest.raises(ValueError, match="document_run_control_invalid"):
            runs.start(request)
