"""Strict outcome envelopes cannot manufacture settlement or upgrade old cards."""

import json

import pytest
from bowhead_core.ordinary_lead_checkpoint import OrdinaryLeadCheckpoint
from bowhead_core.ordinary_lead_continuation import OrdinaryContinuationUnavailable, canonical_json
from test_document_store import ctx as ctx
from test_document_store import migrated_db as migrated_db
from test_ordinary_lead_continuation import prepare

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.mark.parametrize(
    "boundary",
    [
        "roundtrip",
        "legacy",
        "missing",
        "unknown",
        "running",
        "error_prose",
        "success_failure_code",
        "failed_success",
        "missing_code",
        "extra",
        "changed_ok",
    ],
)
async def test_shared_outcome_requires_native_evidence(ctx, monkeypatch, boundary):
    _, _, _, checkpoint = await prepare(ctx, monkeypatch, helper_count=2)
    original = checkpoint.encode()
    value = json.loads(original)
    value["contract"] = "ordinary-lead-loop.v6"
    material = json.loads(value["material_json"])
    for helper in material["helpers"]:
        helper["outcome"] = {"status": "completed", "reason": "completed", "failure_code": None}
    outcome = material["helpers"][0]["outcome"]
    if boundary == "legacy":
        value["contract"] = "ordinary-lead-loop.v5"
    elif boundary == "missing":
        del material["helpers"][0]["outcome"]
    elif boundary == "unknown":
        outcome["reason"] = None
    elif boundary == "running":
        outcome["status"] = "running"
    elif boundary == "error_prose":
        outcome["reason"] = "The call appears complete"
    elif boundary == "success_failure_code":
        outcome["failure_code"] = "peer_empty_result"
    elif boundary == "failed_success":
        outcome.update(status="failed", reason="empty_answer", failure_code="peer_empty_result")
    elif boundary == "missing_code":
        del outcome["failure_code"]
    elif boundary == "extra":
        outcome["retry"] = True
    elif boundary == "changed_ok":
        invocations = json.loads(value["invocations_json"])
        invocations[0]["result"]["ok"] = False
        value["invocations_json"] = canonical_json(invocations)
    value["material_json"] = canonical_json(material)
    encoded = canonical_json(value).encode()
    if boundary == "roundtrip":
        assert OrdinaryLeadCheckpoint.decode(encoded).encode() == encoded
    else:
        with pytest.raises(OrdinaryContinuationUnavailable):
            OrdinaryLeadCheckpoint.decode(encoded)
    assert OrdinaryLeadCheckpoint.decode(original).encode() == original


@pytest.mark.parametrize("boundary", ["roundtrip", "legacy", "malformed", "missing_unknown", "false_success", "execution_after_unknown"])
async def test_uncertain_outcome_requires_new_envelope_and_explicit_status(ctx, monkeypatch, boundary):
    _, _, _, checkpoint = await prepare(ctx, monkeypatch, helper_count=2)
    value = json.loads(checkpoint.encode())
    value["contract"] = "ordinary-lead-loop.v6" if boundary == "legacy" else "ordinary-lead-loop.v7"
    material = json.loads(value["material_json"])
    material["helpers"][0]["outcome"] = {"status": "completed", "reason": "completed", "failure_code": None}
    material["helpers"][1]["outcome"] = {"status": "failed", "reason": "model_unknown", "failure_code": "subagent_execution_uncertain"}
    invocations = json.loads(value["invocations_json"])
    invocations[1]["result"].update(ok=False, execution_outcome="unknown")
    if boundary == "malformed":
        material["helpers"][0]["outcome"] = None
    elif boundary == "missing_unknown":
        del invocations[1]["result"]["execution_outcome"]
    elif boundary == "false_success":
        invocations[1]["result"]["ok"] = True
    elif boundary == "execution_after_unknown":
        material["helpers"][0]["outcome"] = dict(material["helpers"][1]["outcome"])
        invocations[0]["result"].update(ok=False, execution_outcome="unknown")
    value["material_json"] = canonical_json(material)
    value["invocations_json"] = canonical_json(invocations)
    encoded = canonical_json(value).encode()
    if boundary == "roundtrip":
        assert OrdinaryLeadCheckpoint.decode(encoded).encode() == encoded
    else:
        with pytest.raises(OrdinaryContinuationUnavailable):
            OrdinaryLeadCheckpoint.decode(encoded)
