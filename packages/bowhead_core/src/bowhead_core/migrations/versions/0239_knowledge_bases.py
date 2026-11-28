"""Add knowledge base containers and point every knowledge file at its home's default.

Only the files target changes. Two tables join ``kb_files.db``: ``kb_knowledge_bases`` (one
row per knowledge base; one default per Team and home) and ``kb_knowledge_base_members``
(people, groups and Spaces with a level). ``kb_files`` gains a nullable ``knowledge_base_id``
by ``ADD COLUMN``, so the table is not rebuilt and its 0179/0238 triggers stay untouched.

Default IDs are deterministic (``kbase_`` + the first 24 hex digits of
``sha256("default|<team>|<home>|<home id>")``), so this backfill, later lazy creation and
permission checks name the same row. The backfill creates a default for every home that has
rows, soft-deleted rows included, and points every row at it. It is reentrant and verifies that
no row is left without a knowledge base or points at another home before releasing the
savepoint. Defaults store no name; it is rendered from the home. The timestamps of a default are
its home's earliest upload, because a revision must not read the current time.
"""

import hashlib

from alembic import context

revision = "0239_knowledge_bases"
down_revision = "0238_file_library"
branch_labels = None
depends_on = None

STATEMENTS = (
    """CREATE TABLE kb_knowledge_bases (
        id TEXT PRIMARY KEY,
        team_id TEXT NOT NULL,
        home TEXT NOT NULL CHECK(home IN ('personal','space','team')),
        home_id TEXT NOT NULL,
        is_default INTEGER NOT NULL CHECK(is_default IN (0,1)),
        name TEXT CHECK(name IS NULL OR length(name) BETWEEN 1 AND 120),
        description TEXT CHECK(description IS NULL OR length(description) <= 1000),
        audience TEXT NOT NULL CHECK(audience IN ('owner','members','space','team')),
        audience_level TEXT NOT NULL CHECK(audience_level IN ('use','edit')),
        owner_user_id TEXT,
        revision INTEGER NOT NULL DEFAULT 1 CHECK(revision>=1),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        deleted_at TEXT,
        CHECK((is_default=1)=(name IS NULL)),
        CHECK((home='personal')=(audience='owner')),
        CHECK(home!='personal' OR owner_user_id=home_id),
        CHECK(audience!='space' OR home='space'),
        CHECK(audience!='team' OR (home='team' AND audience_level='use')),
        CHECK(owner_user_id IS NOT NULL OR (is_default=1 AND home!='personal'))
    )""",
    "CREATE UNIQUE INDEX uq_kb_knowledge_bases_default ON kb_knowledge_bases(team_id,home,home_id) WHERE is_default=1",
    "CREATE INDEX idx_kb_knowledge_bases_home ON kb_knowledge_bases(team_id,home,home_id) WHERE deleted_at IS NULL",
    """CREATE TABLE kb_knowledge_base_members (
        knowledge_base_id TEXT NOT NULL REFERENCES kb_knowledge_bases(id) ON DELETE CASCADE,
        principal_type TEXT NOT NULL CHECK(principal_type IN ('user','group','space')),
        principal_id TEXT NOT NULL,
        level TEXT NOT NULL CHECK(level IN ('use','edit','manage')),
        added_by TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY(knowledge_base_id,principal_type,principal_id)
    )""",
    "CREATE INDEX idx_kb_knowledge_base_members_principal ON kb_knowledge_base_members(principal_type,principal_id)",
    "ALTER TABLE kb_files ADD COLUMN knowledge_base_id TEXT REFERENCES kb_knowledge_bases(id)",
    "CREATE INDEX idx_kb_files_knowledge_base ON kb_files(knowledge_base_id,uploaded_at)",
)

# Frozen copies of the runtime rules (`bowhead_core.kb.knowledge_base_store`): a revision must not
# import application code that can change after it ships.
DEFAULT_AUDIENCE = {"personal": ("owner", "use"), "space": ("space", "edit"), "team": ("team", "use")}
HOME_SQL = "CASE scope WHEN 'workspace' THEN 'space' ELSE scope END"
PAGE_SIZE = 1000


def default_knowledge_base_id(team_id: str, home: str, home_id: str) -> str:
    return "kbase_" + hashlib.sha256(f"default|{team_id}|{home}|{home_id}".encode()).hexdigest()[:24]


def backfill(db) -> dict[str, int]:
    """Create each home's default and point every row without a knowledge base at it."""
    homes = db.exec_driver_sql(f"SELECT team_id,{HOME_SQL},scope_id,MIN(uploaded_at) FROM kb_files GROUP BY 1,2,3").fetchall()  # noqa: S608 -- Fixed SQL.
    defaults = []
    for team_id, home, home_id, first_upload in homes:
        audience, level = DEFAULT_AUDIENCE[home]
        owner = home_id if home == "personal" else None
        defaults.append(
            (default_knowledge_base_id(team_id, home, home_id), team_id, home, home_id, audience, level, owner, first_upload, first_upload)
        )
    created = 0
    for row in defaults:
        created += db.exec_driver_sql(
            "INSERT OR IGNORE INTO kb_knowledge_bases(id,team_id,home,home_id,is_default,name,description,audience,audience_level,"
            "owner_user_id,revision,created_at,updated_at) VALUES (?,?,?,?,1,NULL,NULL,?,?,?,1,?,?)",
            row,
        ).rowcount
    pointed, after = 0, ""
    while True:
        page = db.exec_driver_sql(
            f"SELECT file_id,team_id,{HOME_SQL},scope_id FROM kb_files WHERE knowledge_base_id IS NULL AND file_id>? ORDER BY file_id LIMIT ?",  # noqa: S608 -- Fixed SQL.
            (after, PAGE_SIZE),
        ).fetchall()
        if not page:
            break
        for file_id, team_id, home, home_id in page:
            pointed += db.exec_driver_sql(
                "UPDATE kb_files SET knowledge_base_id=? WHERE file_id=? AND knowledge_base_id IS NULL",
                (default_knowledge_base_id(team_id, home, home_id), file_id),
            ).rowcount
        after = page[-1][0]
    missing = db.exec_driver_sql("SELECT COUNT(*) FROM kb_files WHERE knowledge_base_id IS NULL").scalar()
    mismatched = db.exec_driver_sql(
        "SELECT COUNT(*) FROM kb_files f LEFT JOIN kb_knowledge_bases k ON k.id=f.knowledge_base_id "
        "WHERE k.id IS NULL OR k.team_id!=f.team_id OR k.home!=(CASE f.scope WHEN 'workspace' THEN 'space' ELSE f.scope END) OR k.home_id!=f.scope_id"
    ).scalar()
    if missing or mismatched:
        raise RuntimeError(
            f"Knowledge base backfill left {missing} knowledge files without a knowledge base and {mismatched} pointing at another home; "
            "the upgrade was rolled back. Restore the paired database snapshot and report this revision."
        )
    return {"homes": len(homes), "created": created, "pointed": pointed}


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "files":
        return
    db = context.get_bind()
    db.exec_driver_sql("SAVEPOINT knowledge_bases")
    try:
        for statement in STATEMENTS:
            db.exec_driver_sql(statement)
        backfill(db)
        db.exec_driver_sql("RELEASE knowledge_bases")
    except BaseException:
        db.exec_driver_sql("ROLLBACK TO knowledge_bases")
        db.exec_driver_sql("RELEASE knowledge_bases")
        raise


def downgrade() -> None:
    raise NotImplementedError("Restore the paired database snapshot to roll back.")
