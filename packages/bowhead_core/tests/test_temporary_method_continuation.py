"""A task-selected method follows the original clarification and execution."""

import json
import sqlite3

import pytest
from test_reference_bi_continuation import continuation_case as continuation_case
from test_reference_bi_continuation import test_gateway_resumes_the_original_agent_without_replaying_its_first_call as original_resume
from test_skill_reference_claim import claimed_case as claimed_case
from test_skill_reference_claim import facts
from test_skill_reference_claim import receiver_case as receiver_case
from test_skill_reference_claim import start_case as start_case
from test_skill_reference_runtime import rows

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
    pytest.mark.parametrize("start_case", ["team"], indirect=True),
    pytest.mark.parametrize("receiver_case", ["admission_temporary_unused_invoke"], indirect=True),
    pytest.mark.parametrize("claimed_case", ["temporary"], indirect=True),
]


async def test_task_method_resumes_with_original_execution_and_receipts(continuation_case):
    case = continuation_case
    await original_resume(case)
    (composition,) = rows(case, "chat_reference_resolutions")
    assert json.loads(composition["composition_json"])["method_reference"] == "user-selected-method.v1"
    checks = rows(case, "chat_reference_extension_checks")
    assert sorted(row["created_generation"] for row in checks) == [1, 2]
    assert all(json.loads(row["policy_check_json"])["schema_version"] == 3 for row in checks)
    assert all(row["composition_digest"] == composition["composition_digest"] for row in checks)


@pytest.mark.parametrize("change", ["missing_marker", "unknown_marker", "other_source"])
async def test_task_method_composition_rejects_forged_authority(claimed_case, monkeypatch, change):
    import bowhead_core.skill_reference_execution as execution

    case = claimed_case
    before = facts(case)
    original = execution._put_primary_composition_in_connection

    async def corrupted(db, *, composition, generation):
        update = {"method_reference": None if change == "missing_marker" else "unknown.v1"}
        if change == "other_source":
            update = {"source_hash": "sha256:" + "f" * 64}
        return await original(db, composition=composition.model_copy(update=update), generation=generation)

    monkeypatch.setattr(execution, "_put_primary_composition_in_connection", corrupted)
    with pytest.raises((sqlite3.IntegrityError, ValueError)):
        await execution.claim_reference_skill_primary_for_worker(**case.args)
    assert facts(case) == before
