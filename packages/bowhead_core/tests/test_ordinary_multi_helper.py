"""Strict ordered helper material and immutable encrypted-envelope upgrades."""

import json
import sqlite3
from copy import deepcopy
from importlib import import_module

import pytest
from bowhead_cli.migrate import TARGET_FILES
from bowhead_core._db import open_shared_ctx
from bowhead_core.chat_turn_source import chat_turn_user_source_projection
from bowhead_core.documents.routing import DocumentRoutingSnapshot
from bowhead_core.ordinary_lead_checkpoint import OrdinaryLeadCheckpoint
from bowhead_core.ordinary_lead_continuation import (
    OrdinaryContinuationUnavailable,
    canonical_json,
    content_hash,
    read_ordinary_card_in_tx,
    reopen_ordinary_checkpoint,
)
from test_chat_reference_storage import _historical_fixture, _upgrade
from test_document_store import ctx as ctx
from test_document_store import migrated_db as migrated_db
from test_ordinary_lead_continuation import prepare
from test_reference_model_context_migration import snapshot
from test_skill_invocation_migration import schema

pytestmark = pytest.mark.integration
MIGRATION = import_module("bowhead_core.migrations.versions.0160_ordinary_multi_helper")


@pytest.mark.parametrize("boundary", ["parallel_history", "split_pair", "extra_user", "past_end", "zero_size", "current_duplicate"])
async def test_retained_history_cannot_hide_current_calls_or_cross_call_pairs(ctx, monkeypatch, boundary):
    _, _, _, checkpoint = await prepare(ctx, monkeypatch, helper_count=2)
    value = json.loads(checkpoint.encode())
    current = json.loads(value["messages_json"])
    historical = deepcopy(current[:5])
    historical[0]["content"] = "Previous independent task"
    # Historical parallel calls are valid context. The current serial helper
    # contract and duplicate-call guard still apply to the executable suffix.
    historical[1]["tool_calls"].extend(historical[3]["tool_calls"])
    historical[1]["tool_calls"][1]["id"] = "previous-only"
    historical[4]["tool_call_id"] = "previous-only"
    del historical[3]
    value["history_sizes"] = [len(historical)]
    if boundary == "split_pair":
        value["history_sizes"] = [2, 2]
    elif boundary == "extra_user":
        historical[2] = deepcopy(historical[0])
    elif boundary == "past_end":
        value["history_sizes"] = [len(historical) + len(current)]
    elif boundary == "zero_size":
        value["history_sizes"] = [0, len(historical)]
    elif boundary == "current_duplicate":
        current[-1]["tool_calls"][0]["id"] = current[1]["tool_calls"][0]["id"]
    value["messages_json"] = canonical_json([*historical, *current])
    encoded = canonical_json(value).encode()
    if boundary != "parallel_history":
        with pytest.raises(OrdinaryContinuationUnavailable):
            OrdinaryLeadCheckpoint.decode(encoded)
        return
    restored = OrdinaryLeadCheckpoint.decode(encoded)
    assert restored.encode() == encoded
    assert len(restored.helper_materials()) == 2
    assert "previous-only" not in restored.retained_call_ids()
    assert restored.messages()[0].content == "Previous independent task"


@pytest.mark.parametrize("target", list(TARGET_FILES))
@pytest.mark.parametrize("revision", ["0160_ordinary_multi_helper", "0161_ordinary_shared_outcome"])
def test_multi_helper_upgrade_preserves_all_targets(tmp_path, target, revision):
    migration = import_module("bowhead_core.migrations.versions." + revision)
    path = tmp_path / TARGET_FILES[target]
    _upgrade(path, migration.down_revision, target)
    before, old_schema = snapshot(path), schema(path)
    _upgrade(path, migration.revision, target)
    _upgrade(path, migration.revision, target)
    assert snapshot(path) == before and schema(path) == old_schema
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (migration.revision,)
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize("contract", [f"ordinary-lead-loop.v{version}" for version in range(1, 6)])
async def test_upgrade_retains_encrypted_bytes_and_routing(ctx, tmp_path, monkeypatch, contract):
    routing = DocumentRoutingSnapshot(
        source_hash=content_hash(chat_turn_user_source_projection(ctx.source)),
        mode="template",
        evidence=ctx.source.user_text,
    )
    _, card, _, checkpoint = await prepare(
        ctx,
        monkeypatch,
        contract="ordinary-lead-loop.v4" if contract.endswith(".v5") else contract,
        document_routing=routing if contract.endswith((".v4", ".v5")) else None,
        helper_count=2 if contract.endswith(".v5") else 1,
    )
    encoded = checkpoint.encode()
    assert OrdinaryLeadCheckpoint.decode(encoded).encode() == encoded
    path = tmp_path / "old-helper.db"
    if contract.endswith(".v5"):
        # New writers do not run on an old head. V5 roundtrips its actual card.
        path = ctx.path
    else:
        _historical_fixture(ctx.path, path, MIGRATION.down_revision)
        before = snapshot(path)
        _upgrade(path, MIGRATION.revision)
        assert snapshot(path) == before
    async with open_shared_ctx(path) as db:
        await db.execute("BEGIN")
        record = await read_ordinary_card_in_tx(db, card_id=card.request_id)
    assert record.card.resume_context == card.resume_context
    restored = reopen_ordinary_checkpoint(record)
    assert restored.encode() == encoded and restored.document_routing == checkpoint.document_routing


@pytest.mark.parametrize(
    "change",
    [
        "empty",
        "single",
        "swapped",
        "extra",
        "duplicate_execution",
        "duplicate_dispatch",
        "duplicate_child",
        "missing_proof",
        "foreign_call",
        "changed_arguments",
        "failed_helper",
        "trace_collision",
        "parallel",
        "nested_duplicate",
        "nested_unknown",
        "aggregate_limit",
        "legacy_contract",
        "unknown_contract",
        "budget",
    ],
)
async def test_invalid_multi_helper_identity_cannot_be_reopened(ctx, monkeypatch, change):
    _, _, _, checkpoint = await prepare(ctx, monkeypatch, helper_count=2)
    value = json.loads(checkpoint.encode())
    material = json.loads(value["material_json"])
    invocations = json.loads(value["invocations_json"])
    messages = json.loads(value["messages_json"])
    helpers = material["helpers"]
    if change == "empty":
        helpers.clear()
    elif change == "single":
        helpers.pop()
    elif change == "swapped":
        helpers.reverse()
    elif change == "extra":
        helpers.append(dict(helpers[0]))
    elif change.startswith("duplicate_"):
        field = {"duplicate_execution": "execution_id", "duplicate_dispatch": "dispatch_key", "duplicate_child": "subagent_id"}[change]
        helpers[0][field] = helpers[1][field]
    elif change == "missing_proof":
        helpers[0].pop("binding_hash")
    elif change == "foreign_call":
        helpers[0]["dispatch_key"] = "call:foreign"
    elif change == "changed_arguments":
        invocations[0]["arguments"]["task"] = "Changed after execution"
    elif change == "failed_helper":
        invocations[0]["result"]["ok"] = False
    elif change == "trace_collision":
        invocations[0]["call_id"] = invocations[1]["call_id"]
    elif change == "parallel":
        messages[1]["tool_calls"].extend(messages[3]["tool_calls"])
        del messages[3]
    elif change.startswith("nested_") or change == "aggregate_limit":
        child = {**invocations[0], "tool_name": "subagent_return", "call_id": "return", "result": {"ok": True}, "nested": []}
        invocations[0]["nested"] = [child, dict(child)] if change == "nested_duplicate" else [child]
        if change == "nested_unknown":
            child["tool_name"] = "untracked_tool"
        elif change == "aggregate_limit":
            for index, invocation in enumerate(invocations):
                invocation["nested"] = [{**child, "call_id": f"return-{index}-{n}"} for n in range(32)]
    elif change == "legacy_contract":
        value["contract"] = "ordinary-lead-loop.v3"
    elif change == "unknown_contract":
        value["contract"] = "ordinary-lead-loop.unknown"
    else:
        value["next_iteration"] = value["max_iterations"]
    value.update(material_json=canonical_json(material), invocations_json=canonical_json(invocations), messages_json=canonical_json(messages))
    with pytest.raises(ValueError):
        OrdinaryLeadCheckpoint.decode(canonical_json(value).encode())


async def test_multi_helper_card_writer_rechecks_creation_gate(ctx, monkeypatch):
    from bowhead_core import ordinary_lead_continuation

    monkeypatch.setattr(ordinary_lead_continuation, "ordinary_multi_helper_question_enabled", lambda: False)
    with pytest.raises(OrdinaryContinuationUnavailable):
        await prepare(ctx, monkeypatch, helper_count=2)
    async with open_shared_ctx(ctx.path) as db:
        assert await (await db.execute("SELECT count(*) FROM clarification_requests")).fetchone() == (0,)


@pytest.mark.parametrize(
    "boundary",
    [
        "roundtrip",
        "missing_result",
        "duplicate_call",
        "foreign_result",
        "question_in_batch",
        "mixed_batch",
        "swapped_binding",
        "duplicate_binding",
        "foreign_binding",
        "unknown_parallel",
        "missing_inventory",
        "legacy_version",
    ],
)
async def test_parallel_checkpoint_preserves_exact_native_pairs(ctx, monkeypatch, boundary):
    _, _, _, original = await prepare(ctx, monkeypatch, helper_count=2)
    value = json.loads(original.encode())
    value["contract"] = "ordinary-lead-loop.v11"
    messages = json.loads(value["messages_json"])
    messages[1]["tool_calls"].extend(messages[3]["tool_calls"])
    del messages[3]
    material = json.loads(value["material_json"])
    material["input_context"] = None
    for helper in material["helpers"]:
        helper["outcome"] = {"status": "completed", "reason": "completed", "failure_code": None}
    context = json.loads(value["context_json"])
    context["reference_inventory"] = None
    if boundary == "missing_result":
        del messages[3]
    elif boundary == "duplicate_call":
        messages[1]["tool_calls"][1]["id"] = messages[1]["tool_calls"][0]["id"]
    elif boundary == "foreign_result":
        messages[3]["tool_call_id"] = "foreign-call"
    elif boundary == "question_in_batch":
        messages[1]["tool_calls"].extend(messages[-1]["tool_calls"])
        del messages[-1]
    elif boundary == "mixed_batch":
        messages[1]["tool_calls"][1]["name"] = "agent_message"
    elif boundary == "swapped_binding":
        material["helpers"].reverse()
    elif boundary == "duplicate_binding":
        material["helpers"][1]["dispatch_key"] = material["helpers"][0]["dispatch_key"]
    elif boundary == "foreign_binding":
        material["helpers"][1]["dispatch_key"] = "call:foreign"
    elif boundary == "unknown_parallel":
        material["helpers"][1]["outcome"] = {"status": "failed", "reason": "model_unknown", "failure_code": "model_outcome_unknown"}
        invocations = json.loads(value["invocations_json"])
        invocations[1]["result"].update(ok=False, execution_outcome="unknown")
        value["invocations_json"] = canonical_json(invocations)
    elif boundary == "missing_inventory":
        del context["reference_inventory"]
    elif boundary == "legacy_version":
        value["contract"] = "ordinary-lead-loop.v10"
    value.update(messages_json=canonical_json(messages), material_json=canonical_json(material), context_json=canonical_json(context))
    encoded = canonical_json(value).encode()
    if boundary != "roundtrip":
        with pytest.raises(OrdinaryContinuationUnavailable):
            OrdinaryLeadCheckpoint.decode(encoded)
        return
    reopened = OrdinaryLeadCheckpoint.decode(encoded)
    assert reopened.encode() == encoded
    assert len(reopened.messages()[1].tool_calls) == 2
    assert reopened.helper_materials() == material["helpers"]
