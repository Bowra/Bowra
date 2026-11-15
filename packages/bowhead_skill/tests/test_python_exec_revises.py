"""python_exec `revises`: a revised deliverable is declared as the next version of a file.

docs/sprints/file-versions-2026-11-10/tech-design.md W1. Every target is checked before the sandbox
runs; the runner forwards the target with that output and reports a refused version by name.
"""

from __future__ import annotations

import importlib.util
import io
import json
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from bowhead_skill.tools import _validated_revises

_RUNNER_PATH = Path(__file__).resolve().parents[1] / "catalog" / "python_sandbox" / "scripts" / "runner.py"


class _Refused(Exception):
    def __init__(self, reason: str, latest_id: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.latest_id = latest_id


class _Store:
    def __init__(self, *, supported: bool = True, refuse: dict[str, _Refused] | None = None) -> None:
        self.supported = supported
        self.refuse = refuse or {}
        self.checked: list[tuple[str, dict[str, Any]]] = []

    async def versions_supported(self) -> bool:
        return self.supported

    async def check_version_target(self, target_id: str, **scope: Any) -> Any:
        self.checked.append((target_id, scope))
        if target_id in self.refuse:
            raise self.refuse[target_id]
        return SimpleNamespace(id=target_id, filename="report.html")


def _ctx(store: _Store) -> SimpleNamespace:
    return SimpleNamespace(artifact_store=store, user_id="u", session_id="s1", workspace_id="w1")


@pytest.mark.anyio
async def test_valid_revises_are_checked_against_the_callers_session() -> None:
    store = _Store()
    out = await _validated_revises({"report.html": "art_v1"}, ["report.html"], _ctx(store))
    assert out == {"revises": {"report.html": "art_v1"}}
    assert store.checked == [("art_v1", {"user_id": "u", "session_id": "s1", "workspace_id": "w1", "mime": None})]


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("raw", "outputs", "needle"),
    [
        (["art_v1"], ["report.html"], "must map"),
        ({"other.html": "art_v1"}, ["report.html"], "must be one of input.deliverable_outputs"),
        ({"report.html": ""}, ["report.html"], "must be the artifact_id"),
        ({"a.html": "art_v1", "b.html": "art_v1"}, ["a.html", "b.html"], "same file for two outputs"),
    ],
)
async def test_malformed_revises_are_refused_before_the_sandbox(raw: Any, outputs: list[str], needle: str) -> None:
    store = _Store()
    out = await _validated_revises(raw, outputs, _ctx(store))
    assert out["error_kind"] == "invalid_args" and needle in out["error"]
    assert store.checked == []


@pytest.mark.anyio
async def test_a_refused_target_names_its_reason_and_the_latest_version() -> None:
    store = _Store(refuse={"art_v1": _Refused("version_target_not_latest", "art_v2")})
    out = await _validated_revises({"report.html": "art_v1"}, ["report.html"], _ctx(store))
    assert (out["error_kind"], out["reason_code"], out["latest_id"]) == ("invalid_args", "version_target_not_latest", "art_v2")
    assert "Latest version: art_v2" in out["error"]


@pytest.mark.anyio
async def test_a_different_file_type_is_not_a_version() -> None:
    out = await _validated_revises({"report.pdf": "art_v1"}, ["report.pdf"], _ctx(_Store()))
    assert out["reason_code"] == "version_target_format_changed"


@pytest.mark.anyio
async def test_without_version_columns_the_output_is_a_new_file() -> None:
    out = await _validated_revises({"report.html": "art_v1"}, ["report.html"], _ctx(_Store(supported=False)))
    assert out == {"revises": {}}


def _runner() -> Any:
    spec = importlib.util.spec_from_file_location("_revises_runner", _RUNNER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_runner_forwards_the_target_and_reports_a_refused_version(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner = _runner()
    output = tmp_path / "report.html"
    output.write_text("<html></html>")
    seen: list[str] = []

    def refuse(request: Any, timeout: int = 0) -> Any:
        seen.append(request.full_url)
        body = json.dumps({"detail": {"error": {"code": "version_target_not_latest", "latest_id": "art_v2"}}}).encode()
        raise urllib.error.HTTPError(request.full_url, 409, "Conflict", {}, io.BytesIO(body))

    monkeypatch.setattr(runner.urllib.request, "urlopen", refuse)
    rec = runner._upload_artifact(output, "http://gw", kind="deliverable", version_of="art_v1")
    assert "version_of=art_v1" in seen[0]
    assert rec == {"name": "report.html", "version_error": "version_target_not_latest", "latest_id": "art_v2"}


@pytest.mark.anyio
async def test_an_input_with_a_newer_version_is_stated_not_replaced() -> None:
    """File versions R6: the model learns the input has a newer version and decides; the input stays as declared."""
    from bowhead_skill.tools import _input_newer_versions

    class _Versions:
        async def newer_versions(self, refs: dict[str, Any]) -> dict[str, Any]:
            assert set(refs) == {"art_v1", "art_other"}
            return {"art_v1": SimpleNamespace(latest=SimpleNamespace(id="art_v3", version_no=3))}

    input_data = {"inputs": {"report": {"artifact_id": "art_v1"}, "rows": {"artifact_id": "art_other"}}}
    facts = await _input_newer_versions(input_data, SimpleNamespace(artifact_store=_Versions()))
    assert facts == [{"input": "report", "artifact_id": "art_v1", "newer_version": {"id": "art_v3", "version": 3}}]
    assert input_data["inputs"]["report"]["artifact_id"] == "art_v1"


@pytest.mark.anyio
async def test_a_stale_target_after_the_runs_own_save_says_so() -> None:
    """Regression check M3: the refusal names the run's own new version instead of a generic 'newer version exists'."""

    class _OwnRun(_Store):
        async def get(self, artifact_id: str) -> Any:
            return SimpleNamespace(id=artifact_id, created_by_turn="t_run" if artifact_id == "art_v2" else "t_other")

    ctx = SimpleNamespace(**vars(_ctx(_Store())), turn_id="t_run")
    ctx.artifact_store = _OwnRun(refuse={"art_v1": _Refused("version_target_not_latest", "art_v2")})
    out = await _validated_revises({"report.html": "art_v1"}, ["report.html"], ctx)
    assert out["reason_code"] == "version_target_not_latest" and out["latest_id"] == "art_v2"
    assert "This run already saved a new version of this file as art_v2" in out["error"]
    ctx.turn_id = "t_later"
    generic = await _validated_revises({"report.html": "art_v1"}, ["report.html"], ctx)
    assert "A newer version of this file exists" in generic["error"]


class _Lineage(_Store):
    """A revision target `art_v1` whose newest live version is `latest`; `art_upload` cannot be versioned."""

    def __init__(self, *, latest: str = "art_v1", names: dict[str, str] | None = None) -> None:
        super().__init__(refuse={"art_upload": _Refused("version_target_out_of_scope")})
        self.latest = latest
        self.names = {"art_v1": "report.html", "art_v2": "report.html", "art_upload": "input.html", **(names or {})}

    async def get(self, artifact_id: str) -> Any:
        return SimpleNamespace(id=artifact_id, filename=self.names[artifact_id]) if artifact_id in self.names else None

    async def latest_live_version_id(self, artifact: Any) -> str | None:
        return self.latest if artifact.id in {"art_v1", "art_v2"} else artifact.id


def _revise_ctx(store: _Store, targets: tuple[str, ...] = ("art_v1",)) -> SimpleNamespace:
    return SimpleNamespace(artifact_store=store, user_id="u", session_id="s1", workspace_id="w1", revision_target_files=targets)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("outputs", "revises", "targets", "latest", "expected"),
    [
        (["report.html"], {}, ("art_v1",), "art_v1", {"report.html": "art_v1"}),
        # The run already saved v2: the next save follows the newest version, not the dispatch-time target.
        (["report.html"], {}, ("art_v1",), "art_v2", {"report.html": "art_v2"}),
        (["report_v2.html"], {}, ("art_v1",), "art_v1", {}),
        (["report.html"], {"report.html": "art_v1"}, ("art_v1",), "art_v1", {}),
        (["report.html"], {}, (), "art_v1", {}),
    ],
    ids=["same_name", "follows_latest", "renamed", "model_mapped", "not_a_file_revision"],
)
async def test_a_same_name_deliverable_in_a_file_revision_is_the_next_version(
    outputs: list[str], revises: dict[str, str], targets: tuple[str, ...], latest: str, expected: dict[str, str]
) -> None:
    """Regression check N10: as a path is in coding harnesses, the target's file name is its identity within the dispatch."""
    from bowhead_skill.tools import _same_name_revisions

    assert await _same_name_revisions(_revise_ctx(_Lineage(latest=latest), targets), outputs, revises) == expected


async def test_python_exec_records_the_same_name_output_as_a_version_and_says_so(tmp_path: Path) -> None:
    from test_sandbox_workdir_contract import _FakeResult, _FixedRuntime, _flat_tool, _mk_skill

    from bowhead_skill import SkillCatalog

    _mk_skill(tmp_path, "python_sandbox", script="runner.py")
    cat = SkillCatalog(roots=[tmp_path])
    cat.refresh()
    saved = {"outputs": [{"artifact_id": "art_v2", "name": "report.html", "mime": "text/html", "size": 10}]}
    rt = _FixedRuntime(_FakeResult(result_json=saved))
    store = _Lineage()
    out = await _flat_tool(cat, rt).handler(
        {"code": "open('report.html','w').write('x')", "purpose": "deliverable", "deliverable_outputs": ["report.html"]},
        _revise_ctx(store),
    )
    assert rt.calls[0]["_revises"] == {"report.html": "art_v1"}
    assert out["recorded_as_new_version"] == [{"filename": "report.html", "artifact_id": "art_v2", "previous_version": "art_v1"}]
    # An upload cannot be versioned: the same-name output stays a new file and the call is not refused.
    rt2 = _FixedRuntime(_FakeResult(result_json={"outputs": [{"artifact_id": "art_new", "name": "input.html"}]}))
    out2 = await _flat_tool(cat, rt2).handler(
        {"code": "x", "purpose": "deliverable", "deliverable_outputs": ["input.html"]}, _revise_ctx(store, ("art_upload",))
    )
    assert "_revises" not in rt2.calls[0] and "recorded_as_new_version" not in out2 and "error_kind" not in out2


def test_only_saved_outputs_are_stated_as_versions() -> None:
    from bowhead_skill.tools import _same_name_version_facts

    assert _same_name_version_facts({"report.html": "art_v1"}, []) == {}
    facts = _same_name_version_facts({"report.html": "art_v1"}, [{"artifact_id": "art_v2", "name": "report.html"}])
    assert facts["recorded_as_new_version"] == [{"filename": "report.html", "artifact_id": "art_v2", "previous_version": "art_v1"}]


@pytest.mark.anyio
async def test_the_models_own_mapping_is_never_overridden() -> None:
    """The model mapped report.html to another target on purpose: the same-name rule adds nothing for that output."""
    from bowhead_skill.tools import _same_name_revisions

    store = _Lineage(names={"art_other": "summary.html"})
    ctx = _revise_ctx(store, ("art_v1", "art_other"))
    assert await _same_name_revisions(ctx, ["report.html"], {"report.html": "art_other"}) == {}
