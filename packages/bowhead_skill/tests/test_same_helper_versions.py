"""A helper's same-name rewrite in one Turn is the next version of its earlier copy.

docs/sprints/same-name-results-versions-2026-11-17/tech-design.md (owner decisions D1, D3): the tool
targets the helper's previous copy of a declared entry, the gateway checks it like any version, and a
refused target leaves a new file: before the run (pre-check) or at upload (the runner saves it again).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
from bowhead_skill.tools import _same_helper_revisions, _same_name_version_facts
from test_sandbox_workdir_contract import _FakeResult, _FixedRuntime, _flat_tool, _mk_skill

from bowhead_skill import SkillCatalog

_RUNNER_PATH = Path(__file__).resolve().parents[1] / "catalog" / "python_sandbox" / "scripts" / "runner.py"


class _Refused(Exception):
    def __init__(self, reason: str, latest_id: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.latest_id = latest_id


class _Store:
    """Version targets by id; ``refuse`` maps an id to the gateway's refusal."""

    def __init__(self, names: dict[str, str], refuse: dict[str, _Refused] | None = None) -> None:
        self.names = names
        self.refuse = refuse or {}

    async def versions_supported(self) -> bool:
        return True

    async def check_version_target(self, target_id: str, **_scope: Any) -> Any:
        if target_id in self.refuse:
            raise self.refuse[target_id]
        return SimpleNamespace(id=target_id, filename=self.names[target_id])

    async def get(self, artifact_id: str) -> Any:
        return SimpleNamespace(id=artifact_id, created_by_turn="t_other")


def _ctx(store: _Store, previous: dict[str, str], recorded: list[tuple[str, str]]) -> SimpleNamespace:
    """Core's hooks for one helper: its previous copy per entry, and what the call records."""
    return SimpleNamespace(
        artifact_store=store,
        user_id="u",
        session_id="s1",
        workspace_id="w1",
        turn_id="t_game",
        same_helper_previous_copy=previous.get,
        record_same_helper_copy=lambda entry, artifact_id: recorded.append((entry, artifact_id)),
    )


def _tool(tmp_path: Path, saved: dict[str, Any]) -> tuple[Any, _FixedRuntime]:
    _mk_skill(tmp_path, "python_sandbox", script="runner.py")
    catalog = SkillCatalog(roots=[tmp_path])
    catalog.refresh()
    runtime = _FixedRuntime(_FakeResult(result_json=saved))
    return _flat_tool(catalog, runtime), runtime


async def test_a_rewrite_of_the_helpers_own_copy_is_its_next_version(tmp_path: Path) -> None:
    # Observed case: one helper wrote dragonball_kinto_un.html six times; each became a separate file.
    store = _Store({"art_1": "dragonball_kinto_un.html"})
    recorded: list[tuple[str, str]] = []
    tool, runtime = _tool(tmp_path, {"outputs": [{"artifact_id": "art_2", "name": "dragonball_kinto_un.html", "mime": "text/html"}]})
    out = await tool.handler(
        {"code": "x", "purpose": "deliverable", "deliverable_outputs": ["dragonball_kinto_un.html"]},
        _ctx(store, {"dragonball_kinto_un.html": "art_1"}, recorded),
    )
    assert runtime.calls[0]["_revises"] == {"dragonball_kinto_un.html": "art_1"}
    assert runtime.calls[0]["_revises_implicit"] == ["dragonball_kinto_un.html"]
    assert out["recorded_as_new_version"] == [{"filename": "dragonball_kinto_un.html", "artifact_id": "art_2", "previous_version": "art_1"}]
    assert "already saved earlier in this run" in out["_version_note"]
    assert recorded == [("dragonball_kinto_un.html", "art_2")], "The next call targets the newest copy"


async def test_the_first_copy_and_other_entries_stay_new_files(tmp_path: Path) -> None:
    recorded: list[tuple[str, str]] = []
    saved = {
        "outputs": [
            {"artifact_id": "art_home", "name": "index.html", "path": "site/index.html"},
            {"artifact_id": "art_stdout", "name": "sandbox-stdout.txt", "data_role": "stdout"},
        ]
    }
    tool, runtime = _tool(tmp_path, saved)
    # This helper saved only site/blog/index.html before: site/index.html is another file of that name.
    out = await tool.handler(
        {"code": "x", "purpose": "deliverable", "deliverable_outputs": ["site/index.html"]},
        _ctx(_Store({"art_blog": "index.html"}), {"site/blog/index.html": "art_blog"}, recorded),
    )
    assert "_revises" not in runtime.calls[0] and "_revises_implicit" not in runtime.calls[0]
    assert "recorded_as_new_version" not in out
    assert recorded == [("site/index.html", "art_home")], "stdout and undeclared files are not recorded"


async def test_an_analysis_call_neither_links_nor_records(tmp_path: Path) -> None:
    recorded: list[tuple[str, str]] = []
    tool, runtime = _tool(tmp_path, {"outputs": [{"artifact_id": "art_2", "name": "report.html"}]})
    await tool.handler({"code": "x", "purpose": "analysis"}, _ctx(_Store({"art_1": "report.html"}), {"report.html": "art_1"}, recorded))
    assert "_revises" not in runtime.calls[0] and recorded == []


@pytest.mark.parametrize(
    ("refusal", "names"),
    [
        (_Refused("version_target_not_latest", "art_9"), {"art_1": "report.html"}),
        (None, {"art_1": "report.pdf"}),  # another file type
    ],
    ids=["another_call_saved_a_newer_version", "another_type"],
)
async def test_a_refused_previous_copy_leaves_a_new_file_without_failing(tmp_path: Path, refusal: _Refused | None, names: dict[str, str]) -> None:
    store = _Store(names, refuse={"art_1": refusal} if refusal else None)
    tool, runtime = _tool(tmp_path, {"outputs": [{"artifact_id": "art_2", "name": "report.html"}]})
    out = await tool.handler(
        {"code": "x", "purpose": "deliverable", "deliverable_outputs": ["report.html"]},
        _ctx(store, {"report.html": "art_1"}, []),
    )
    assert "_revises" not in runtime.calls[0] and "error_kind" not in out and "recorded_as_new_version" not in out


async def test_the_models_own_revises_wins_and_each_target_is_used_once() -> None:
    lookup = {"a.html": "art_a", "b.html": "art_shared", "c.html": "art_shared"}
    ctx = SimpleNamespace(same_helper_previous_copy=lookup.get)
    found = _same_helper_revisions(ctx, ["a.html", "b.html", "c.html"], {"a.html": "art_mine"})
    assert found == {"b.html": "art_shared"}
    assert _same_helper_revisions(SimpleNamespace(), ["a.html"], {}) == {}, "Without Core's hook nothing changes"


def test_an_output_saved_as_a_new_file_is_not_stated_as_a_version() -> None:
    saved = [{"artifact_id": "art_new", "name": "report.html"}]
    assert _same_name_version_facts({}, saved, {"report.html": "art_1"}, ["report.html"]) == {}
    facts = _same_name_version_facts({}, saved, {"report.html": "art_1"})
    assert facts["recorded_as_new_version"] == [{"filename": "report.html", "artifact_id": "art_new", "previous_version": "art_1"}]


@pytest.fixture
def runner() -> ModuleType:
    spec = importlib.util.spec_from_file_location("_same_helper_runner", _RUNNER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(
    runner: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: str, refuse: set[str], **controls: Any
) -> tuple[dict, list[dict]]:
    """Run main(); an upload naming a version in ``refuse`` is refused as no longer the latest (D3 race)."""
    uploads: list[dict] = []

    def upload(path, _gateway, *_args, **kwargs):  # type: ignore[no-untyped-def]
        uploads.append({"name": Path(path).name, "version_of": kwargs.get("version_of")})
        if kwargs.get("version_of") in refuse:
            return {"name": Path(path).name, "version_error": "version_target_not_latest", "latest_id": "art_parallel"}
        return {"name": Path(path).name, "artifact_id": f"art_{len(uploads):012d}", "mime": "text/html", "size": 1}

    monkeypatch.setattr(runner, "_upload_artifact", upload)
    workdir = tmp_path / "wd"
    (tmp_path / "in.json").write_text(json.dumps({"code": code, **controls}), encoding="utf-8")
    monkeypatch.setenv("BOWHEAD_SANDBOX_BACKEND", "subprocess")
    monkeypatch.setattr(sys, "argv", ["runner.py", "--input", str(tmp_path / "in.json"), "--workdir", str(workdir)])
    assert runner.main() == 0
    return json.loads((workdir / "result.json").read_text(encoding="utf-8")), uploads


_WRITE = "import os\nos.makedirs('site', exist_ok=True)\nopen('game.html', 'w').write('g')\nopen('site/index.html', 'w').write('i')\n"


def test_a_refused_implicit_version_is_saved_as_a_new_file(runner: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    result, uploads = _run(
        runner,
        tmp_path,
        monkeypatch,
        _WRITE,
        refuse={"art_game_v1", "art_index_v1"},
        _output_purpose="deliverable",
        _deliverable_outputs=["game.html", "site/index.html"],
        _revises={"game.html": "art_game_v1", "site/index.html": "art_index_v1"},
        _revises_implicit=["game.html", "site/index.html"],
    )
    assert result["ok"] is True, result.get("error")
    assert [(upload["name"], upload["version_of"]) for upload in uploads if upload["name"] != "sandbox-stdout.txt"] == [
        ("game.html", "art_game_v1"),
        ("game.html", None),
        ("index.html", "art_index_v1"),
        ("index.html", None),
    ]
    assert result["saved_as_new_file"] == ["game.html", "site/index.html"]


def test_a_refused_version_the_model_asked_for_still_fails(runner: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    result, uploads = _run(
        runner,
        tmp_path,
        monkeypatch,
        _WRITE,
        refuse={"art_game_v1"},
        _output_purpose="deliverable",
        _deliverable_outputs=["game.html"],
        _revises={"game.html": "art_game_v1"},
        _revises_implicit=["other.html"],
    )
    assert result["ok"] is False
    assert "game.html was not stored as a new version of art_game_v1" in result["error"]
    assert [upload["version_of"] for upload in uploads if upload["name"] == "game.html"] == ["art_game_v1"]
    assert "saved_as_new_file" not in result
