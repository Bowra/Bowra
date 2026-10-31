"""Consumer fixtures verified against the authorized delivery store and publication."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest
from test_document_delivery import _review
from test_document_store import SCOPE
from test_document_store import ctx as ctx
from test_document_store import migrated_db as migrated_db

pytestmark = pytest.mark.integration

FIXTURE = Path(__file__).resolve().parents[2] / "bowhead_frontend/__tests__/fixtures/document-draft-canvas.json"


async def test_same_name_evidence_without_canonical_history_keeps_file_origin(ctx):
    fixture = json.loads(FIXTURE.read_text())
    record = {key: fixture["review"]["draft"][key] for key in ("kind", "manifest", "preview")}
    record["manifest"]["changes"][0]["evidence"] = [
        {
            "source_name": "Document clarification history.json",
            "source_sha256": "a" * 64,
            "pointer": "/answers/0/answer",
            "value": "New title",
        }
    ]
    delivery, job_id, draft, _ = await _review(ctx, record=record)
    assert "clarification_evidence_sources" not in draft
    assert draft["draft"]["manifest"]["changes"][0]["evidence"] == record["manifest"]["changes"][0]["evidence"]
    assert (await delivery.get(scope=SCOPE, job_id=job_id))["draft_digest"] == draft["draft_digest"]


async def test_canvas_fixture_preserves_real_projection_and_published_association(ctx, monkeypatch):
    # This frozen consumer fixture represents revision admission disabled.
    # The enabled default is covered by the revision and authoring lifecycles.
    monkeypatch.setenv("FEATURE_TEMPLATE_DOCUMENT_REUSE", "0")
    fixture = json.loads(FIXTURE.read_text())
    expected = fixture["review"]
    # Only worker content is synthetic. Scope, storage, hashes and publication are real.
    record = {key: expected["draft"][key] for key in ("kind", "manifest", "preview")}
    delivery, job_id, actual, worker = await _review(ctx, record=record)
    encoded = json.dumps(actual["draft"], ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    assert actual["draft_digest"] == hashlib.sha256(encoded.encode()).hexdigest()
    assert actual["origin_turn_id"] == ctx.source.turn_id

    def normalize(projection):
        value = copy.deepcopy(projection)
        value["id"] = expected["id"]
        value["draft_digest"] = expected["draft_digest"]
        # The last execution change is a real timestamp; the fixture carries a stable one.
        assert isinstance(value.get("updated_at"), str)
        value["updated_at"] = expected["updated_at"]
        for name, file in value["draft"]["files"].items():
            assert file["sha256"] == hashlib.sha256(worker.files[name]).hexdigest()
            file["artifact_id"] = expected["draft"]["files"][name]["artifact_id"]
        return value

    assert normalize(actual) == fixture["review"]
    files = actual["draft"]["files"]
    assert await ctx.artifacts.get(files["candidate.pptx"]["artifact_id"]) is None
    receipt = await delivery.confirm(scope=SCOPE, job_id=job_id, command_id="canvas-fixture", draft_digest=actual["draft_digest"])
    assert normalize(receipt) == fixture["published"]
    assert normalize(await delivery.get(scope=SCOPE, job_id=job_id)) == fixture["published"]
    source = await ctx.artifacts.get(files["candidate.pptx"]["artifact_id"])
    pdf = await ctx.artifacts.get(files["candidate.pdf"]["artifact_id"])
    assert source.created_by_turn == pdf.created_by_turn == ctx.source.turn_id
    assert pdf.derived_from == source.id
    assert source.kind == pdf.kind == "deliverable"
