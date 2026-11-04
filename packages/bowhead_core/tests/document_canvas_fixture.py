"""Synthetic Canvas input fixtures shared by privacy and document tests."""

from types import SimpleNamespace

from bowhead_core.canvas_store import CanvasStore
from bowhead_core.documents.canvas_inputs import CanvasInputStore
from bowhead_core.documents.request import CanvasInputSelection
from bowhead_core.user_store import UserStore
from bowhead_gateway.document_canvas_inputs import DocumentCanvasInputs
from test_document_store import SCOPE


async def setup_canvas(ctx, state=None):
    store = CanvasStore(ctx.path)
    canvas = await store.ensure_canvas(session_id="s_doc", user_id=SCOPE.user_id, workspace_id=SCOPE.workspace_id)
    complete = b"period,revenue\nEast,87.25\nWest,120.5\nNorth,9\n"
    artifact = await ctx.artifacts.put_bytes(content=complete, filename="revenue.csv", mime="text/csv", created_by=SCOPE.user_id)
    payload = {
        "spec": {
            "kind": "bar",
            "title": "Revenue snapshot",
            "x": "period",
            "series": ["revenue"],
            "columns": [{"key": "period", "type": "text"}, {"key": "revenue", "type": "numeric", "unit": "USD"}],
            "rows": [{"period": "West", "revenue": 120.5}, {"period": "East", "revenue": 87.25}],
            "row_count": 3,
            "data_artifact_id": artifact.id,
            "top_n": 2,
            "views": [
                {
                    "id": "cost",
                    "kind": "chart",
                    "spec": {
                        "kind": "bar",
                        "title": "Costs",
                        "x": "period",
                        "series": ["cost"],
                        "columns": [{"key": "period", "type": "text"}, {"key": "cost", "type": "numeric", "unit": "USD"}],
                        "rows": [{"period": "East", "cost": 60}, {"period": "West", "cost": 72.3}],
                    },
                },
                {
                    "id": "quality",
                    "kind": "chart",
                    "spec": {
                        "kind": "line",
                        "title": "Quality",
                        "x": "period",
                        "series": ["ratio"],
                        "columns": [{"key": "period", "type": "text"}, {"key": "ratio", "type": "numeric", "unit": "%", "percent_scale": "fraction"}],
                        "rows": [{"period": "West", "ratio": 0}, {"period": "East", "ratio": None}],
                    },
                },
            ],
            "layout": {"type": "split", "children": [{"view": "cost"}, {"view": "quality"}]},
        }
    }
    for spec in [payload["spec"], *(view["spec"] for view in payload["spec"]["views"])]:
        spec["data_artifact_id"] = artifact.id
        spec.setdefault("row_count", len(spec["rows"]))
        for column in spec["columns"]:
            column["header"] = column["key"]
            if column["type"] == "text":
                column["type"] = "categorical"
    section = await store.append_section(canvas_id=canvas.id, kind="chart", payload=payload, by_turn_id="t_doc")
    text = await store.append_section(canvas_id=canvas.id, kind="markdown", payload={"content": "Synthetic business commentary"})
    state = state or SimpleNamespace(artifact_store=ctx.artifacts, user_store=UserStore(ctx.path))
    state.canvas_store, state.session_store = store, ctx.sessions
    state.artifact_fetch_secret = "synthetic-document-canvas-secret"
    return SimpleNamespace(
        store=store, canvas=canvas, section=section, text=text, artifact=artifact, complete=complete, adapter=DocumentCanvasInputs(state)
    )


async def admit_canvas(ctx, canvas):
    selection = CanvasInputSelection(section_ids=(canvas.section.id,))
    proposal = ctx.proposal.model_copy(update={"canvas": selection})
    admission = await canvas.adapter.prepare(scope=SCOPE, session_id="s_doc", selection=selection)
    receipt = await ctx.store.submit_from_turn(scope=SCOPE, turn_id="t_doc", command_id="canvas-command", proposal=proposal, _canvas=admission)
    return receipt, admission


async def freeze_canvas(ctx, canvas, job_id):
    await CanvasInputStore(ctx.artifacts).freeze(
        scope=SCOPE,
        job_id=job_id,
        materialize=lambda manifest: canvas.adapter.materialize(scope=SCOPE, manifest=manifest),
        authorize=lambda manifest: canvas.adapter.authorize(scope=SCOPE, manifest=manifest),
    )
