"""A call's undeclared work files: the result says first that the user does not receive them.

an observed execution: a requested CSV was saved by a call without ``purpose``, so it became a work file, and
the model and the answer called it delivered (docs/sprints/chat-dogfood-findings-2026-11-23/spike.md §1).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
from bowhead_skill.tools import _undeclared_work_file_note
from test_sandbox_workdir_contract import _FakeResult, _FixedRuntime, _flat_tool, _mk_skill

from bowhead_skill import SkillCatalog

_RUNNER_PATH = Path(__file__).resolve().parents[1] / "catalog" / "python_sandbox" / "scripts" / "runner.py"


@pytest.fixture
def runner() -> ModuleType:
    spec = importlib.util.spec_from_file_location("_undeclared_work_runner", _RUNNER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(runner: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: str, **controls: Any) -> dict:
    def upload(path, _gateway, *_args, **_kwargs):  # type: ignore[no-untyped-def]
        return {"name": Path(path).name, "artifact_id": f"art_{Path(path).stem}", "mime": "text/plain", "size": 1}

    monkeypatch.setattr(runner, "_upload_artifact", upload)
    workdir = tmp_path / "wd"
    (tmp_path / "in.json").write_text(json.dumps({"code": code, **controls}), encoding="utf-8")
    monkeypatch.setenv("BOWHEAD_SANDBOX_BACKEND", "subprocess")
    monkeypatch.setattr(sys, "argv", ["runner.py", "--input", str(tmp_path / "in.json"), "--workdir", str(workdir)])
    assert runner.main() == 0
    return json.loads((workdir / "result.json").read_text(encoding="utf-8"))


_WRITE = "for name in ('table.csv', 'report.html', 'fix.html', 'draft.csv'):\n    open(name, 'w').write('x')\n"


@pytest.mark.parametrize(
    ("controls", "expected"),
    [
        ({}, {"files": ["draft.csv", "table.csv"], "reason": "no_purpose"}),
        ({"_work_outputs": ["draft.csv"]}, {"files": ["table.csv"], "reason": "no_purpose"}),
        (
            {"_output_purpose": "deliverable", "_deliverable_outputs": ["report.html", "table.csv"]},
            {"files": ["draft.csv", "fix.html"], "reason": "listed_other_files"},
        ),
        ({"_output_purpose": "analysis", "_output_purpose_declared": True}, None),
    ],
    ids=["no-purpose", "declared-work-file", "other-files-listed", "analysis-declared"],
)
def test_the_runner_names_only_work_files_the_call_did_not_declare(
    runner: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, controls: dict, expected: dict | None
) -> None:
    result = _run(runner, tmp_path, monkeypatch, _WRITE, **controls)
    assert result.get("undeclared_work_files") == expected


def test_a_failed_run_still_names_the_file_it_saved(runner: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The observed case: the CSV was written, then the code failed while building the HTML data.
    result = _run(runner, tmp_path, monkeypatch, "open('per_visit.csv', 'w').write('x')\nraise KeyError('2026-01')\n")
    assert result["ok"] is False
    assert result["undeclared_work_files"] == {"files": ["per_visit.csv"], "reason": "no_purpose"}


def test_the_note_states_the_consequence_and_the_way_to_deliver() -> None:
    one = _undeclared_work_file_note({"files": ["per_visit.csv"], "reason": "no_purpose"})["work_file_note"]
    assert one == (
        "The user does not receive per_visit.csv: this call declared no purpose, so it was saved as a work file, not as a "
        "result file. To give it to the user, save it with purpose='deliverable' and list it in deliverable_outputs."
    )
    two = _undeclared_work_file_note({"files": ["a.csv", "fix.html"], "reason": "listed_other_files"})["work_file_note"]
    assert two.startswith("The user does not receive a.csv, fix.html: this call listed other files in deliverable_outputs, so they were")
    assert _undeclared_work_file_note({"files": [], "reason": "no_purpose"}) == {}
    assert _undeclared_work_file_note({"files": ["a.csv"], "reason": "unknown"}) == {}
    assert _undeclared_work_file_note(None) == {}


async def test_the_tool_result_opens_with_the_note(tmp_path: Path) -> None:
    _mk_skill(tmp_path, "python_sandbox", script="runner.py")
    catalog = SkillCatalog(roots=[tmp_path])
    catalog.refresh()
    saved = {
        "ok": False,
        "exit_code": 1,
        "error": "exit_code=1",
        "outputs": [{"artifact_id": "art_csv", "name": "per_visit.csv", "mime": "text/csv"}],
        "undeclared_work_files": {"files": ["per_visit.csv"], "reason": "no_purpose"},
    }
    tool = _flat_tool(catalog, _FixedRuntime(_FakeResult(result_json=saved)))
    out = await tool.handler({"code": "x"}, SimpleNamespace(user_id="u", session_id="s1", workspace_id="w1", turn_id="t1"))
    assert next(iter(out)) == "work_file_note"
    assert out["work_file_note"].startswith("The user does not receive per_visit.csv:")
    assert "undeclared_work_files" not in json.dumps(out["result_json"])
