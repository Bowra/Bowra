"""Later immutable revisions upgrade every admitted domain and resume forward."""

from __future__ import annotations

import json
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from bowhead_cli.migrate import upgrade
from bowhead_core.sqlite_domain_upgrade import SOURCE_REVISION, SPLIT_REVISION, run_domain_upgrade
from bowhead_core.sqlite_domains import DOMAIN_TABLES, DOMAIN_TARGET_FILES
from bowhead_core.sqlite_upgrade import LAYOUT_FILE, SQLiteUpgradeError, database_head


@pytest.fixture(scope="module")
def baseline(tmp_path_factory):
    directory = tmp_path_factory.mktemp("append-baseline") / "sqlite"
    from alembic.script import ScriptDirectory

    with pytest.MonkeyPatch.context() as historical:
        historical.setattr(ScriptDirectory, "get_heads", lambda _: [SPLIT_REVISION])
        upgrade(f"sqlite:///{directory / 'bowhead.db'}")
    return directory


@pytest.fixture
def directory(baseline, tmp_path):
    target = tmp_path / "sqlite"
    shutil.copytree(baseline, target)
    return target


def _plan(monkeypatch):
    number = int(SPLIT_REVISION.split("_", 1)[0])
    first, last = (f"{number + step:04d}_append_fixture" for step in (1, 2))
    for domain in ("core", "execution"):
        monkeypatch.setitem(DOMAIN_TABLES, domain, (*DOMAIN_TABLES[domain], f"append_fixture_{domain}"))
    order = (SOURCE_REVISION, SPLIT_REVISION, first, last)
    return first, last, order


def _run(directory, last, order, callback, initial=lambda *_: pytest.fail("A ready layout must not replay the initial split")):
    return run_domain_upgrade(directory, initial, target_head=last, known_revisions=frozenset(order), revision_order=order, upgrade_append=callback)


def _callback(first, last, calls, fail=None):
    def apply(domain, path):
        calls.append(domain)
        with closing(sqlite3.connect(path)) as db:
            current = database_head(path)
            for revision in (first, last):
                if current == last or current == first and revision == first:
                    continue
                db.execute("BEGIN IMMEDIATE")
                try:
                    if domain in {"core", "execution"}:
                        if revision == first:
                            db.execute(f"CREATE TABLE append_fixture_{domain}(value TEXT PRIMARY KEY)")
                            db.execute(f"INSERT INTO append_fixture_{domain}(value) VALUES ('unchanged')")
                        else:
                            db.execute(f"ALTER TABLE append_fixture_{domain} ADD COLUMN generation INTEGER DEFAULT 2")
                    db.execute("UPDATE alembic_version SET version_num=?", (revision,))
                    db.commit()
                    current = revision
                except BaseException:
                    db.rollback()
                    raise
                if fail is not None:
                    fail(domain, revision)

    return apply


def test_ready_layout_appends_all_twelve_targets_and_keeps_frozen_import_evidence(directory, monkeypatch):
    first, last, order = _plan(monkeypatch)
    calls = []
    _run(directory, last, order, _callback(first, last, calls))
    assert calls == list(DOMAIN_TARGET_FILES)
    state = json.loads((directory / LAYOUT_FILE).read_text())
    assert state["status"] == "ready" and state["target_head"] == last
    backup = Path(state["backup"])
    manifest = json.loads((backup / "backup-manifest.json").read_text())
    assert set(manifest["heads"]) == set(DOMAIN_TARGET_FILES)
    assert json.loads((backup / LAYOUT_FILE).read_text())["target_head"] == SPLIT_REVISION
    for filename in DOMAIN_TARGET_FILES.values():
        assert database_head(directory / filename) == last
    for domain in ("core", "execution"):
        with closing(sqlite3.connect(directory / DOMAIN_TARGET_FILES[domain])) as db:
            assert db.execute(f"SELECT value,generation FROM append_fixture_{domain}").fetchall() == [("unchanged", 2)]
    encoded = (directory / LAYOUT_FILE).read_bytes()
    _run(directory, last, order, lambda *_: pytest.fail("A completed append must not replay"))
    assert (directory / LAYOUT_FILE).read_bytes() == encoded


def test_append_resumes_a_committed_intermediate_revision_without_replaying_it(directory, monkeypatch):
    first, last, order = _plan(monkeypatch)
    calls = []

    def fail(domain, revision):
        if domain == "core" and revision == first:
            raise RuntimeError("interrupted after the first committed future revision")

    with pytest.raises(RuntimeError, match="interrupted"):
        _run(directory, last, order, _callback(first, last, calls, fail))
    state = json.loads((directory / LAYOUT_FILE).read_text())
    assert state["phase"] == "append" and state["status"] == "in_progress"
    assert database_head(directory / "bowhead.db") == first
    archived = state["backup"]
    _run(directory, last, order, _callback(first, last, calls))
    state = json.loads((directory / LAYOUT_FILE).read_text())
    assert state["status"] == "ready" and state["backup"] == archived
    assert calls.count("core") == 2


def test_unknown_database_head_blocks_append_before_any_new_backup(directory, monkeypatch):
    _first, last, order = _plan(monkeypatch)
    with closing(sqlite3.connect(directory / "bowhead.db")) as db:
        db.execute("UPDATE alembic_version SET version_num='unknown_source'")
        db.commit()
    with pytest.raises(SQLiteUpgradeError, match="unknown migration"):
        _run(directory, last, order, lambda *_: pytest.fail("Unknown revisions must not execute migrations"))
    assert not (directory.parent / "backups").exists()


def test_first_target_transaction_failure_rolls_back_and_retries_the_whole_revision(directory, monkeypatch):
    first, last, order = _plan(monkeypatch)

    def failure(domain, path):
        assert domain == "core"
        with closing(sqlite3.connect(path)) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute("CREATE TABLE rolled_back_append(value TEXT)")
                raise RuntimeError("before future revision commit")
            except BaseException:
                db.rollback()
                raise

    with pytest.raises(RuntimeError, match="before future revision commit"):
        _run(directory, last, order, failure)
    assert database_head(directory / "bowhead.db") == SPLIT_REVISION
    with closing(sqlite3.connect(directory / "bowhead.db")) as db:
        assert db.execute("SELECT 1 FROM sqlite_schema WHERE name='rolled_back_append'").fetchone() is None
    _run(directory, last, order, _callback(first, last, []))
    assert json.loads((directory / LAYOUT_FILE).read_text())["status"] == "ready"


def test_fresh_source_uses_frozen_cutover_before_future_tables_and_appended_revisions(baseline, tmp_path, monkeypatch):
    from alembic import command
    from alembic.config import Config
    from bowhead_core.sqlite_domains import ORIGINAL_TARGET_FILES

    from bowhead_core import migrations

    original = json.loads((baseline / LAYOUT_FILE).read_text())
    archive = Path(original["backup"])
    directory = tmp_path / "sqlite"
    shutil.copytree(archive / "sqlite", directory)
    shutil.copy2(archive / LAYOUT_FILE, directory / LAYOUT_FILE)
    first, last, order = _plan(monkeypatch)

    def cutover(domain, path, cleanup):
        config = Config()
        config.set_main_option("script_location", str(Path(migrations.__file__).parent))
        config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
        config.attributes["bowhead_target"] = domain
        config.attributes["bowhead_split_cleanup"] = cleanup
        config.attributes["bowhead_domain_coordinator"] = True
        command.upgrade(config, SPLIT_REVISION)

    assert all(database_head(directory / filename) == SOURCE_REVISION for filename in ORIGINAL_TARGET_FILES.values())
    _run(directory, last, order, _callback(first, last, []), initial=cutover)
    assert json.loads((directory / LAYOUT_FILE).read_text())["target_head"] == last
    for domain in ("core", "execution"):
        with closing(sqlite3.connect(directory / DOMAIN_TARGET_FILES[domain])) as db:
            assert db.execute(f"SELECT generation FROM append_fixture_{domain}").fetchone() == (2,)
