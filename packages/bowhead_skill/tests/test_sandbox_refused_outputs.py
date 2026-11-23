"""A file the code wrote but the file store did not keep is reported as not saved, with its reason.

116 (2026-11-12 and 2026-11-18): seven sandbox steps wrote a .pptx the upload refused with 415. The code had
exited 0, but the step showed a code error ("代码运行出错") and the model read only "Data artifact upload failed"
or "HTTP Error 415", so it repeated the same broken slide removal five times.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

_RUNNER_PATH = Path(__file__).resolve().parents[1] / "catalog" / "python_sandbox" / "scripts" / "runner.py"


@pytest.fixture
def runner() -> ModuleType:
    spec = importlib.util.spec_from_file_location("_refused_outputs_runner", _RUNNER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(runner: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: str, upload, **controls) -> dict:
    monkeypatch.setattr(runner, "_upload_artifact", upload)
    workdir = tmp_path / "wd"
    (tmp_path / "in.json").write_text(json.dumps({"code": code, **controls}), encoding="utf-8")
    monkeypatch.setenv("BOWHEAD_SANDBOX_BACKEND", "subprocess")
    monkeypatch.setattr(sys, "argv", ["runner.py", "--input", str(tmp_path / "in.json"), "--workdir", str(workdir)])
    assert runner.main() == 0
    return json.loads((workdir / "result.json").read_text(encoding="utf-8"))


def _store_except(refused: dict[str, str | None]):
    def upload(path, _gateway, *_args, **_kwargs):
        name = Path(path).name
        if name in refused:
            return {"name": name, "refused": refused[name]} if refused[name] else None
        return {"name": name, "artifact_id": "art_000000000001", "mime": "text/plain", "size": Path(path).stat().st_size}

    return upload


def test_a_refused_deck_is_not_saved_with_its_reason_and_the_code_did_not_fail(runner, tmp_path, monkeypatch) -> None:
    code = "open('deck.pptx', 'wb').write(b'PK')\nopen('notes.txt', 'w').write('kept')\n"
    result = _run(runner, tmp_path, monkeypatch, code, _store_except({"deck.pptx": "file_office_duplicate_part"}))
    assert result["ok"] is False
    assert result["exit_code"] == 0
    assert result["outputs_not_saved"] == ["deck.pptx"]
    assert "sandbox file deck.pptx was not saved: the Office package contains two parts with the same name" in result["error"]
    assert "python-pptx names a new slide part slide{count+1}.xml" in result["error"]
    assert "slide.part.partname = PackURI('/ppt/slides/slide<n>.xml')" in result["error"]
    assert "(file_office_duplicate_part)" in result["error"]
    assert "Data artifact upload failed" not in result["error"]
    assert [output["name"] for output in result["outputs"]] == ["notes.txt"]


def test_an_uncoded_upload_failure_is_still_a_file_not_saved(runner, tmp_path, monkeypatch) -> None:
    result = _run(runner, tmp_path, monkeypatch, "open('deck.pptx', 'wb').write(b'PK')\n", _store_except({"deck.pptx": None}))
    assert (result["ok"], result["exit_code"], result["outputs_not_saved"]) == (False, 0, ["deck.pptx"])
    assert "sandbox file deck.pptx Data artifact upload failed" in result["error"]


def test_a_refused_declared_nested_deliverable_is_not_saved(runner, tmp_path, monkeypatch) -> None:
    code = "import os\nos.makedirs('out', exist_ok=True)\nopen('out/deck.pptx', 'wb').write(b'PK')\n"
    result = _run(
        runner,
        tmp_path,
        monkeypatch,
        code,
        _store_except({"deck.pptx": "file_office_invalid_package"}),
        _output_purpose="deliverable",
        _deliverable_outputs=["out/deck.pptx"],
    )
    assert (result["ok"], result["exit_code"], result["outputs_not_saved"]) == (False, 0, ["out/deck.pptx"])
    assert "sandbox file deck.pptx was not saved: it is not a valid Office package" in result["error"]


def test_a_declared_deliverable_the_code_never_wrote_stays_a_code_failure(runner, tmp_path, monkeypatch) -> None:
    result = _run(
        runner,
        tmp_path,
        monkeypatch,
        "print('nothing written')\n",
        _store_except({}),
        _output_purpose="deliverable",
        _deliverable_outputs=["deck.pptx"],
    )
    assert result["ok"] is False
    assert result["exit_code"] == 1
    assert "outputs_not_saved" not in result


def test_a_refused_deliverable_is_not_also_reported_as_missing(runner, tmp_path, monkeypatch) -> None:
    # 116 (1.7.63): the runtime declared 删页测试.pptx a deliverable; once refused, the missing-deliverable check
    # also reported it ("were not produced and persisted") and set exit_code=1, so the step read "代码运行出错".
    result = _run(
        runner,
        tmp_path,
        monkeypatch,
        "open('deck.pptx', 'wb').write(b'PK')\n",
        _store_except({"deck.pptx": "file_office_duplicate_part"}),
        _output_purpose="deliverable",
        _deliverable_outputs=["deck.pptx"],
    )
    assert (result["ok"], result["exit_code"], result["outputs_not_saved"]) == (False, 0, ["deck.pptx"])
    assert "were not produced" not in result["error"]
