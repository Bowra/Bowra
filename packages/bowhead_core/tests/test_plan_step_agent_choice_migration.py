"""M3-13E durable Plan Agent-choice migration contract."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from importlib import import_module
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from bowhead_core.agent_role_candidates import ExactAgentRoleChoice
from bowhead_core.plan_store import PlanStore, new_step

from bowhead_core import migrations

pytestmark = pytest.mark.integration
MIGRATION = import_module(
    "bowhead_core.migrations.versions.0056_plan_step_agent_choice",
)


def _config(path: Path, target: str = "core") -> Config:
    config = Config()
    config.set_main_option("script_location", str(Path(migrations.__file__).resolve().parent))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    config.attributes["bowhead_target"] = target
    return config


def _choice() -> ExactAgentRoleChoice:
    return ExactAgentRoleChoice(
        role_id="image",
        candidate_id="lrc_" + "a" * 32,
        reason_code="task_match",
        agent_id="system:marketing-analyst",
        runtime_profile_name="marketing-analyst",
        assignment_id=None,
        revision_ref="arv_system_image",
        release_id=None,
        behavior_fingerprint="sha256:" + "b" * 64,
    )


def _insert_raw_choice(
    db: sqlite3.Connection,
    *,
    plan_id: str,
    step_id: str,
    choice: dict[str, object],
) -> None:
    db.execute(
        "INSERT INTO plan_steps(id,plan_id,seq,level,agent_name,objective,deliverable,depends_on,"
        "needs_corpus,needs_web,status,origin,included,success_criteria,agent_choice_json,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            step_id,
            plan_id,
            1,
            0,
            "marketing-analyst",
            "x",
            "",
            "[]",
            0,
            0,
            "pending",
            "initial",
            1,
            "[]",
            json.dumps(choice),
            "2026-09-08T00:00:00+00:00",
            "2026-09-08T00:00:00+00:00",
        ),
    )


def test_0056_upgrade_is_repeatable_and_at_head(tmp_path: Path) -> None:
    path = tmp_path / "core.db"
    config = _config(path)
    command.upgrade(config, MIGRATION.down_revision)
    command.upgrade(config, "head")
    command.upgrade(config, "head")
    command.stamp(config, MIGRATION.down_revision)
    # Replay this revision only, then restore ledger head; later historical revisions (e.g. 0063 table creation) need not rerun on upgraded DB.
    command.upgrade(config, MIGRATION.revision)
    with sqlite3.connect(path) as db:
        # Confirm replay reached this revision; checking after stamp head would be meaningless tautology.
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (MIGRATION.revision,)
    command.stamp(config, "head")

    with sqlite3.connect(path) as db:
        columns = {row[1]: row for row in db.execute("PRAGMA table_info(plan_steps)")}
        assert columns["agent_choice_json"][2].upper() == "TEXT"
        assert columns["agent_choice_json"][3] == 0


@pytest.mark.parametrize("target", ["files", "doc_types", "extracted_records", "matrix"])
def test_0056_is_noop_for_non_core_targets(tmp_path: Path, target: str) -> None:
    path = tmp_path / f"{target}.db"
    command.upgrade(_config(path, target), "head")
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT 1 FROM sqlite_schema WHERE name='trg_plan_step_agent_choice_insert_guard'").fetchone() is None


@pytest.mark.parametrize("atomic", [False, True])
def test_exact_choice_round_trips_and_is_immutable(tmp_path: Path, atomic: bool) -> None:
    path = tmp_path / "core.db"
    command.upgrade(_config(path), "head")

    async def exercise() -> tuple[str, str]:
        store = PlanStore(str(path))
        step = new_step(
            plan_id="",
            seq=0,
            agent_name="marketing-analyst",
            objective="Create a campaign image",
            agent_choice=_choice(),
        )
        plan = await store.create_plan(
            session_id="session_plan_choice",
            workspace_id="workspace_plan_choice",
            user_id="user_plan_choice",
            title="图片计划",
            initial_steps=[step] if atomic else None,
        )
        if not atomic:
            step.plan_id = plan.id
            await store.add_steps([step])
        loaded = (await store.list_steps(plan.id))[0]
        assert loaded.agent_choice == _choice() and loaded.plan_id == plan.id
        return plan.id, step.id

    plan_id, step_id = asyncio.run(exercise())
    with sqlite3.connect(path) as db:
        with pytest.raises(sqlite3.IntegrityError, match="plan_agent_choice_immutable"):
            db.execute(
                "UPDATE plan_steps SET agent_choice_json=NULL WHERE id=?",
                (step_id,),
            )
        malformed = json.loads(_choice().canonical_json())
        malformed["runtime_profile_name"] = "other-agent"
        with pytest.raises(sqlite3.IntegrityError, match="plan_agent_choice_invalid"):
            _insert_raw_choice(db, plan_id=plan_id, step_id="ps_bad_profile", choice=malformed)

        bad_candidate = json.loads(_choice().canonical_json())
        bad_candidate["candidate_id"] = "lrc_" + "g" * 32
        with pytest.raises(sqlite3.IntegrityError, match="plan_agent_choice_invalid"):
            _insert_raw_choice(db, plan_id=plan_id, step_id="ps_bad_candidate", choice=bad_candidate)

        bad_reason = json.loads(_choice().canonical_json())
        bad_reason["reason_code"] = "Task Match"
        with pytest.raises(sqlite3.IntegrityError, match="plan_agent_choice_invalid"):
            _insert_raw_choice(db, plan_id=plan_id, step_id="ps_bad_reason", choice=bad_reason)

        missing_key = json.loads(_choice().canonical_json())
        del missing_key["assignment_id"]
        with pytest.raises(sqlite3.IntegrityError, match="plan_agent_choice_invalid"):
            _insert_raw_choice(db, plan_id=plan_id, step_id="ps_missing_key", choice=missing_key)
