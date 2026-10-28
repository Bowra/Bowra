"""Integrate exact MCP reads with Lead delegation and its existing safe resumes."""

import re
from importlib import import_module

from alembic import context

revision = "0128_reference_lead_mcp"
down_revision = "0127_access_request_legacy_origin"
branch_labels = None
depends_on = None

CALLS = import_module("bowhead_core.migrations.versions.0090_reference_mcp_calls")
MCP = import_module("bowhead_core.migrations.versions.0089_reference_mcp_composition")
LEAD = import_module("bowhead_core.migrations.versions.0125_reference_lead_capability")
PAUSE = import_module("bowhead_core.migrations.versions.0112_reference_lead_postdispatch")
LINEAGE = import_module("bowhead_core.migrations.versions.0106_reference_queue_lineage")

if any(source.count(needle) != 1 for source, needle in (
    (LEAD.PLAN_DDL, "BEGIN SELECT RAISE"),
    (MCP.DDL, "THEN RAISE(ABORT,'reference MCP composition mismatch')"),
    (PAUSE.PREVIOUS.LEAD_MODEL_FENCE, " AND v.subagent_id=NEW.subagent_id\n"),
    (CALLS.DDL[3], "r.created_generation=NEW.created_generation"),
    (CALLS.DDL[3], "d.lease_kind='turn'"),
    (CALLS.DDL[3], "d.dispatch_kind='start' AND d.parent_turn_id IS NULL"),
    (CALLS.DDL[0], "CREATE TABLE chat_reference_mcp_calls"),
    (CALLS.DDL[0], "CHECK(created_generation=1)"),
)):
    raise RuntimeError("reference Lead MCP migration source drift")

MCP_PLAN = """COALESCE(json_extract(NEW.plan_json,'$.orchestration_contract')='lead-delegation.v1'
 AND (SELECT COUNT(*) FROM json_each(NEW.plan_json,'$.admissions') a
      WHERE json_extract(a.value,'$.resource.kind')='agent'
       AND json_extract(a.value,'$.resource.source_kind')='team' AND json_extract(a.value,'$.operation')='delegate')=1
 AND (SELECT COUNT(*) FROM json_each(NEW.plan_json,'$.admissions') a
      WHERE json_extract(a.value,'$.resource.kind')='agent')=1
 AND (SELECT COUNT(*) FROM json_each(NEW.plan_json,'$.admissions') a
      WHERE json_extract(a.value,'$.resource.kind')='mcp_source') BETWEEN 0 AND 3
 AND (SELECT COUNT(*) FROM json_each(NEW.plan_json,'$.admissions') a
      WHERE json_extract(a.value,'$.resource.kind')='mcp_tool') BETWEEN 0 AND 3
 AND json_array_length(NEW.plan_json,'$.admissions') BETWEEN 2 AND 7
 AND NOT EXISTS (SELECT 1 FROM json_each(NEW.plan_json,'$.admissions') a
      WHERE COALESCE(json_extract(a.value,'$.resource.kind') IN ('agent','mcp_source','mcp_tool'),0)=0),0)"""
PLAN_DDL = LEAD.PLAN_DDL.replace("BEGIN SELECT RAISE", f"AND NOT {MCP_PLAN}\nBEGIN SELECT RAISE")
GUARD_NAME = "trg_reference_lead_mcp_plan"
GUARD = f"""
CREATE TRIGGER {GUARD_NAME} BEFORE INSERT ON chat_reference_plans
WHEN json_extract(NEW.plan_json,'$.orchestration_contract')='lead-delegation.v1'
 AND EXISTS (SELECT 1 FROM json_each(NEW.plan_json,'$.admissions') a
   WHERE json_extract(a.value,'$.resource.kind') IN ('mcp_source','mcp_tool'))
 AND NOT {MCP_PLAN}
BEGIN SELECT RAISE(ABORT,'reference Lead MCP plan mismatch'); END
"""
DELEGATION = """AND NOT EXISTS (
 SELECT 1 FROM chat_reference_plans p
 JOIN chat_reference_resolutions original ON original.turn_id=p.turn_id AND original.resolution_seq=1
 WHERE p.turn_id=NEW.turn_id AND p.source_turn_id=NEW.source_turn_id
 AND json_extract(p.plan_json,'$.orchestration_contract')='lead-delegation.v1'
 AND json_extract(original.composition_json,'$.orchestration_contract')='lead-delegation.v1'
 AND json_extract(original.composition_json,'$.runtime_policy')='initial-primary-mcp-read.v1'
 AND NEW.resolution_seq=2 AND NEW.idempotency_key='reference:primary:delegation'
 AND NEW.execution_id=original.execution_id
 AND json_extract(NEW.composition_json,'$.runtime_policy')='lead-delegation.v1'
 AND json_extract(NEW.composition_json,'$.source_composition_digest')=original.composition_digest
) """
COMPOSITION_DDL = MCP.DDL.replace("THEN RAISE(ABORT,'reference MCP composition mismatch')", DELEGATION + "THEN RAISE(ABORT,'reference MCP composition mismatch')")

# Retain original generation-one interpretation. Later calls need the exact
# resolved card and delegation/attempt fences already used by model receipts.
PREDISPATCH = PAUSE.PREVIOUS.LEAD_MODEL_FENCE.replace(" AND v.subagent_id=NEW.subagent_id\n", "\n")
POSTDISPATCH = PAUSE._postdispatch_fence("NEW.created_generation", "started.subagent_id")
CALL_SCOPE = CALLS.DDL[3].replace(
    "r.created_generation=NEW.created_generation",
    f"(r.created_generation=NEW.created_generation OR ({PREDISPATCH}) OR ({POSTDISPATCH}))",
).replace(
    "d.lease_kind='turn'", "d.lease_kind=CASE WHEN NEW.created_generation=1 THEN 'turn' ELSE 'resume' END",
).replace(
    "d.dispatch_kind='start' AND d.parent_turn_id IS NULL",
    LINEAGE.NEW_ORIGIN,
)
TABLE_DDL = CALLS.DDL[0].replace("CREATE TABLE chat_reference_mcp_calls", "CREATE TABLE chat_reference_mcp_calls_next").replace(
    "CHECK(created_generation=1)", "CHECK(created_generation>=1)",
)
CHANGES = {
    "trg_reference_lead_plan": (LEAD.PLAN_DDL, PLAN_DDL),
    MCP.TRIGGER: (MCP.DDL, COMPOSITION_DDL),
}


def upgrade():
    if context.config.attributes["bowhead_target"] != "core":
        return
    db = context.get_bind()
    expected = {name: old for name, (old, _) in CHANGES.items()}
    for sql in CALLS.DDL:
        match = re.search(r"CREATE (?:TABLE|(?:UNIQUE )?INDEX|TRIGGER) (\w+)", sql)
        expected[match.group(1)] = sql
    for name, sql in expected.items():
        row = db.exec_driver_sql("SELECT sql FROM sqlite_master WHERE name=?", (name,)).fetchone()
        if row is None or CALLS._normalized(row[0]) != CALLS._normalized(sql):
            raise RuntimeError(f"reference Lead MCP schema drift: {name}")
    if db.exec_driver_sql("SELECT 1 FROM sqlite_master WHERE name IN (?,?)", (GUARD_NAME, "chat_reference_mcp_calls_next")).fetchone():
        raise RuntimeError("reference Lead MCP schema drift")
    db.exec_driver_sql("SAVEPOINT reference_lead_mcp")
    try:
        for name in CALLS.TRIGGERS:
            db.exec_driver_sql(f"DROP TRIGGER {name}")
        db.exec_driver_sql(TABLE_DDL)
        db.exec_driver_sql("INSERT INTO chat_reference_mcp_calls_next SELECT * FROM chat_reference_mcp_calls")
        db.exec_driver_sql("DROP TABLE chat_reference_mcp_calls")
        db.exec_driver_sql("ALTER TABLE chat_reference_mcp_calls_next RENAME TO chat_reference_mcp_calls")
        for sql in CALLS.DDL[1:]:
            db.exec_driver_sql(CALL_SCOPE if sql == CALLS.DDL[3] else sql)
        for name, (_, replacement) in CHANGES.items():
            db.exec_driver_sql(f"DROP TRIGGER {name}")
            db.exec_driver_sql(replacement)
        db.exec_driver_sql(GUARD)
        db.exec_driver_sql("RELEASE SAVEPOINT reference_lead_mcp")
    except BaseException:
        db.exec_driver_sql("ROLLBACK TO SAVEPOINT reference_lead_mcp")
        db.exec_driver_sql("RELEASE SAVEPOINT reference_lead_mcp")
        raise


def downgrade():
    raise NotImplementedError("Forward-only; restore the matching database snapshot to roll back.")
