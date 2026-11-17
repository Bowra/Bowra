"""Declared nested files and folders, and the notice about unsaved nested files.

Design: docs/sprints/sandbox-nested-deliverables-2026-11-16/tech-design.md §4.2–§4.4. Case: on 116
(the regression case) a finished game written to /workdir/out/ was lost twelve times without notice.
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
import zipfile
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
from bowhead_skill.tools import _output_artifacts_from_result_json, _same_name_revisions, _same_name_version_facts, _validated_revises

_RUNNER_PATH = Path(__file__).resolve().parents[1] / "catalog" / "python_sandbox" / "scripts" / "runner.py"


@pytest.fixture
def runner() -> ModuleType:
    spec = importlib.util.spec_from_file_location("_nested_deliverables_runner", _RUNNER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(runner: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: str, **controls) -> tuple[dict, list[dict]]:
    """Run main() with the subprocess backend; uploads are recorded instead of sent."""
    uploads: list[dict] = []

    def upload(path, _gateway, *_args, **kwargs):
        data = Path(path).read_bytes()
        uploads.append({"name": Path(path).name, "kind": kwargs.get("kind"), "version_of": kwargs.get("version_of"), "bytes": data})
        return {"name": Path(path).name, "artifact_id": f"art_{len(uploads):012d}", "mime": "application/octet-stream", "size": len(data)}

    monkeypatch.setattr(runner, "_upload_artifact", upload)
    workdir = tmp_path / "wd"
    payload = {"code": code, **controls}
    (tmp_path / "in.json").write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setenv("BOWHEAD_SANDBOX_BACKEND", "subprocess")
    monkeypatch.setattr(sys, "argv", ["runner.py", "--input", str(tmp_path / "in.json"), "--workdir", str(workdir)])
    assert runner.main() == 0
    return json.loads((workdir / "result.json").read_text(encoding="utf-8")), uploads


_WRITE_GAME = """
import os
for folder in ("build", "site/js", "site/__pycache__", "out"):
    os.makedirs(folder, exist_ok=True)
open("game.html", "w").write("<html>top</html>")
open("build/report.pdf", "wb").write(b"%PDF-1.4 report")
open("site/index.html", "w").write('<script src="js/game.js"></script>')
open("site/js/game.js", "w").write("console.log('fly')")
open("site/__pycache__/x.cpython-312.pyc", "wb").write(b"pyc")
open("out/check.js", "w").write("check")
"""


def test_declared_nested_file_and_folder_are_delivered_and_the_rest_is_reported(runner, tmp_path, monkeypatch) -> None:
    result, uploads = _run(
        runner,
        tmp_path,
        monkeypatch,
        _WRITE_GAME,
        _output_purpose="deliverable",
        _deliverable_outputs=["game.html", "build/report.pdf", "site/"],
    )
    assert result["ok"] is True, result.get("error")
    by_name = {output["name"]: output for output in result["outputs"]}
    assert by_name["report.pdf"]["path"] == "build/report.pdf"
    assert by_name["site.zip"]["path"] == "site/" and by_name["site.zip"]["files"] == 2
    assert "path" not in by_name["game.html"]
    kinds = {upload["name"]: upload["kind"] for upload in uploads}
    assert kinds["report.pdf"] == "deliverable" and kinds["site.zip"] == "deliverable" and kinds["game.html"] == "deliverable"
    archive = next(upload["bytes"] for upload in uploads if upload["name"] == "site.zip")
    with zipfile.ZipFile(io.BytesIO(archive)) as zf:
        assert zf.namelist() == ["site/index.html", "site/js/game.js"]
    # The undeclared nested file is named, with how to deliver it; the run itself still succeeded.
    assert result["not_saved"] == ["out/check.js"]
    assert "[runner] These files were written below the top level and were not saved: out/check.js" in result["stderr"]
    assert "deliverable_outputs" in result["stderr"]


def test_case_on_116_names_the_unsaved_game(runner, tmp_path, monkeypatch) -> None:
    code = (
        "import os\nos.makedirs('/'.join(['out']), exist_ok=True)\nopen('out/dragonball_n64.html', 'w').write('<html></html>')\nprint('bytes: 13')\n"
    )
    result, uploads = _run(runner, tmp_path, monkeypatch, code)
    assert result["ok"] is True
    assert result["not_saved"] == ["out/dragonball_n64.html"]
    assert [upload["name"] for upload in uploads] == ["sandbox-stdout.txt"]


def test_folder_archive_is_deterministic(runner, tmp_path) -> None:
    (tmp_path / "site/b").mkdir(parents=True)
    (tmp_path / "site/a.txt").write_text("a")
    (tmp_path / "site/b/c.txt").write_text("c")
    first, count = runner._pack_declared_folder(tmp_path, ("site",), tmp_path)
    data = first.read_bytes()
    second, _ = runner._pack_declared_folder(tmp_path, ("site",), tmp_path)
    assert count == 2 and second.read_bytes() == data


def test_folder_inside_a_folder_keeps_its_own_name_as_the_archive_root(runner, tmp_path) -> None:
    (tmp_path / "dist/web").mkdir(parents=True)
    (tmp_path / "dist/web/index.html").write_text("x")
    target, _ = runner._pack_declared_folder(tmp_path, ("dist", "web"), tmp_path)
    assert target.name == "web.zip"
    with zipfile.ZipFile(target) as zf:
        assert zf.namelist() == ["web/index.html"]


@pytest.mark.parametrize("shape", ["component", "file_inside", "folder_inside"])
def test_links_anywhere_fail_the_entry_with_their_path(runner, tmp_path, shape) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("host data")
    work = tmp_path / "wd"
    work.mkdir()
    if shape == "component":
        (work / "build").symlink_to(outside)
        with pytest.raises(runner._DeclaredOutputError, match="symbolic link: build"):
            runner._declared_file(work, ("build", "secret.txt"))
        with pytest.raises(runner._DeclaredOutputError, match="symbolic link: build"):
            runner._pack_declared_folder(work, ("build",), tmp_path)
        return
    (work / "site").mkdir()
    (work / "site/index.html").write_text("x")
    if shape == "file_inside":
        (work / "site/secret.txt").symlink_to(outside / "secret.txt")
        expected = "site/secret.txt"
    else:
        (work / "site/linked").symlink_to(outside)
        expected = "site/linked"
    with pytest.raises(runner._DeclaredOutputError, match=f"symbolic link: {expected}"):
        runner._pack_declared_folder(work, ("site",), tmp_path)


def test_folder_limits(runner, tmp_path, monkeypatch) -> None:
    (tmp_path / "many").mkdir()
    for index in range(3):
        (tmp_path / f"many/{index}.txt").write_text("x")
    monkeypatch.setattr(runner, "_MAX_FOLDER_FILES", 2)
    with pytest.raises(runner._DeclaredOutputError, match="more than 2 files"):
        runner._pack_declared_folder(tmp_path, ("many",), tmp_path)
    monkeypatch.setattr(runner, "_MAX_FOLDER_FILES", 2000)
    monkeypatch.setattr(runner, "_MAX_OUTPUT_BYTES", 2)
    with pytest.raises(runner._DeclaredOutputError, match="holds more than"):
        runner._pack_declared_folder(tmp_path, ("many",), tmp_path)


def test_a_failed_entry_fails_the_call_and_keeps_other_outputs(runner, tmp_path, monkeypatch) -> None:
    code = "import os\nos.makedirs('site', exist_ok=True)\nopen('game.html', 'w').write('x')\nopen('site/index.html', 'w').write('x')\nos.symlink('/etc/hosts', 'site/hosts')\n"
    result, uploads = _run(
        runner,
        tmp_path,
        monkeypatch,
        code,
        _output_purpose="deliverable",
        _deliverable_outputs=["game.html", "site/", "build/missing.pdf"],
    )
    assert result["ok"] is False
    assert "declared deliverable site/ contains a symbolic link: site/hosts" in result["error"]
    assert "declared deliverable build/missing.pdf was not written" in result["error"]
    # Each failed entry is reported once, with its own reason, not again as a generic missing output.
    assert "not produced and persisted" not in result["error"]
    assert "game.html" in [upload["name"] for upload in uploads] and "site.zip" not in [upload["name"] for upload in uploads]
    assert "not_saved" not in result


def test_nested_file_cannot_take_the_name_of_a_top_level_output(runner, tmp_path, monkeypatch) -> None:
    code = "import os\nos.makedirs('build', exist_ok=True)\nopen('report.pdf', 'w').write('a')\nopen('build/report.pdf', 'w').write('b')\n"
    result, _ = _run(runner, tmp_path, monkeypatch, code, _output_purpose="deliverable", _deliverable_outputs=["build/report.pdf"])
    assert result["ok"] is False
    assert "would be saved as report.pdf, which this run also wrote at the top level" in result["error"]


def test_runner_rechecks_entries_it_receives(runner, tmp_path, monkeypatch) -> None:
    result, uploads = _run(runner, tmp_path, monkeypatch, "pass\n", _output_purpose="deliverable", _deliverable_outputs=["../escape.txt"])
    assert result["ok"] is False
    assert "declared deliverable ../escape.txt is not a relative path inside the working directory" in result["error"]
    assert [upload["name"] for upload in uploads] == []


def test_nested_entry_revises_its_earlier_version(runner, tmp_path, monkeypatch) -> None:
    code = "import os\nos.makedirs('site', exist_ok=True)\nopen('site/index.html', 'w').write('v2')\n"
    _, uploads = _run(
        runner,
        tmp_path,
        monkeypatch,
        code,
        _output_purpose="deliverable",
        _deliverable_outputs=["site/"],
        _revises={"site/": "art_previous0001"},
    )
    assert next(upload for upload in uploads if upload["name"] == "site.zip")["version_of"] == "art_previous0001"


def test_unsaved_notice_groups_folders_and_skips_what_is_not_new_work(runner, tmp_path) -> None:
    for name in ("input.json", "user.py", "dirty_table.py", "period_growth.py"):
        (tmp_path / name).write_text("x")
    (tmp_path / "staged").mkdir()
    (tmp_path / "staged/helper.py").write_text("x")
    pre_existing = {path.name for path in tmp_path.iterdir()}
    (tmp_path / "assets").mkdir()
    for index in range(300):
        (tmp_path / f"assets/s2_{index}.png").write_bytes(b"png")
    (tmp_path / "out").mkdir()
    (tmp_path / "out/dragonball_n64.html").write_text("x")
    (tmp_path / "out/check.js").write_text("x")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__/period_growth.cpython-312.pyc").write_bytes(b"pyc")
    (tmp_path / "out/__pycache__").mkdir()
    (tmp_path / "out/__pycache__/x.pyc").write_bytes(b"pyc")
    (tmp_path / "inputs").mkdir()
    (tmp_path / "inputs/rows.csv").write_text("a")
    (tmp_path / "site").mkdir()
    (tmp_path / "site/index.html").write_text("x")
    (tmp_path / "build").mkdir()
    (tmp_path / "build/report.pdf").write_text("x")
    (tmp_path / "build/report.aux").write_text("x")
    (tmp_path / "linked").symlink_to(tmp_path / "site")
    lines = runner._unsaved_nested_files(tmp_path, pre_existing, [(("site",), True), (("build", "report.pdf"), False)])
    assert lines == ["assets/ (300 files)", "build/report.aux", "out/check.js, out/dragonball_n64.html"]


def test_unsaved_notice_is_capped(runner, tmp_path) -> None:
    for index in range(20):
        (tmp_path / f"d{index:02d}").mkdir()
        (tmp_path / f"d{index:02d}/f.txt").write_text("x")
    assert len(runner._unsaved_nested_files(tmp_path, set(), [])) == 12


def test_skill_mode_build_folders_are_not_reported(runner, tmp_path, monkeypatch) -> None:
    """A Skill keeps intermediate files in nested build folders on purpose (LaTeX); only direct code runs get the notice."""
    from bowhead_skill.catalog import builtin_catalog_dir

    monkeypatch.setenv("BOWHEAD_SANDBOX_BACKEND", "executor")

    def execute(_url, _token, **kwargs):
        workdir = kwargs["workdir"]
        (workdir / "build").mkdir()
        (workdir / "build/main.aux").write_text("aux")
        (workdir / "main.tex").write_text("\\documentclass{article}")
        return {"ok": True, "exit_code": 0, "stdout": "", "stderr": "", "duration_ms": 1}

    monkeypatch.setattr(runner, "_run_via_executor", execute)
    monkeypatch.setattr(runner, "_upload_artifact", lambda path, *_a, **_k: {"name": Path(path).name, "artifact_id": "art_000000000001", "size": 1})
    payload = {
        "_skill_dir": str(builtin_catalog_dir() / "latex-report"),
        "_skill_entry": "compile_report.py",
        "_requires_isolated_runtime": True,
        "_sandbox_executor_url": "http://executor.test",
        "_sandbox_executor_token": "secret",
        "source": "source",
    }
    (tmp_path / "in.json").write_text(json.dumps(payload))
    workdir = tmp_path / "wd"
    workdir.mkdir()
    monkeypatch.setattr(sys, "argv", ["runner.py", "--input", str(tmp_path / "in.json"), "--workdir", str(workdir)])
    runner.main()
    result = json.loads((workdir / "result.json").read_text())
    assert "not_saved" not in result
    assert "were not saved" not in (result.get("stderr") or "")


def test_missing_folder_hint_explains_the_fresh_working_directory(runner, tmp_path) -> None:
    for missing in ("work/game.html", "/workdir/work/game.html", "./work/game.html"):
        result = {"ok": False, "stderr": f"FileNotFoundError: [Errno 2] No such file or directory: '{missing}'", "error": "exit_code=1"}
        runner._hint_missing_file(result, {}, tmp_path)
        assert "Every call starts in an empty working directory" in result["stderr"]
        assert "folder work/" in result["stderr"]
        assert "一个 input 都没声明" not in result["stderr"]


def test_missing_folder_hint_stays_out_when_the_folder_exists_or_is_an_input_path(runner, tmp_path) -> None:
    (tmp_path / "work").mkdir()
    existing = {"ok": False, "stderr": "FileNotFoundError: [Errno 2] No such file or directory: 'work/rows.csv'", "error": "exit_code=1"}
    runner._hint_missing_file(existing, {}, tmp_path)
    assert "Every call starts in an empty working directory" not in existing["stderr"]
    assert "一个 input 都没声明" in existing["stderr"]
    for path in ("inputs/rows.csv", "INPUTS/sales/x.csv", "rows.csv", "/data/rows.csv"):
        result = {"ok": False, "stderr": f"FileNotFoundError: [Errno 2] No such file or directory: '{path}'", "error": "exit_code=1"}
        runner._hint_missing_file(result, {}, tmp_path)
        assert "Every call starts in an empty working directory" not in result["stderr"], path


def test_tool_result_projects_entry_path_and_file_count() -> None:
    projected = _output_artifacts_from_result_json(
        {"outputs": [{"name": "site.zip", "artifact_id": "art_000000000001", "mime": "application/zip", "size": 9, "path": "site/", "files": 2}]}
    )
    assert projected == [{"artifact_id": "art_000000000001", "name": "site.zip", "mime": "application/zip", "size": 9, "path": "site/", "files": 2}]


def test_version_facts_match_nested_outputs_by_entry() -> None:
    facts = _same_name_version_facts({"site/": "art_previous0001"}, [{"name": "site.zip", "path": "site/", "artifact_id": "art_000000000002"}])
    assert facts["recorded_as_new_version"] == [{"filename": "site/", "artifact_id": "art_000000000002", "previous_version": "art_previous0001"}]


class _VersionStore:
    def __init__(self, filename: str) -> None:
        self.target = SimpleNamespace(id="art_previous0001", filename=filename)

    async def versions_supported(self) -> bool:
        return True

    async def check_version_target(self, artifact_id, **_kwargs):
        return self.target

    async def get(self, artifact_id):
        return self.target if artifact_id == self.target.id else None

    async def latest_live_version_id(self, target):
        return target.id


async def test_folder_entry_revises_an_archive_of_the_same_name() -> None:
    ctx = SimpleNamespace(artifact_store=_VersionStore("site.zip"), revision_target_files=["art_previous0001"], user_id="u", session_id="s")
    assert await _same_name_revisions(ctx, ["site/"], {}) == {"site/": "art_previous0001"}
    assert (await _validated_revises({"site/": "art_previous0001"}, ["site/"], ctx))["revises"] == {"site/": "art_previous0001"}
    other = SimpleNamespace(artifact_store=_VersionStore("game.html"), user_id="u", session_id="s")
    refused = await _validated_revises({"site/": "art_previous0001"}, ["site/"], other)
    assert refused["reason_code"] == "version_target_format_changed"
