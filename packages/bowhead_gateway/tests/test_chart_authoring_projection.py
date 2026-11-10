"""Chart authoring uses the same live source authorization as Canvas."""

from types import SimpleNamespace

import jwt
import pytest
from bowhead_core.canvas_store import CanvasStore
from bowhead_core.chart.authoring_tools import ChartProjectionUnavailable
from bowhead_gateway.chart_authoring_projection import project_chart_for_authoring

TEST_JWT_SECRET = "synthetic-chart-projection-jwt-secret"


@pytest.fixture(autouse=True)
def viewer_signing_key(monkeypatch):
    monkeypatch.setenv("BOWHEAD_JWT_SECRET", TEST_JWT_SECRET)


def viewer_request(store, artifacts):
    token = jwt.encode({"sub": "user"}, TEST_JWT_SECRET, algorithm="HS256")
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(canvas_store=store, artifact_store=artifacts, artifact_fetch_secret="secret")),
        headers={"Authorization": "Bearer " + token},
        cookies={},
    )


async def refusal(request, section, session="session"):
    with pytest.raises(ChartProjectionUnavailable) as refused:
        await project_chart_for_authoring(request, session, section)
    return refused.value.reason


class ArtifactStore:
    def __init__(self):
        self.available = True

    async def get_many(self, ids, *, include_deleted=False):
        return [SimpleNamespace(id=item, status="ready", created_by="user", workspace_id=None) for item in ids] if self.available else []


async def test_current_projection_denies_missing_source_and_stale_section(tmp_path, monkeypatch):
    from bowhead_gateway.routes import canvas as canvas_routes

    async def refs(*args, **kwargs):
        return []

    monkeypatch.setattr(canvas_routes, "_build_source_refs_for_session", refs)
    monkeypatch.setattr(canvas_routes, "kb_file_access_for_request", lambda request: None)
    store = CanvasStore(tmp_path / "canvas.db")
    await store.initialize()
    canvas = await store.ensure_canvas(session_id="session", workspace_id="ws_1", user_id="user")
    section = await store.append_section(
        canvas_id=canvas.id,
        kind="chart",
        payload={
            "spec": {
                "kind": "line",
                "title": "Sales",
                "data_artifact_id": "art_123456789abc",
                "source_meta": {"id": "art_123456789abc", "artifact_id": "art_123456789abc"},
                "columns": [{"key": "amount", "header": "Amount", "type": "numeric"}],
                "rows": [{"amount": 42}],
            }
        },
    )
    artifacts = ArtifactStore()
    request = viewer_request(store, artifacts)
    authorized = await project_chart_for_authoring(request, "session", section)
    assert authorized and authorized["spec"]["rows"] == [{"amount": 42}]
    artifacts.available = False
    assert await refusal(request, section) == "source_access_denied"
    artifacts.available = True
    await store.patch_section(section_id=section.id, patch={"spec": section.payload["spec"]}, expected_rev=section.rev)
    assert await refusal(request, section) == "section_changed"


async def test_python_artifact_with_datarc_workspace_context_keeps_source_access_boundary(tmp_path, monkeypatch):
    from bowhead_gateway.routes import canvas as canvas_routes

    async def refs(*args, **kwargs):
        return []

    monkeypatch.setattr(canvas_routes, "_build_source_refs_for_session", refs)
    monkeypatch.setattr(canvas_routes, "kb_file_access_for_request", lambda request: None)
    store = CanvasStore(tmp_path / "canvas.db")
    await store.initialize()
    canvas = await store.ensure_canvas(session_id="session", workspace_id="ws_1", user_id="user")
    spec = {
        "kind": "line",
        "title": "Synthetic revenue",
        "data_artifact_id": "art_123456789abc",
        "source_meta": {"id": "art_123456789abc", "artifact_id": "art_123456789abc", "tool": "python_sandbox"},
        "columns": [{"key": "amount", "header": "Amount", "type": "numeric"}],
        "rows": [{"amount": 42}],
    }
    context = {"tool": "datarc", "context_id": "1", "context_name": "Example"}
    section = await store.append_section(canvas_id=canvas.id, kind="chart", payload={"spec": spec, "data_source": context})
    artifacts = ArtifactStore()
    request = viewer_request(store, artifacts)
    authorized = await project_chart_for_authoring(request, "session", section)
    assert authorized and authorized["spec"]["rows"] == [{"amount": 42}]
    assert authorized["data_source"]["access_state"] == "linked_unavailable"
    artifacts.available = False
    assert await refusal(request, section) == "source_access_denied"
    artifacts.available = True
    richer_context = await store.append_section(
        canvas_id=canvas.id, kind="chart", payload={"spec": spec, "data_source": {**context, "artifact_id": "art_other"}}
    )
    assert await refusal(request, richer_context) == "section_outside_report_closure"
    datarc_spec = {**spec, "source_meta": {**spec["source_meta"], "tool": "datarc"}}
    datarc = await store.append_section(canvas_id=canvas.id, kind="chart", payload={"spec": datarc_spec, "data_source": context})
    assert await refusal(request, datarc) == "section_outside_report_closure"
    with_other_source = await store.append_section(
        canvas_id=canvas.id, kind="chart", payload={"spec": spec, "data_source": context, "source_refs": ["unknown-source"]}
    )
    assert await refusal(request, with_other_source) == "section_outside_report_closure"
    mismatched_spec = {**spec, "data_artifact_id": "art_different"}
    mismatch = await store.append_section(canvas_id=canvas.id, kind="chart", payload={"spec": mismatched_spec, "data_source": context})
    assert await refusal(request, mismatch) == "provenance_unavailable"


async def test_calculated_chart_requires_both_original_and_derived_artifacts(tmp_path, monkeypatch):
    from bowhead_gateway.routes import canvas as canvas_routes

    async def refs(*args, **kwargs):
        return []

    monkeypatch.setattr(canvas_routes, "_build_source_refs_for_session", refs)
    monkeypatch.setattr(canvas_routes, "kb_file_access_for_request", lambda request: None)
    store = CanvasStore(tmp_path / "canvas.db")
    await store.initialize()
    canvas = await store.ensure_canvas(session_id="session", workspace_id="ws_1", user_id="user")
    original, derived = "art_aaaaaaaaaaaa", "art_bbbbbbbbbbbb"
    spec = {
        "kind": "table",
        "data_artifact_id": derived,
        "source_meta": {"id": original, "artifact_id": original, "tool": "python_sandbox"},
        "columns": [{"key": "amount", "header": "Amount", "type": "numeric"}],
        "rows": [{"amount": 90000}],
    }
    section = await store.append_section(
        canvas_id=canvas.id, kind="chart", payload={"spec": spec, "data_source": {"tool": "datarc", "context_id": "1"}}
    )

    class DerivedStore:
        available = {original, derived}
        parent = original
        producer = "chart_calculation"

        async def get_many(self, ids, *, include_deleted=False):
            return [
                SimpleNamespace(id=item, status="ready", source=self.producer, derived_from=self.parent, created_by="user", workspace_id=None)
                for item in ids
                if item in self.available
            ]

    artifacts = DerivedStore()
    request = viewer_request(store, artifacts)
    assert (await project_chart_for_authoring(request, "session", section))["spec"]["rows"] == [{"amount": 90000}]
    artifacts.available = {original}
    assert await refusal(request, section) == "provenance_unavailable"
    artifacts.available = {derived}
    assert await refusal(request, section) == "source_access_denied"
    artifacts.available = {original, derived}
    artifacts.parent = "art_unrelated"
    assert await refusal(request, section) == "provenance_unavailable"
    artifacts.parent = original
    artifacts.producer = "python_sandbox"
    assert await refusal(request, section) == "provenance_unavailable"
    artifacts.producer = "chart_calculation"
    extra = await store.append_section(
        canvas_id=canvas.id,
        kind="chart",
        payload={"spec": spec, "data_source": {"tool": "datarc", "context_id": "1"}, "source_refs": ["unknown-source"]},
    )
    assert await refusal(request, extra) == "section_outside_report_closure"
