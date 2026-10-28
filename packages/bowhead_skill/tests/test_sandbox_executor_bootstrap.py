"""Verify executor ownership of the shared writable workdir root.

The application cannot repair a root-owned bind mount. The executor creates
the shared root and sets sticky world-writable permissions before accepting
execution requests. These tests load its standalone standard-library script."""

from __future__ import annotations

import importlib.util
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

_APP_PY = Path(__file__).resolve().parents[3] / "deploy" / "sandbox-executor" / "app.py"


def _load_executor() -> Any:
    spec = importlib.util.spec_from_file_location("_bowhead_sandbox_executor_under_test", _APP_PY)
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_executor_source_is_where_the_contract_says() -> None:
    """路径写死在测试里，文件搬家时这条先红，而不是让加载静默失败。"""
    assert _APP_PY.is_file(), f"sandbox-executor app.py not found at {_APP_PY}"


def test_ensure_workdir_root_creates_missing_dir_with_1777(tmp_path: Path) -> None:
    """目录不存在 → 建出来并置 1777（= /tmp 语义：三方 uid 都要写，无公共 group）。"""
    mod = _load_executor()
    root = tmp_path / "sandbox"
    assert not root.exists()

    mod._ensure_workdir_root(str(root))

    assert root.is_dir()
    assert stat.S_IMODE(root.stat().st_mode) == 0o1777


@pytest.mark.skipif(os.geteuid() == 0, reason="root 下 chmod 位语义不同，此断言无意义")
def test_ensure_workdir_root_repairs_unwritable_directory(tmp_path: Path) -> None:
    """Repair an existing 0755 root to 1777 so other application users can create workdirs."""
    mod = _load_executor()
    root = tmp_path / "sandbox"
    root.mkdir(mode=0o755)
    assert stat.S_IMODE(root.stat().st_mode) == 0o755

    mod._ensure_workdir_root(str(root))

    assert stat.S_IMODE(root.stat().st_mode) == 0o1777


def test_ensure_workdir_root_is_idempotent_and_silent_when_already_correct(tmp_path: Path, capsys) -> None:
    """已经是 1777 → 不再 chmod、不写审计行（重启安全，且不污染日志）。"""
    mod = _load_executor()
    root = tmp_path / "sandbox"
    root.mkdir()
    # S103: 1777 is the tested contract itself (/tmp semantics; see the module docstring), not an oversight.
    root.chmod(0o1777)  # noqa: S103
    capsys.readouterr()

    mod._ensure_workdir_root(str(root))

    assert stat.S_IMODE(root.stat().st_mode) == 0o1777
    assert capsys.readouterr().err == ""


def test_ensure_workdir_root_never_raises_when_chmod_denied(tmp_path: Path, monkeypatch, capsys) -> None:
    """chmod 被宿主拒绝（如 userns-remap → EPERM）时只告警，**绝不**抛出。

    executor 拒绝启动会因 compose 的 `depends_on: service_healthy` 连带拖垮 BI/chat ——
    沙箱只是众多能力之一。失败时由 app 侧的可用性闸把它变成用户可读的「沙箱未就绪」。
    """
    mod = _load_executor()
    root = tmp_path / "sandbox"
    root.mkdir(mode=0o755)

    def _denied(*_a: Any, **_kw: Any) -> None:
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(mod.os, "chmod", _denied)
    capsys.readouterr()

    mod._ensure_workdir_root(str(root))  # No exception = pass.

    err = capsys.readouterr().err
    assert "WARN" in err
    assert "fail closed" in err


def test_executor_create_timeout_removes_the_named_container(tmp_path: Path, monkeypatch) -> None:
    """A timed-out create cannot leave an untracked container for later start."""
    mod = _load_executor()
    root = tmp_path / "sandbox"
    workdir = root / "run-1"
    workdir.mkdir(parents=True)
    monkeypatch.setenv("BOWHEAD_SKILL_WORKDIR_ROOT", str(root))
    monkeypatch.setenv("BOWHEAD_SANDBOX_IMAGE", "example/sandbox:1")
    calls: list[list[str]] = []

    def _run(argv: list[str], **_kwargs: Any) -> Any:
        calls.append(argv)
        if argv[:2] == ["docker", "create"]:
            raise subprocess.TimeoutExpired(argv, 5)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(mod.subprocess, "run", _run)

    result = mod._run({"workdir": str(workdir), "timeout": 5})

    assert result["error"] == "timeout after 5s"
    assert result["cause"] == "execution_timeout"
    assert result["timeout_sec"] == 5
    run_name = calls[0][calls[0].index("--name") + 1]
    assert calls[1] == ["docker", "rm", "-f", run_name]


@pytest.mark.parametrize("configured,requested,expected", [("600", None, 120), ("600", 9000, 600), ("60", 9000, 60), ("0", 9000, 600)])
def test_executor_owns_the_execution_deadline(tmp_path, monkeypatch, configured, requested, expected):
    mod = _load_executor()
    workdir = tmp_path / "run-1"
    workdir.mkdir()
    monkeypatch.setenv("BOWHEAD_SKILL_WORKDIR_ROOT", str(tmp_path))
    monkeypatch.setenv("BOWHEAD_SANDBOX_IMAGE", "sandbox:test")
    monkeypatch.setenv("BOWHEAD_SANDBOX_MAX_TIMEOUT", configured)
    monkeypatch.setattr(
        mod.subprocess, "run", lambda argv, **kwargs: subprocess.CompletedProcess(argv, 1 if argv[1] == "create" else 0, "", "create refused")
    )
    result = mod._run({"workdir": str(workdir), "timeout": requested})
    assert result["timeout_sec"] == expected
    assert result["execution_state"] == "not_started"
    assert result["cause"] == "container_create_failed"


@pytest.mark.parametrize("cancel_at", ["before_create", "during_create"])
def test_executor_cancellation_fences_late_start(tmp_path, monkeypatch, cancel_at):
    mod = _load_executor()
    workdir = tmp_path / "run-1"
    workdir.mkdir()
    monkeypatch.setenv("BOWHEAD_SKILL_WORKDIR_ROOT", str(tmp_path))
    monkeypatch.setenv("BOWHEAD_SANDBOX_IMAGE", "example/sandbox:1")
    marker = workdir.with_name(f".{workdir.name}.cancelled")
    if cancel_at == "before_create":
        marker.touch()
    calls = []

    def execute(argv, **kwargs):
        calls.append(argv)
        if argv[1] == "create":
            marker.touch()
        return subprocess.CompletedProcess(argv, 0, "", "")

    def forbidden_start(*args, **kwargs):
        raise AssertionError("Cancelled containers must not start")

    monkeypatch.setattr(mod.subprocess, "run", execute)
    monkeypatch.setattr(mod.subprocess, "Popen", forbidden_start)
    assert mod._run({"workdir": str(workdir)})["error"] == "cancelled"
    assert marker.exists()
    assert [argv[1] for argv in calls] == ([] if cancel_at == "before_create" else ["create", "rm"])
    # Replaying the same request cannot erase the cancellation tombstone.
    calls.clear()
    assert mod._run({"workdir": str(workdir)})["error"] == "cancelled"
    assert calls == []


def test_executor_does_not_claim_cleanup_when_docker_remove_fails(tmp_path, monkeypatch):
    mod = _load_executor()
    workdir = tmp_path / "run-1"
    workdir.mkdir()
    monkeypatch.setenv("BOWHEAD_SKILL_WORKDIR_ROOT", str(tmp_path))
    monkeypatch.setenv("BOWHEAD_SANDBOX_IMAGE", "sandbox:test")

    def run(argv, **kwargs):
        if argv[:2] == ["docker", "create"]:
            workdir.with_name(f".{workdir.name}.cancelled").touch()
            return subprocess.CompletedProcess(argv, 0, "created", "")
        return subprocess.CompletedProcess(argv, 1, "", "daemon unavailable")

    monkeypatch.setattr(mod.subprocess, "run", run)
    result = mod._run({"workdir": str(workdir)})
    assert result["ok"] is False
    assert result["error"] == "container_cleanup_unconfirmed"
    assert result["cause"] == "execution_state_unknown"
    assert result["execution_state"] == "unknown"


@pytest.mark.parametrize("boundary", ["settled", "cleanup_failed", "identity_missing", "cancel_during_inspect"])
def test_executor_attests_actual_container_only_after_confirmed_cleanup(tmp_path, monkeypatch, boundary):
    import json
    from types import SimpleNamespace

    mod = _load_executor()
    workdir = tmp_path / "run-original"
    workdir.mkdir()
    monkeypatch.setenv("BOWHEAD_SKILL_WORKDIR_ROOT", str(tmp_path))
    monkeypatch.setenv("BOWHEAD_SANDBOX_IMAGE", "sandbox:mutable-tag")
    monkeypatch.setenv("BOWHEAD_SANDBOX_RUNTIME", "runsc")
    commands = []

    def run(argv, **kwargs):
        commands.append(argv)
        if argv[1] == "inspect":
            if boundary == "cancel_during_inspect":
                workdir.with_name(f".{workdir.name}.cancelled").touch()
            metadata = [{"Image": "sha256:" + "a" * 64, "HostConfig": {"Runtime": "runsc", "NetworkMode": "none"}}]
            return subprocess.CompletedProcess(argv, 0, "[]" if boundary == "identity_missing" else json.dumps(metadata), "")
        failed = boundary == "cleanup_failed" and argv[1] == "rm"
        return subprocess.CompletedProcess(argv, 1 if failed else 0, "", "daemon unavailable" if failed else "")

    def start(*args, **kwargs):
        assert boundary != "cancel_during_inspect"
        return SimpleNamespace(returncode=0, communicate=lambda **kwargs: ("42", ""))

    monkeypatch.setattr(mod.subprocess, "run", run)
    monkeypatch.setattr(mod.subprocess, "Popen", start)
    result = mod._run({"workdir": str(workdir)})
    assert [command[1] for command in commands] == ["create", "inspect", "rm"]
    if boundary == "settled":
        assert result["execution"]["image_id"] == "sha256:" + "a" * 64
        assert result["execution"]["runtime"] == "runsc"
        assert result["execution"]["settled"] is True
        assert result["execution"]["id"] == commands[0][commands[0].index("--name") + 1]
    else:
        assert "execution" not in result
        assert result["ok"] is (boundary == "identity_missing")
