"""Let an Agent's Owner run a Turn on the saved draft (the Owner draft trial).

Revision ID: 0234_owner_draft_trial
Revises: 0233_personal_mcp_oauth

docs/sprints/agent-page-redesign-2026-11-24/trial-tech-design.md §3. A trial Turn's primary
execution binds the frozen revision of a release candidate instead of the live release. The
execution file gains ``agent_owner_trial_turn``: one immutable row per trial Turn, written in the
Turn's own admission transaction, naming the session, Team, Agent, Owner and the candidate's
revision and capability fingerprint. Its index serves the Owner's trial list. Account erasure owns
rows by ``owner_user_id``; a retired account cannot receive a new row, through the same reviewed
guard as ``user_result_pins``. A Turn's deletion removes its row.

Three reviewed guards refused every release-less Team execution or root binding outside an
evaluation. Each now also accepts one covered by a trial row whose Owner is the Agent's Owner and
the session's user, while the candidate is ``prepared`` or ``pending``.
``trg_turn_agent_execution_revision`` already passes ``NEW.turn_id``, so only its registered
predicate changes. The two root-binding guards had no Turn reference: their compiled triggers are
replaced here with the registered forms that add ``NEW.turn_id`` (and, for the assignment guard,
``NEW.release_id``). A trigger that matches neither the 0220 compiled form nor the reviewed new
form is refused rather than replaced. Only the execution target runs DDL, inside one savepoint;
every other target advances its ledger. No stored data changes.
"""

from alembic import context

revision = "0234_owner_draft_trial"
down_revision = "0233_personal_mcp_oauth"
branch_labels = None
depends_on = None

_SAVEPOINT = "owner_draft_trial"

TABLE = """CREATE TABLE agent_owner_trial_turn (
    turn_id                TEXT PRIMARY KEY REFERENCES turns(id) ON DELETE CASCADE,
    session_id             TEXT NOT NULL,
    team_id                TEXT NOT NULL,
    agent_id               TEXT NOT NULL,
    owner_user_id          TEXT NOT NULL,
    candidate_id           TEXT NOT NULL CHECK(candidate_id LIKE 'arc_%'),
    revision_ref           TEXT NOT NULL CHECK(revision_ref LIKE 'arv_%'),
    capability_fingerprint TEXT NOT NULL CHECK(capability_fingerprint LIKE 'sha256:%'),
    candidate_kind         TEXT NOT NULL CHECK(candidate_kind IN ('draft','submitted')),
    frozen_at              TEXT NOT NULL,
    created_at             TEXT NOT NULL
)"""

OWNER_INDEX = "CREATE INDEX idx_agent_owner_trial_turn_owner ON agent_owner_trial_turn(team_id, agent_id, owner_user_id, created_at DESC)"

IMMUTABLE_GUARD = (
    "CREATE TRIGGER trg_agent_owner_trial_turn_immutable BEFORE UPDATE ON agent_owner_trial_turn "
    "BEGIN SELECT RAISE(ABORT,'agent owner trial turn is immutable'); END"
)

# The compiled form of the reviewed guard registered in ``sqlite_domain_guards.json``: the account
# deletion ledger lives in core, so the execution file asks the runtime's domain predicate.
RETIRED_GUARD = (
    'CREATE TRIGGER trg_account_retired_agent_owner_trial_turn_insert BEFORE INSERT ON "agent_owner_trial_turn" '
    "WHEN bowhead_domain_predicate_v1('trg_account_retired_agent_owner_trial_turn_insert',NEW.\"owner_user_id\")=1 "
    "BEGIN SELECT RAISE(ABORT,'account is deleted'); END"
)

STATEMENTS = (TABLE, OWNER_INDEX, IMMUTABLE_GUARD, RETIRED_GUARD)

# The root-binding guards as 0220 compiled them, frozen here for drift detection.
PREVIOUS_GUARDS = {
    "trg_turn_agent_binding_revision": (
        "CREATE TRIGGER trg_turn_agent_binding_revision\n"
        "        BEFORE INSERT ON turn_agent_binding WHEN bowhead_domain_predicate_v1('trg_turn_agent_binding_revision',"
        'NEW."agent_source_kind",NEW."release_id",NEW."team_id",NEW."requested_agent_id",NEW."revision_ref",'
        'NEW."capability_fingerprint")=1 BEGIN\n'
        "          SELECT RAISE(ABORT, 'turn Agent binding revision mismatch');\n"
        "        END"
    ),
    "trg_turn_agent_binding_assignment": (
        "CREATE TRIGGER trg_turn_agent_binding_assignment\n"
        "        BEFORE INSERT ON turn_agent_binding WHEN bowhead_domain_predicate_v1('trg_turn_agent_binding_assignment',"
        'NEW."agent_source_kind",NEW."assignment_id",NEW."team_id",NEW."user_id",NEW."requested_agent_id")=1 BEGIN\n'
        "          SELECT RAISE(ABORT, 'turn Agent binding assignment mismatch');\n"
        "        END"
    ),
}

# The reviewed replacements; they equal the ``compiled`` entries of the registry at this revision.
REPLACEMENT_GUARDS = {
    "trg_turn_agent_binding_revision": (
        "CREATE TRIGGER trg_turn_agent_binding_revision\n"
        "        BEFORE INSERT ON turn_agent_binding WHEN bowhead_domain_predicate_v1('trg_turn_agent_binding_revision',"
        'NEW."agent_source_kind",NEW."release_id",NEW."team_id",NEW."requested_agent_id",NEW."revision_ref",'
        'NEW."capability_fingerprint",NEW."turn_id")=1 BEGIN\n'
        "          SELECT RAISE(ABORT, 'turn Agent binding revision mismatch');\n"
        "        END"
    ),
    "trg_turn_agent_binding_assignment": (
        "CREATE TRIGGER trg_turn_agent_binding_assignment\n"
        "        BEFORE INSERT ON turn_agent_binding WHEN bowhead_domain_predicate_v1('trg_turn_agent_binding_assignment',"
        'NEW."agent_source_kind",NEW."assignment_id",NEW."team_id",NEW."user_id",NEW."requested_agent_id",'
        'NEW."turn_id",NEW."release_id")=1 BEGIN\n'
        "          SELECT RAISE(ABORT, 'turn Agent binding assignment mismatch');\n"
        "        END"
    ),
}


def _canonical(sql: str) -> str:
    return "".join(sql.split())


def replace_binding_guard(db, name: str) -> bool:
    """Install the reviewed 0234 form of one root-binding guard; False when it is already current.

    A fresh layout compiles its guards from the current registry when the split runs, so it may
    already hold the reviewed form. Anything else than the 0220 form is drift and is refused.
    """
    from bowhead_core.sqlite_domain_runtime import _TRIGGERS

    row = db.exec_driver_sql("SELECT sql FROM sqlite_schema WHERE type='trigger' AND name=?", (name,)).fetchone()
    current = _canonical(row[0]) if row is not None and row[0] is not None else None
    if current in {_canonical(REPLACEMENT_GUARDS[name]), _canonical(_TRIGGERS[name]["compiled"])}:
        return False
    if current != _canonical(PREVIOUS_GUARDS[name]):
        raise RuntimeError(f"{name} does not match its reviewed definition; refusing to replace it")
    db.exec_driver_sql(f"DROP TRIGGER {name}")
    db.exec_driver_sql(REPLACEMENT_GUARDS[name])
    return True


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "execution":
        return
    db = context.get_bind()
    db.exec_driver_sql(f"SAVEPOINT {_SAVEPOINT}")
    try:
        for statement in STATEMENTS:
            db.exec_driver_sql(statement)
        for name in REPLACEMENT_GUARDS:
            replace_binding_guard(db, name)
        db.exec_driver_sql(f"RELEASE {_SAVEPOINT}")
    except BaseException:
        db.exec_driver_sql(f"ROLLBACK TO {_SAVEPOINT}")
        db.exec_driver_sql(f"RELEASE {_SAVEPOINT}")
        raise


def downgrade() -> None:
    raise NotImplementedError("Bowhead does not support schema downgrade")
