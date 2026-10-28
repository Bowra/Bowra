import sqlite3
from importlib import import_module
from pathlib import Path

import pytest
from alembic import command
from test_agent_eval_policy_plan_store import NOW, _seed
from test_office_upload_confirmation_migration import _config

from bowhead_core import migrations

REVISION = import_module("bowhead_core.migrations.versions.0176_platform_contract_revisions")
pytestmark = pytest.mark.integration


def _append_platform_revision(db: sqlite3.Connection, *, revision_id: str, created_by: str) -> None:
    db.execute(
        """
        INSERT INTO agent_eval_suite_revision(
          id,suite_id,sequence,definition_json,definition_hash,scorer_bundle_ref,
          judge_policy_json,case_count,case_set_hash,created_by,created_at
        ) VALUES(?,'aes_platform',2,'{}',?,'evalkit.core@1','{"enabled":false}',1,?,?,?)
        """,
        (revision_id, "sha256:" + "4" * 64, "sha256:" + "5" * 64, created_by, NOW),
    )


def test_only_the_agent_owner_can_append_a_platform_contract_revision(tmp_path: Path) -> None:
    path = tmp_path / "core.db"
    config = _config(path)
    command.upgrade(config, REVISION.down_revision)
    _seed(path)  # Owner user_owner, member user_other, platform suite aes_platform at revision 1.
    with sqlite3.connect(path) as db:
        db.execute("UPDATE team_members SET role='team_admin' WHERE user_id='user_other'")
        # Before: the platform contract could never gain a revision, not even from its Owner.
        with pytest.raises(sqlite3.IntegrityError, match="suite revision scope mismatch"):
            _append_platform_revision(db, revision_id="aev_platform_2", created_by="user_owner")

    command.upgrade(config, "head")
    command.upgrade(config, "head")
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (migrations.chain_head(),)
        # A Team admin still can't change an Agent's platform contract.
        with pytest.raises(sqlite3.IntegrityError, match="suite revision scope mismatch"):
            _append_platform_revision(db, revision_id="aev_platform_admin", created_by="user_other")
        _append_platform_revision(db, revision_id="aev_platform_2", created_by="user_owner")
        assert db.execute("SELECT sequence FROM agent_eval_suite_revision WHERE suite_id='aes_platform' ORDER BY sequence").fetchall() == [(1,), (2,)]


@pytest.mark.parametrize("target", ["files", "doc_types", "extracted_records", "extracted_rows", "per_row_jobs", "team_templates", "matrix"])
def test_other_targets_only_advance_the_ledger(tmp_path: Path, target: str) -> None:
    path = tmp_path / f"{target}.db"
    config = _config(path, target)
    command.upgrade(config, REVISION.down_revision)
    with sqlite3.connect(path) as db:
        before = db.execute("SELECT name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()
    command.upgrade(config, "head")
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY name").fetchall() == before
