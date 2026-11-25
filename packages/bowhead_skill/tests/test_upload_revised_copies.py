"""A revised copy of an upload: a new file named like the deck editor's copy that records the upload.

pptx dogfood 2026-11-25 (docs/sprints/pptx-upload-edit-dogfood-2026-11-23/spike.md): uploads are never
versioned, so a helper asked to revise an upload saved a second file with the upload's name and no link
to it. In a dispatch revising this person's upload, the deliverable named like the upload is saved as
``<name>（已修改）`` with ``derived_from`` the upload; a later save of it in the same run is its next
version and keeps that name.
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
from bowhead_skill.tools import _output_artifacts_from_result_json, _same_name_revisions, _upload_copies

_RUNNER_PATH = Path(__file__).resolve().parents[1] / "catalog" / "python_sandbox" / "scripts" / "runner.py"
_COPIES = {"art_upload": ("deck.pptx", "deck（已修改）.pptx")}


def _ctx(**extra: Any) -> SimpleNamespace:
    return SimpleNamespace(user_id="u", session_id="s1", workspace_id="w1", revision_target_files=("art_upload",), **extra)


@pytest.mark.parametrize(
    ("outputs", "revises", "helper_versions", "expected"),
    [
        (["deck.pptx"], {}, {}, {"deck.pptx": {"filename": "deck（已修改）.pptx", "upload": "art_upload"}}),
        # The model already named the output like the copy.
        (["deck（已修改）.pptx"], {}, {}, {"deck（已修改）.pptx": {"filename": "deck（已修改）.pptx", "upload": "art_upload"}}),
        # A nested entry is matched by the name it is saved under.
        (["out/deck.pptx"], {}, {}, {"out/deck.pptx": {"filename": "deck（已修改）.pptx", "upload": "art_upload"}}),
        # This run's own earlier copy: the next version keeps the copy name.
        (
            ["deck.pptx"],
            {"deck.pptx": "art_copy"},
            {"deck.pptx": "art_copy"},
            {"deck.pptx": {"filename": "deck（已修改）.pptx", "upload": "art_upload"}},
        ),
        # The model mapped the output to another file on purpose.
        (["deck.pptx"], {"deck.pptx": "art_other"}, {}, {}),
        (["summary.pptx"], {}, {}, {}),
    ],
    ids=["upload_name", "copy_name", "nested", "own_copy_next_version", "model_mapped", "other_name"],
)
def test_the_output_named_like_the_upload_is_its_revised_copy(
    outputs: list[str], revises: dict[str, str], helper_versions: dict[str, str], expected: dict[str, Any]
) -> None:
    assert _upload_copies(_ctx(revision_upload_copies=_COPIES), outputs, revises, helper_versions) == expected


def test_without_an_upload_of_this_person_nothing_is_renamed() -> None:
    assert _upload_copies(_ctx(), ["deck.pptx"], {}, {}) == {}
    assert _upload_copies(_ctx(revision_upload_copies={"art_upload": ("deck.pptx", "")}), ["deck.pptx"], {}, {}) == {}


@pytest.mark.anyio
async def test_an_upload_is_not_tried_as_a_version_target() -> None:
    class _Store:
        async def get(self, artifact_id: str) -> Any:
            raise AssertionError(f"looked up {artifact_id}")

        async def latest_live_version_id(self, artifact: Any) -> str:
            raise AssertionError("no version lookup for an upload")

    ctx = _ctx(revision_upload_copies=_COPIES, artifact_store=_Store())
    assert await _same_name_revisions(ctx, ["deck.pptx"], {}) == {}


async def test_python_exec_saves_the_copy_and_says_under_which_name(tmp_path: Path) -> None:
    from test_sandbox_workdir_contract import _FakeResult, _FixedRuntime, _flat_tool, _mk_skill

    from bowhead_skill import SkillCatalog

    _mk_skill(tmp_path, "python_sandbox", script="runner.py")
    cat = SkillCatalog(roots=[tmp_path])
    cat.refresh()
    saved = {
        "outputs": [
            {"artifact_id": "art_copy", "name": "deck.pptx", "saved_as": "deck（已修改）.pptx", "derived_from": "art_upload", "size": 10},
        ]
    }
    rt = _FixedRuntime(_FakeResult(result_json=saved))
    out = await _flat_tool(cat, rt).handler(
        # A model-supplied control key is never trusted.
        {
            "code": "x",
            "purpose": "deliverable",
            "deliverable_outputs": ["deck.pptx"],
            "_derived_copies": {"deck.pptx": {"filename": "x", "upload": "y"}},
        },
        _ctx(revision_upload_copies=_COPIES, artifact_store=None),
    )
    assert rt.calls[0]["_derived_copies"] == {"deck.pptx": {"filename": "deck（已修改）.pptx", "upload": "art_upload"}}
    assert "_revises" not in rt.calls[0] and "error_kind" not in out
    assert out["output_artifacts"][0] | {} == {
        "artifact_id": "art_copy",
        "name": "deck.pptx",
        "mime": None,
        "size": 10,
        "saved_as": "deck（已修改）.pptx",
        "derived_from": "art_upload",
    }
    assert "saved_as" in out["_upload_copy_note"] and "upload is unchanged" in out["_upload_copy_note"]


def test_an_ordinary_output_has_no_copy_fields() -> None:
    projected = _output_artifacts_from_result_json({"outputs": [{"artifact_id": "art_1", "name": "a.csv", "size": 1}]})
    assert projected == [{"artifact_id": "art_1", "name": "a.csv", "mime": None, "size": 1}]


@pytest.fixture
def runner() -> ModuleType:
    spec = importlib.util.spec_from_file_location("_upload_copy_runner", _RUNNER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_upload_stores_the_copy_name_and_its_source(runner: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    output = tmp_path / "deck.pptx"
    output.write_bytes(b"PK deck")
    seen: list[Any] = []

    class _Response(io.BytesIO):
        def __enter__(self) -> _Response:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

    def accept(request: Any, timeout: int = 0) -> Any:
        seen.append(request)
        return _Response(json.dumps({"file_id": "art_copy", "size": 7}).encode())

    monkeypatch.setattr(runner.urllib.request, "urlopen", accept)
    rec = runner._upload_artifact(output, "http://gw", kind="deliverable", derived_from="art_upload", saved_name="deck（已修改）.pptx")
    assert "derived_from=art_upload" in seen[0].full_url
    assert 'filename="deck（已修改）.pptx"'.encode() in seen[0].data
    assert rec == {
        "name": "deck.pptx",
        "artifact_id": "art_copy",
        "mime": rec["mime"],
        "size": 7,
        "saved_as": "deck（已修改）.pptx",
        "derived_from": "art_upload",
    }
    plain = runner._upload_artifact(output, "http://gw", kind="deliverable")
    assert b'filename="deck.pptx"' in seen[1].data and "saved_as" not in plain and "derived_from" not in plain


def _run(runner: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: str, **controls: Any) -> list[dict[str, Any]]:
    uploads: list[dict[str, Any]] = []

    def upload(path: Any, _gateway: str, *_args: Any, **kwargs: Any) -> dict[str, Any]:
        uploads.append({"name": Path(path).name, **{key: kwargs.get(key) for key in ("version_of", "derived_from", "saved_name")}})
        return {"name": Path(path).name, "artifact_id": f"art_{len(uploads):012d}", "size": 1}

    monkeypatch.setattr(runner, "_upload_artifact", upload)
    workdir = tmp_path / "wd"
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "in.json").write_text(json.dumps({"code": code, **controls}), encoding="utf-8")
    monkeypatch.setenv("BOWHEAD_SANDBOX_BACKEND", "subprocess")
    monkeypatch.setattr(sys, "argv", ["runner.py", "--input", str(tmp_path / "in.json"), "--workdir", str(workdir)])
    assert runner.main() == 0
    return uploads


def test_runner_saves_a_new_copy_with_its_source_and_a_later_save_as_its_version(
    runner: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    copy = {"filename": "deck（已修改）.pptx", "upload": "art_upload"}
    code = "open('deck.pptx','wb').write(b'PK'); open('notes.pptx','wb').write(b'PK')"
    controls = {"_output_purpose": "deliverable", "_deliverable_outputs": ["deck.pptx", "notes.pptx"]}
    first = _run(runner, tmp_path / "a", monkeypatch, code, **controls, _derived_copies={"deck.pptx": copy})
    assert {item["name"]: item for item in first}["deck.pptx"] == {
        "name": "deck.pptx",
        "version_of": None,
        "derived_from": "art_upload",
        "saved_name": "deck（已修改）.pptx",
    }
    assert {item["name"]: item for item in first}["notes.pptx"] == {
        "name": "notes.pptx",
        "version_of": None,
        "derived_from": None,
        "saved_name": None,
    }
    later = _run(runner, tmp_path / "b", monkeypatch, code, **controls, _derived_copies={"deck.pptx": copy}, _revises={"deck.pptx": "art_copy"})
    assert {item["name"]: item for item in later}["deck.pptx"] == {
        "name": "deck.pptx",
        "version_of": "art_copy",
        "derived_from": None,
        "saved_name": "deck（已修改）.pptx",
    }
    # Entries outside the declared deliverables are ignored.
    stray = _run(runner, tmp_path / "c", monkeypatch, code, **controls, _derived_copies={"other.pptx": copy})
    assert all(item["saved_name"] is None for item in stray)
