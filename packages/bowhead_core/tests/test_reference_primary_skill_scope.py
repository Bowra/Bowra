"""An @Agent admitted without Skill material reads its own Skills like a delegated Agent.

`initial-primary.v1` is the composition of an @Agent with no selected Skill and no exact
Skill baseline (for example @Coach, whose manifest grants a built-in reference package it
reads on demand). Its own Tool and Skill scope applies; every other model run in the same
reference Turn still needs exact reference Skill access.
"""

from __future__ import annotations

import hashlib
import sqlite3
from types import SimpleNamespace

import pytest
from bowhead_core.skill_bridge import register_skill_tools
from bowhead_core.skill_reference_context import reference_turn_requires_context
from bowhead_core.tool_registry import ToolExecutionContext, ToolRegistry
from test_reference_primary_claim import _claim
from test_reference_primary_reader import accepted_case as _accepted_case_fixture
from test_reference_primary_reader import start_case as _start_case_fixture
from test_skill_read_access import receiver

from bowhead_skill import SkillCatalog, SkillRuntime

accepted_case = _accepted_case_fixture
start_case = _start_case_fixture
pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


def _primary_subagent(case: SimpleNamespace) -> str:
    with sqlite3.connect(case.fixture.db_path) as db:
        (execution_id,) = db.execute("SELECT execution_id FROM chat_reference_resolutions WHERE turn_id='t_reference'").fetchone()
    return "sub_" + hashlib.sha256(execution_id.encode()).hexdigest()[:20]


def _skill_registry(tmp_path) -> ToolRegistry:
    root = tmp_path / "skills" / "method"
    root.mkdir(parents=True)
    (root / "SKILL.md").write_text("---\nname: method\ndescription: the receiver's own method\n---\nOWN_METHOD_BODY\n")
    registry = ToolRegistry()
    register_skill_tools(registry, SkillCatalog(roots=[tmp_path / "skills"]), SkillRuntime())
    return registry


def _ctx(case: SimpleNamespace) -> ToolExecutionContext:
    return ToolExecutionContext(
        **case.scope.identity(), turn_id="t_reference", member_uid=case.fixture.member_id, datarc=None, session_store=case.store
    )


async def test_only_the_admitted_primary_skips_exact_reference_skill_access(accepted_case) -> None:
    case = accepted_case
    await _claim(case)
    primary = _primary_subagent(case)
    with sqlite3.connect(case.fixture.db_path) as db:
        (composition,) = db.execute("SELECT composition_json FROM chat_reference_resolutions WHERE turn_id='t_reference'").fetchone()
    assert '"runtime_policy":"initial-primary.v1"' in composition
    path = case.fixture.db_path
    assert await reference_turn_requires_context(path, turn_id="t_reference", subagent_id=primary) is False
    # Lead, a helper and an unknown run of the same Turn keep the exact-access requirement.
    assert await reference_turn_requires_context(path, turn_id="t_reference") is True
    assert await reference_turn_requires_context(path, turn_id="t_reference", subagent_id="sub_" + "0" * 20) is True


async def test_a_skill_material_composition_still_requires_exact_access(accepted_case) -> None:
    case = accepted_case
    await _claim(case)
    primary = _primary_subagent(case)
    with sqlite3.connect(case.fixture.db_path) as db:
        db.execute("DROP TRIGGER IF EXISTS trg_reference_composition_immutable")
        for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='chat_reference_resolutions'").fetchall():
            db.execute(f"DROP TRIGGER {name}")
        db.execute(
            "UPDATE chat_reference_resolutions SET composition_json=json_set(composition_json,'$.runtime_policy','initial-primary-skill-baseline.v1') "
            "WHERE turn_id='t_reference'"
        )
    assert await reference_turn_requires_context(case.fixture.db_path, turn_id="t_reference", subagent_id=primary) is True


async def test_the_admitted_primary_reads_its_own_skill_and_others_are_refused(accepted_case, tmp_path) -> None:
    case = accepted_case
    await _claim(case)
    registry = _skill_registry(tmp_path)
    with receiver(_primary_subagent(case)):
        ctx = _ctx(case)
        read = await registry.execute("skill_read", {"name": "method"}, ctx)
    assert read.error is None, read.error
    assert "OWN_METHOD_BODY" in str(read.result)
    assert ctx.reference_skill_required is False and ctx.reference_skill_access is None
    with receiver("sub_" + "0" * 20):
        other = _ctx(case)
        denied = await registry.execute("skill_read", {"name": "method"}, other)
    assert denied.error == "ToolPermissionError: reference_skill_unavailable"
    assert "OWN_METHOD_BODY" not in str(denied.result)


async def test_the_admitted_primary_stays_inside_its_own_skill_scope(accepted_case, tmp_path) -> None:
    from bowhead_skill.runtime_scope import ExactSkillRuntimeScope

    case = accepted_case
    await _claim(case)
    registry = _skill_registry(tmp_path)
    with receiver(_primary_subagent(case)):
        ctx = _ctx(case)
        ctx.skill_runtime_scope = ExactSkillRuntimeScope(read_names=("agent-authoring",), invoke_names=())
        denied = await registry.execute("skill_read", {"name": "method"}, ctx)
    assert denied.result["error_kind"] == "skill_unavailable" and denied.result["reason"] == "not_granted"
    assert "OWN_METHOD_BODY" not in str(denied.result)
