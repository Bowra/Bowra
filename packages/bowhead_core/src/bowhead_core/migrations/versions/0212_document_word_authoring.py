"""Extend original private-document guards for exact Word drafts."""

from importlib import import_module

from alembic import context

revision = "0212_document_word_authoring"
down_revision = "0211_bowra_connect"
branch_labels = None
depends_on = None

AUTHORING = import_module("bowhead_core.migrations.versions.0201_document_authoring")
DRAFTS = import_module("bowhead_core.migrations.versions.0130_document_drafts")
REVISIONS = import_module("bowhead_core.migrations.versions.0148_document_revisions")
REFERENCE = import_module("bowhead_core.migrations.versions.0209_reference_question_origin")
PPTX = "pptx-script-worker-v1"
WORD = "docx-script-worker-v1"
OLD_CHECK = f"CHECK(profile IN ('ordinary-pptx-worker-v1','{PPTX}'))"
NEW_CHECK = f"CHECK(profile IN ('ordinary-pptx-worker-v1','{PPTX}','{WORD}'))"
EXPECTED_DISPATCH = AUTHORING.EXPECTED["document_dispatches"].replace("CHECK(profile='ordinary-pptx-worker-v1')", OLD_CHECK)


def _trigger(module, name):
    return next(sql for sql in module.DDL if sql.startswith(f"CREATE TRIGGER {name} "))


def _source(job):
    return f"(CASE (SELECT profile FROM document_dispatches WHERE job_id={job}) WHEN '{WORD}' THEN 'candidate.docx' ELSE 'candidate.pptx' END)"


EXPECTED = {name: _trigger(DRAFTS, name) for name in (
    "trg_document_dispatch_admission", "trg_document_confirmation_admission", "trg_document_draft_artifact_identity",
)}
REVISION_NAME = next(sql.split()[2] for sql in REVISIONS.DDL if "d.name='candidate.pptx'" in sql)
EXPECTED[REVISION_NAME] = _trigger(REVISIONS, REVISION_NAME)
EXPECTED["trg_reference_primary_completion"] = REFERENCE.DDL["trg_reference_primary_completion"]
for table in ("document_model_calls", "document_physical_receipt_links"):
    EXPECTED[f"trg_{table}_authoring_call"] = f"""CREATE TRIGGER trg_{table}_authoring_call BEFORE INSERT ON {table}
      WHEN (NEW.call_no>2 OR EXISTS(SELECT 1 FROM document_dispatches WHERE job_id=NEW.job_id AND profile='{PPTX}'))
      AND NOT EXISTS(SELECT 1 FROM document_dispatches d
        JOIN document_turn_dispatches t ON t.job_id=d.job_id WHERE d.job_id=NEW.job_id
          AND d.profile='{PPTX}' AND json_extract(t.runtime_snapshot_json,'$.schema_version')=2
          AND json_extract(t.runtime_snapshot_json,'$.planner_profile')='{PPTX}')
      BEGIN SELECT RAISE(ABORT,'document authoring call unavailable'); END"""
EXPECTED["trg_document_authoring_dispatch"] = f"""CREATE TRIGGER trg_document_authoring_dispatch BEFORE INSERT ON document_turn_dispatches
  WHEN NOT EXISTS(SELECT 1 FROM document_dispatches d WHERE d.job_id=NEW.job_id AND (
    (d.profile='{PPTX}' AND json_extract(NEW.runtime_snapshot_json,'$.schema_version')=2
      AND json_extract(NEW.runtime_snapshot_json,'$.planner_profile')='{PPTX}') OR
    (d.profile='ordinary-pptx-worker-v1' AND json_extract(NEW.runtime_snapshot_json,'$.schema_version')=1
      AND json_extract(NEW.runtime_snapshot_json,'$.planner_profile') IN ('pptx-grounded-plan-v4','pptx-grounded-plan-v5'))))
  BEGIN SELECT RAISE(ABORT,'document runtime profile mismatch'); END"""

DDL = dict(EXPECTED)
DDL["trg_document_dispatch_admission"] = (
    DDL["trg_document_dispatch_admission"]
    .replace("c.capability_version=1", f"c.capability_version=CASE WHEN NEW.profile='{WORD}' THEN 2 ELSE 1 END")
    .replace("c.implementation_version=1", f"c.implementation_version=CASE WHEN NEW.profile='{WORD}' THEN 2 ELSE 1 END")
    .replace("c.output_contract='document.admission.v1'", f"c.output_contract=CASE WHEN NEW.profile='{WORD}' THEN 'document.admission.v2' ELSE 'document.admission.v1' END")
)
word_descriptor = f"""NOT EXISTS(SELECT 1 FROM document_dispatches d JOIN document_executions e ON e.job_id=d.job_id
 JOIN document_jobs j ON j.id=d.job_id WHERE d.job_id=NEW.job_id AND d.profile='{WORD}' AND NOT coalesce(
 json_extract(e.draft_json,'$.profile')='{WORD}' AND json_extract(e.draft_json,'$.manifest.schema_version')=3
 AND json_extract(e.draft_json,'$.descriptor.format')='docx'
 AND json_extract(e.draft_json,'$.descriptor.source_name')='candidate.docx'
 AND json_extract(e.draft_json,'$.descriptor.pdf_name')='candidate.pdf'
 AND json_extract(e.draft_json,'$.descriptor.published_outputs')=json_extract(j.request_json,'$.proposal.requested_outputs'),0))"""
DDL["trg_document_confirmation_admission"] = (
    DDL["trg_document_confirmation_admission"]
    .replace("'candidate.pptx'", _source("NEW.job_id"))
    .replace("WHEN NOT EXISTS(", f"WHEN NOT ({word_descriptor}) OR NOT EXISTS(", 1)
)
DDL["trg_document_draft_artifact_identity"] = (
    DDL["trg_document_draft_artifact_identity"]
    .replace("'candidate.pptx'", _source("f.job_id"))
    .replace("e.state='published' AND", f"""e.state='published'
      AND (coalesce((SELECT profile FROM document_dispatches WHERE job_id=f.job_id),'ordinary-pptx-worker-v1')<>'{WORD}'
        OR f.name='candidate.docx' OR json_extract(e.draft_json,'$.descriptor.published_outputs[1]')='pdf') AND""")
)
DDL[REVISION_NAME] = DDL[REVISION_NAME].replace("'candidate.pptx'", _source("p.id"))
DDL["trg_reference_primary_completion"] = DDL["trg_reference_primary_completion"].replace(
    "capability.output_contract='document.admission.v1'",
    f"capability.output_contract=CASE WHEN admission.profile='{WORD}' THEN 'document.admission.v2' ELSE 'document.admission.v1' END",
)
for table in ("document_model_calls", "document_physical_receipt_links"):
    name = f"trg_{table}_authoring_call"
    DDL[name] = EXPECTED[name].replace(f"profile='{PPTX}'", f"profile IN ('{PPTX}','{WORD}')", 1).replace(
        f"d.profile='{PPTX}' AND json_extract(t.runtime_snapshot_json,'$.schema_version')=2\n          AND json_extract(t.runtime_snapshot_json,'$.planner_profile')='{PPTX}'",
        f"""d.profile IN ('{PPTX}','{WORD}')
          AND json_extract(t.runtime_snapshot_json,'$.schema_version')=CASE WHEN d.profile='{WORD}' THEN 3 ELSE 2 END
          AND json_extract(t.runtime_snapshot_json,'$.planner_profile')=d.profile""",
    )
DDL["trg_document_authoring_dispatch"] = EXPECTED["trg_document_authoring_dispatch"].replace(
    f"(d.profile='{PPTX}' AND", f"""(d.profile='{WORD}' AND json_extract(NEW.runtime_snapshot_json,'$.schema_version')=3
      AND json_extract(NEW.runtime_snapshot_json,'$.planner_profile')='{WORD}') OR (d.profile='{PPTX}' AND""", 1,
)


def _rebuild_dispatch(db):
    actual = db.exec_driver_sql("SELECT sql FROM sqlite_schema WHERE type='table' AND name='document_dispatches'").scalar()
    if actual is None or AUTHORING._canonical(actual) != AUTHORING._canonical(EXPECTED_DISPATCH):
        raise RuntimeError("Word document schema drift: document_dispatches")
    objects = db.exec_driver_sql("SELECT sql FROM sqlite_schema WHERE tbl_name='document_dispatches' AND type IN ('index','trigger') AND sql IS NOT NULL ORDER BY type,name").fetchall()
    db.exec_driver_sql("CREATE TEMP TABLE word_dispatch_backup AS SELECT * FROM document_dispatches")
    db.exec_driver_sql("DROP TABLE document_dispatches")
    db.exec_driver_sql(EXPECTED_DISPATCH.replace(OLD_CHECK, NEW_CHECK))
    db.exec_driver_sql("INSERT INTO document_dispatches SELECT * FROM word_dispatch_backup")
    columns = ",".join(row[1] for row in db.exec_driver_sql("PRAGMA table_info(document_dispatches)"))
    if db.exec_driver_sql(f"SELECT 1 FROM (SELECT {columns} FROM document_dispatches EXCEPT SELECT {columns} FROM word_dispatch_backup) UNION ALL SELECT 1 FROM (SELECT {columns} FROM word_dispatch_backup EXCEPT SELECT {columns} FROM document_dispatches)").fetchone() or db.exec_driver_sql("SELECT (SELECT count(*) FROM document_dispatches)<>(SELECT count(*) FROM word_dispatch_backup)").scalar():  # noqa: S608
        raise RuntimeError("Word document dispatch copy mismatch")
    db.exec_driver_sql("DROP TABLE word_dispatch_backup")
    for (sql,) in objects:
        db.exec_driver_sql(sql)


def upgrade():
    if context.config.attributes["bowhead_target"] != "core":
        return
    db = context.get_bind()
    for name, expected in EXPECTED.items():
        actual = db.exec_driver_sql("SELECT sql FROM sqlite_schema WHERE type='trigger' AND name=?", (name,)).scalar()
        if actual is None or AUTHORING._canonical(actual) != AUTHORING._canonical(expected):
            raise RuntimeError(f"Word document schema drift: {name}")
    foreign_keys = int(db.exec_driver_sql("PRAGMA foreign_keys").scalar_one())
    if foreign_keys:
        db.exec_driver_sql("PRAGMA foreign_keys=OFF")
        if int(db.exec_driver_sql("PRAGMA foreign_keys").scalar_one()):
            raise RuntimeError("Word document rebuild requires a connection outside an active foreign-key transaction")
    db.exec_driver_sql("SAVEPOINT document_word_authoring")
    try:
        _rebuild_dispatch(db)
        for name, sql in DDL.items():
            db.exec_driver_sql(f"DROP TRIGGER {name}")
            db.exec_driver_sql(sql)
        if any(row[0].startswith("document_") or row[2].startswith("document_") for row in db.exec_driver_sql("PRAGMA foreign_key_check")):
            raise RuntimeError("Word document foreign key mismatch")
        db.exec_driver_sql("RELEASE SAVEPOINT document_word_authoring")
    except BaseException:
        db.exec_driver_sql("ROLLBACK TO SAVEPOINT document_word_authoring")
        db.exec_driver_sql("RELEASE SAVEPOINT document_word_authoring")
        raise
    finally:
        db.exec_driver_sql("PRAGMA foreign_keys=" + str(foreign_keys))


def downgrade():
    raise RuntimeError("Word document authoring requires restoring the matching pre-upgrade backup")
