"""D5 — _output_artifacts_from_result_json projects real art_ ids from runner output.

The python_sandbox tool result previously only re-exposed bare file paths; the real
artifact_ids (uploaded by the inner runner) live in result_json['outputs'][*]['artifact_id'].
Surfacing them lets the peer chart sandbox output directly instead of improvising a path
(→ invalid_artifact_id_format) and re-querying bi_query for a real id.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest
from bowhead_skill.tools import _output_artifacts_from_result_json

_RUNNER_PATH = Path(__file__).resolve().parents[1] / "catalog" / "python_sandbox" / "scripts" / "runner.py"


@pytest.fixture
def runner() -> ModuleType:
    spec = importlib.util.spec_from_file_location("_d5_data_runner", _RUNNER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_projects_artifact_ids_and_drops_idless() -> None:
    rj = {
        "outputs": [
            {"name": "out.csv", "artifact_id": "art_abc123def456", "mime": "text/csv", "size": 42},
            {"name": "user.py", "artifact_id": None},  # scratch file w/o upload id → dropped
            {"name": "noid.txt"},  # missing key → dropped
        ]
    }
    assert _output_artifacts_from_result_json(rj) == [
        {"artifact_id": "art_abc123def456", "name": "out.csv", "mime": "text/csv", "size": 42},
    ]


def test_missing_or_malformed_result_json_is_empty() -> None:
    assert _output_artifacts_from_result_json(None) == []
    assert _output_artifacts_from_result_json({}) == []
    assert _output_artifacts_from_result_json({"outputs": "notalist"}) == []
    assert _output_artifacts_from_result_json({"outputs": [{"name": "x"}]}) == []
    assert _output_artifacts_from_result_json("garbage") == []


def test_projects_data_role_for_materialized_sandbox_content() -> None:
    assert _output_artifacts_from_result_json(
        {
            "outputs": [
                {
                    "name": "sandbox-stdout.txt",
                    "artifact_id": "art_stdout123456",
                    "mime": "text/plain",
                    "size": 12,
                    "data_role": "stdout",
                },
            ],
        },
    ) == [
        {
            "artifact_id": "art_stdout123456",
            "name": "sandbox-stdout.txt",
            "mime": "text/plain",
            "size": 12,
            "data_role": "stdout",
        },
    ]


def test_output_kind_requires_explicit_delivery_intent_for_video(
    runner: ModuleType,
) -> None:
    """媒体 MIME/扩展名不能替代交付意图；显式 purpose 才能提升 MP4。"""
    assert runner._kind_for_output("analysis.mp4") is None
    assert runner._kind_for_output("final.mp4", purpose="deliverable") is None
    assert (
        runner._kind_for_output(
            "final.mp4",
            purpose="deliverable",
            deliverable_outputs=frozenset({"final.mp4"}),
        )
        == "deliverable"
    )
    # Explicit delivery also supports a user-requested CSV; automatic stdout/structured outputs do not use this function.
    assert (
        runner._kind_for_output(
            "export.csv",
            purpose="deliverable",
            deliverable_outputs=frozenset({"export.csv"}),
        )
        == "deliverable"
    )


def test_output_collection_rejects_symlink_to_host_file(
    runner: ModuleType,
    tmp_path: Path,
) -> None:
    """沙箱产物不能借共享 workdir 的符号链接让 host runner 读取宿主文件。"""
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    host_secret = tmp_path / "host-secret.txt"
    host_secret.write_text("must-not-upload", encoding="utf-8")
    (workdir / "result.txt").symlink_to(host_secret)

    assert runner._is_collectable_output(workdir / "result.txt") is False
    regular = workdir / "safe.txt"
    regular.write_text("ok", encoding="utf-8")
    assert runner._is_collectable_output(regular) is True


def test_output_kind_keeps_existing_document_and_preview_contracts(
    runner: ModuleType,
) -> None:
    assert runner._kind_for_output("report.pdf") == "deliverable"
    assert (
        runner._kind_for_output(
            "deck-slide-01.png",
            purpose="deliverable",
            deliverable_outputs=frozenset({"deck-slide-01.png"}),
        )
        == "deck_preview"
    )


def test_declared_deliverables_require_persisted_exact_outputs(
    runner: ModuleType,
) -> None:
    outputs = [
        {
            "name": "final.mp4",
            "artifact_id": "art_video123456",
            "mime": "video/mp4",
        },
        {"name": "unpersisted.csv", "artifact_id": None},
    ]

    assert runner._missing_declared_deliverables(
        outputs,
        purpose="deliverable",
        deliverable_outputs=frozenset({"final.mp4", "missing.csv", "unpersisted.csv"}),
    ) == ["missing.csv", "unpersisted.csv"]
    assert (
        runner._missing_declared_deliverables(
            outputs,
            purpose="analysis",
            deliverable_outputs=frozenset({"missing.csv"}),
        )
        == []
    )


def test_materializes_complete_stdout_not_inline_preview(
    runner: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    full_stdout = "row\n" * 3_000
    uploaded: dict[str, bytes] = {}

    def upload(path: Path, *_args: object, **_kwargs: object) -> dict[str, object]:
        uploaded[path.name] = path.read_bytes()
        return {
            "name": path.name,
            "artifact_id": "art_stdout123456",
            "mime": "text/plain",
            "size": path.stat().st_size,
        }

    monkeypatch.setattr(runner, "_upload_artifact", upload)
    result = {
        "ok": True,
        "exit_code": 0,
        "stdout": runner._trunc(full_stdout),
        "_stdout_full": full_stdout,
        "stderr": "",
    }
    outputs: list[dict] = []

    runner._materialize_data_outputs(
        workdir=tmp_path,
        result=result,
        outputs=outputs,
        gateway="http://gateway",
        session_id="s_1",
        created_by_turn="t_1",
        token="token",
    )

    assert uploaded["sandbox-stdout.txt"] == full_stdout.encode()
    assert "_stdout_full" not in result
    assert outputs[0]["artifact_id"] == "art_stdout123456"
    assert outputs[0]["data_role"] == "stdout"


def test_materializes_structured_skill_result_as_canonical_json(
    runner: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    uploaded: dict[str, bytes] = {}

    def upload(path: Path, *_args: object, **_kwargs: object) -> dict[str, object]:
        uploaded[path.name] = path.read_bytes()
        return {
            "name": path.name,
            "artifact_id": "art_result123456",
            "mime": "application/json",
            "size": path.stat().st_size,
        }

    monkeypatch.setattr(runner, "_upload_artifact", upload)
    result = {
        "ok": True,
        "exit_code": 0,
        "stdout": "",
        "_stdout_full": "",
        "stderr": "",
        "skill_result": {"value": 391, "ok": True},
    }
    outputs: list[dict] = []

    runner._materialize_data_outputs(
        workdir=tmp_path,
        result=result,
        outputs=outputs,
        gateway="http://gateway",
        session_id="s_1",
        created_by_turn="t_1",
        token=None,
    )

    assert uploaded["sandbox-result.json"] == b'{"ok":true,"value":391}\n'
    assert outputs[0]["data_role"] == "structured"


def test_required_data_upload_failure_fails_the_run(
    runner: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runner, "_upload_artifact", lambda *_args, **_kwargs: None)
    result = {
        "ok": True,
        "exit_code": 0,
        "stdout": "value=391\n",
        "_stdout_full": "value=391\n",
        "stderr": "",
    }

    runner._materialize_data_outputs(
        workdir=tmp_path,
        result=result,
        outputs=[],
        gateway="http://gateway",
        session_id="s_1",
        created_by_turn="t_1",
        token=None,
    )

    assert result["ok"] is False
    assert result["exit_code"] == 1
    assert result["error"] == "sandbox stdout Data artifact upload failed"


def test_failed_run_still_materializes_stdout_and_structured_result(
    runner: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    uploaded: list[str] = []

    def upload(path: Path, *_args: object, **_kwargs: object) -> dict[str, object]:
        uploaded.append(path.name)
        return {
            "name": path.name,
            "artifact_id": f"art_{len(uploaded):012d}",
            "mime": "text/plain",
            "size": path.stat().st_size,
        }

    monkeypatch.setattr(runner, "_upload_artifact", upload)
    result = {
        "ok": False,
        "exit_code": 1,
        "stdout": "partial answer\n",
        "_stdout_full": "partial answer\n",
        "stderr": "boom",
        "error": "exit_code=1",
        "skill_result": {"ok": False, "partial": 391},
    }
    outputs: list[dict] = []

    runner._materialize_data_outputs(
        workdir=tmp_path,
        result=result,
        outputs=outputs,
        gateway="http://gateway",
        session_id="s_1",
        created_by_turn="t_1",
        token=None,
    )

    assert uploaded == ["sandbox-stdout.txt", "sandbox-result.json"]
    assert [output["data_role"] for output in outputs] == ["stdout", "structured"]
    assert result["ok"] is False
    assert result["error"] == "exit_code=1"


def test_one_data_upload_failure_does_not_hide_other_outputs(
    runner: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def upload(path: Path, *_args: object, **_kwargs: object) -> dict[str, object] | None:
        if path.name == "sandbox-stdout.txt":
            return None
        return {
            "name": path.name,
            "artifact_id": "art_result123456",
            "mime": "application/json",
            "size": path.stat().st_size,
        }

    monkeypatch.setattr(runner, "_upload_artifact", upload)
    result = {
        "ok": True,
        "exit_code": 0,
        "stdout": "partial output\n",
        "_stdout_full": "partial output\n",
        "stderr": "",
        "skill_result": {"value": 391},
    }
    outputs: list[dict] = []

    runner._materialize_data_outputs(
        workdir=tmp_path,
        result=result,
        outputs=outputs,
        gateway="http://gateway",
        session_id="s_1",
        created_by_turn="t_1",
        token=None,
    )

    assert result["ok"] is False
    assert result["error"] == "sandbox stdout Data artifact upload failed"
    assert [output["data_role"] for output in outputs] == ["structured"]
