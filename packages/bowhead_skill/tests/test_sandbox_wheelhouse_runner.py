"""python_sandbox runner 的离线依赖与 executor 委派测试。"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from types import ModuleType

_RUNNER_PATH = Path(__file__).resolve().parents[1] / "catalog" / "python_sandbox" / "scripts" / "runner.py"
_LOCK_BODY = "prophet==1.1.5 \\\n+    --hash=sha256:0000000000000000000000000000000000000000000000000000\n"


def _load_runner() -> ModuleType:
    spec = importlib.util.spec_from_file_location("_wheelhouse_runner", _RUNNER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def runner() -> ModuleType:
    return _load_runner()


def _make_wheelhouse(tmp_path: Path) -> Path:
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    (wheelhouse / "requirements.lock").write_text(_LOCK_BODY, encoding="utf-8")
    return wheelhouse


def test_validate_requirements_against_wheelhouse_lock(runner: ModuleType, tmp_path: Path) -> None:
    lockset = runner._wheelhouse_lockset(str(_make_wheelhouse(tmp_path)))
    valid, errors = runner._validate_sandbox_requirements(["prophet", "evil; rm -rf /"], lockset)

    assert valid == ["prophet"]
    assert len(errors) == 1


def test_requirements_without_wheelhouse_fail_closed(runner: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BOWHEAD_SANDBOX_WHEELHOUSE", raising=False)
    payload = {"code": "print('x')", "_sandbox_requirements": ["prophet"]}
    input_path = tmp_path / "input.json"
    input_path.write_text(json.dumps(payload), encoding="utf-8")

    with monkeypatch.context() as context:
        context.setattr(sys, "argv", ["runner.py", "--input", str(input_path), "--workdir", str(tmp_path)])
        assert runner.main() == 0

    result = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))
    assert result["ok"] is False
    assert "no wheelhouse available" in result["error"]


def test_runner_delegates_validated_requirements_to_executor(runner: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    wheelhouse = _make_wheelhouse(tmp_path)
    monkeypatch.setenv("BOWHEAD_SANDBOX_WHEELHOUSE", str(wheelhouse))
    captured: dict[str, object] = {}

    def _executor(url: str, token: str, **kwargs: object) -> dict:
        captured.update(url=url, token=token, **kwargs)
        return {"ok": True, "exit_code": 0, "stdout": "", "stderr": "", "duration_ms": 1}

    monkeypatch.setattr(runner, "_run_via_executor", _executor)
    payload = {
        "code": "import prophet",
        "_sandbox_requirements": ["prophet"],
        "_sandbox_executor_url": "http://executor:8091",
        "_sandbox_executor_token": "test-token",
    }
    input_path = tmp_path / "input.json"
    input_path.write_text(json.dumps(payload), encoding="utf-8")

    with monkeypatch.context() as context:
        context.setattr(sys, "argv", ["runner.py", "--input", str(input_path), "--workdir", str(tmp_path)])
        assert runner.main() == 0

    assert captured["url"] == "http://executor:8091"
    assert captured["token"] == "test-token"
    assert captured["requirements"] == ["prophet"]


def test_runner_without_executor_fails_closed(runner: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {"code": "print('x')"}
    input_path = tmp_path / "input.json"
    input_path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.delenv("BOWHEAD_SANDBOX_EXECUTOR_URL", raising=False)
    monkeypatch.delenv("BOWHEAD_SANDBOX_EXECUTOR_TOKEN", raising=False)

    with monkeypatch.context() as context:
        context.setattr(sys, "argv", ["runner.py", "--input", str(input_path), "--workdir", str(tmp_path)])
        assert runner.main() == 0

    result = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))
    assert result["cause"] == "executor_not_configured"
