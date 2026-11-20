"""Fence personal settings only under their privacy and access keys.

Revision ID: 0231_user_settings_key_fence
Revises: 0230_unfence_routine_metadata

docs/sprints/user-settings-key-fence-2026-11-20/tech-design.md. Every write to
``user_settings`` took the exclusive core lease, so saving the knowledge-base
scope filter on 116 (1.7.59) repeated 68 times over 9.7 s behind running Turns.
No cross-file trigger or view reads the table. Pausing memory consolidation and
the person's Plugin disable lists keep their fences; every other key, such as
the interface language or the knowledge-base scope filter, takes none. Like
``0230``, this revision owns its three trigger definitions; fresh installs,
whose ``0221`` already scopes the fences by key, end with byte-identical
triggers.
"""

from alembic import context

from bowhead_core.sqlite_domain_runtime import _AUTHORITY

revision = "0231_user_settings_key_fence"
down_revision = "0230_unfence_routine_metadata"
branch_labels = None
depends_on = None

TABLE = "user_settings"
# The keys whose rows stay fenced, frozen at this revision.
FENCED_KEYS = ("builtin_plugins_disabled", "memory_dreaming_paused", "team_plugins_disabled")


def _key_in(row: str) -> str:
    listed = ",".join("'" + key + "'" for key in FENCED_KEYS)
    return f'{row}."key" IN ({listed})'


def trigger(suffix: str) -> str:
    """One fence of ``user_settings``: ``delete``, ``insert`` or ``update``."""
    columns = _AUTHORITY["core"][TABLE]
    names = ",".join(f'"{column}"' for column in columns)
    changed = " OR ".join(f'OLD."{column}" IS NOT NEW."{column}"' for column in columns)
    operation, condition = {
        "delete": ("DELETE", _key_in("OLD")),
        "insert": ("INSERT", _key_in("NEW")),
        "update": (f"UPDATE OF {names}", f"({_key_in('OLD')} OR {_key_in('NEW')}) AND ({changed})"),
    }[suffix]
    return (
        f'CREATE TRIGGER "trg_domain_mutation_{TABLE}_{suffix}" BEFORE {operation} ON "{TABLE}" WHEN {condition} '
        f"BEGIN SELECT bowhead_domain_mutation_v2('{TABLE}','{suffix}'); END"
    )


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    db = context.get_bind()
    db.exec_driver_sql("SAVEPOINT user_settings_key_fence")
    try:
        for suffix in ("delete", "insert", "update"):
            db.exec_driver_sql(f'DROP TRIGGER "trg_domain_mutation_{TABLE}_{suffix}"')
            db.exec_driver_sql(trigger(suffix))
        db.exec_driver_sql("RELEASE user_settings_key_fence")
    except BaseException:
        db.exec_driver_sql("ROLLBACK TO user_settings_key_fence")
        db.exec_driver_sql("RELEASE user_settings_key_fence")
        raise


def downgrade() -> None:
    raise NotImplementedError("Bowhead does not support schema downgrade")
