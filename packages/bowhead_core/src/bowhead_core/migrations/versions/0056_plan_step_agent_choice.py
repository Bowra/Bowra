"""Persist immutable planner Agent choices beside Plan steps.

Revision ID: 0056_plan_step_agent_choice
Revises: 0055_resource_file_cleanup_receipt
Create Date: 2026-09-08
"""

from __future__ import annotations

from alembic import context

revision = "0056_plan_step_agent_choice"
down_revision = "0055_resource_file_cleanup_receipt"
branch_labels = None
depends_on = None


_TRIGGERS: tuple[tuple[str, str], ...] = (
    (
        "trg_plan_step_agent_choice_insert_guard",
        """
        CREATE TRIGGER trg_plan_step_agent_choice_insert_guard
        BEFORE INSERT ON plan_steps
        WHEN NEW.agent_choice_json IS NOT NULL AND CASE
          WHEN json_valid(NEW.agent_choice_json)=0 THEN 1
          ELSE
            json_type(NEW.agent_choice_json) IS NOT 'object'
            OR (SELECT count(*) FROM json_each(NEW.agent_choice_json)) != 10
            OR EXISTS (
              SELECT 1 FROM json_each(NEW.agent_choice_json)
              WHERE key NOT IN (
                'schema_version','role_id','candidate_id','reason_code','agent_id',
                'runtime_profile_name','assignment_id','revision_ref','release_id',
                'behavior_fingerprint'
              )
            )
            OR json_type(NEW.agent_choice_json,'$.schema_version') IS NOT 'integer'
            OR json_extract(NEW.agent_choice_json,'$.schema_version') != 1
            OR json_type(NEW.agent_choice_json,'$.role_id') IS NOT 'text'
            OR json_extract(NEW.agent_choice_json,'$.role_id') NOT IN (
              'data','compute','deck','knowledge','canvas_edit','image'
            )
            OR json_type(NEW.agent_choice_json,'$.candidate_id') IS NOT 'text'
            OR length(json_extract(NEW.agent_choice_json,'$.candidate_id')) != 36
            OR substr(json_extract(NEW.agent_choice_json,'$.candidate_id'),1,4) != 'lrc_'
            OR substr(json_extract(NEW.agent_choice_json,'$.candidate_id'),5) GLOB '*[^0-9a-f]*'
            OR json_type(NEW.agent_choice_json,'$.reason_code') IS NOT 'text'
            OR length(json_extract(NEW.agent_choice_json,'$.reason_code')) NOT BETWEEN 1 AND 256
            OR substr(json_extract(NEW.agent_choice_json,'$.reason_code'),1,1) NOT GLOB '[a-z]'
            OR substr(json_extract(NEW.agent_choice_json,'$.reason_code'),2) GLOB '*[^a-z0-9_.:@/-]*'
            OR json_type(NEW.agent_choice_json,'$.agent_id') IS NOT 'text'
            OR length(json_extract(NEW.agent_choice_json,'$.agent_id')) NOT BETWEEN 1 AND 200
            OR json_type(NEW.agent_choice_json,'$.runtime_profile_name') IS NOT 'text'
            OR length(json_extract(NEW.agent_choice_json,'$.runtime_profile_name')) NOT BETWEEN 1 AND 200
            OR json_extract(NEW.agent_choice_json,'$.runtime_profile_name') != NEW.agent_name
            OR COALESCE(json_type(NEW.agent_choice_json,'$.assignment_id'),'missing') NOT IN ('null','text')
            OR (
              json_type(NEW.agent_choice_json,'$.assignment_id')='text'
              AND substr(json_extract(NEW.agent_choice_json,'$.assignment_id'),1,4) != 'asa_'
            )
            OR json_type(NEW.agent_choice_json,'$.revision_ref') IS NOT 'text'
            OR substr(json_extract(NEW.agent_choice_json,'$.revision_ref'),1,4) != 'arv_'
            OR COALESCE(json_type(NEW.agent_choice_json,'$.release_id'),'missing') NOT IN ('null','text')
            OR (
              json_type(NEW.agent_choice_json,'$.release_id')='text'
              AND substr(json_extract(NEW.agent_choice_json,'$.release_id'),1,4) != 'arl_'
            )
            OR (
              json_type(NEW.agent_choice_json,'$.assignment_id')='null'
            ) != (
              json_type(NEW.agent_choice_json,'$.release_id')='null'
            )
            OR json_type(NEW.agent_choice_json,'$.behavior_fingerprint') IS NOT 'text'
            OR length(json_extract(NEW.agent_choice_json,'$.behavior_fingerprint')) != 71
            OR substr(json_extract(NEW.agent_choice_json,'$.behavior_fingerprint'),1,7) != 'sha256:'
            OR substr(json_extract(NEW.agent_choice_json,'$.behavior_fingerprint'),8) GLOB '*[^0-9a-f]*'
        END
        BEGIN
          SELECT RAISE(ABORT,'plan_agent_choice_invalid');
        END
        """,
    ),
    (
        "trg_plan_step_agent_choice_immutable",
        """
        CREATE TRIGGER trg_plan_step_agent_choice_immutable
        BEFORE UPDATE OF agent_choice_json,agent_name ON plan_steps
        WHEN OLD.agent_choice_json IS NOT NEW.agent_choice_json
          OR (OLD.agent_choice_json IS NOT NULL AND OLD.agent_name IS NOT NEW.agent_name)
        BEGIN
          SELECT RAISE(ABORT,'plan_agent_choice_immutable');
        END
        """,
    ),
)


def _normalized_sql(value: str) -> str:
    return "".join(value.lower().split())


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    connection = context.get_bind()
    columns = {
        str(row[1]): row
        for row in connection.exec_driver_sql(
            "PRAGMA table_info(plan_steps)",
        ).fetchall()
    }
    current = columns.get("agent_choice_json")
    if current is None:
        connection.exec_driver_sql(
            "ALTER TABLE plan_steps ADD COLUMN agent_choice_json TEXT",
        )
    elif str(current[2]).upper() != "TEXT" or int(current[3]) != 0:
        raise RuntimeError("Plan step Agent choice column drift")

    for name, ddl in _TRIGGERS:
        existing = connection.exec_driver_sql(
            "SELECT sql FROM sqlite_schema WHERE type='trigger' AND name=?",
            (name,),
        ).scalar()
        if existing is None:
            connection.exec_driver_sql(ddl)
        elif _normalized_sql(str(existing)) != _normalized_sql(ddl):
            raise RuntimeError(f"Plan step Agent choice schema drift: {name}")


def downgrade() -> None:
    raise NotImplementedError("Forward-only; retain exact Plan Agent choices.")
