"""Gateway startup initializers preserve the admitted multi-file schema."""

from __future__ import annotations

import importlib
import json
import sqlite3
from contextlib import closing

import pytest
from bowhead_cli.migrate import upgrade
from bowhead_core.sqlite_domain_upgrade import schema_digest
from bowhead_core.sqlite_domains import DOMAIN_TARGET_FILES
from bowhead_core.user_memory import MEMORY_POLICY_CUTOVER_KEY
from core_legacy_database import upgrade as upgrade_legacy
from cryptography.fernet import Fernet


@pytest.fixture(scope="module")
def domain_directory(tmp_path_factory):
    directory = tmp_path_factory.mktemp("domain-initializers") / "sqlite"
    with pytest.MonkeyPatch.context() as environment:
        environment.setenv("BOWHEAD_FERNET_KEY", Fernet.generate_key().decode())
        upgrade(f"sqlite:///{directory / 'bowhead.db'}")
        yield directory


def _startup_stores(domain_directory):
    core_stores = [
        ("skill_instance_store", "SkillInstanceStore"),
        ("custom_subagent_store", "CustomSubagentStore"),
        ("public_entries", "PublicEntryStore"),
        ("home_proposal_cache_store", "HomeProposalCacheStore"),
        ("memory_notes", "MemoryNotesStore"),
        ("memory_pack_snapshots", "MemoryPackSnapshotsStore"),
        ("memory_note_anchors", "MemoryNoteAnchorsStore"),
        ("access_request_store", "AccessRequestStore"),
        ("access_control", "AccessControlStore"),
        ("team_settings_store", "TeamSettingsStore"),
        ("workspace_settings_store", "WorkspaceSettingsStore"),
        ("user_settings_store", "UserSettingsStore"),
        ("user_memory", "UserMemoryStore"),
        ("user_memory", "ProjectMemoryStore"),
        ("team_subagent_store", "TeamSubagentStore"),
        ("datarc_connection_store", "DatarcConnectionStore"),
    ]
    kb_stores = [
        ("files_store", "KbFilesStore", "files"),
        ("doc_type_store", "KbDocTypeStore", "doc_types"),
        ("team_template_store", "KbTeamTemplateStore", "team_templates"),
        ("extracted_record_store", "KbExtractedRecordStore", "extracted_records"),
        ("extracted_rows_store", "KbExtractedRowsStore", "extracted_rows"),
        ("per_row_jobs_store", "KbPerRowJobsStore", "per_row_jobs"),
        ("matrix_store", "KbMatrixStore", "matrix"),
        ("lifespan_marker_store", "KbLifespanMarkerStore", "doc_types"),
        ("workspace_quotas_store", "KbWorkspaceQuotasStore", "per_row_jobs"),
    ]
    instances = []
    for module, name in core_stores:
        store = getattr(importlib.import_module("bowhead_core." + module), name)
        instances.append(store(domain_directory / "bowhead.db"))
    for module, name, domain in kb_stores:
        store = getattr(importlib.import_module("bowhead_core.kb." + module), name)
        instances.append(store(domain_directory / DOMAIN_TARGET_FILES[domain]))
    from bowhead_gateway.feishu.store import FeishuEventStore

    instances.append(FeishuEventStore(domain_directory / "bowhead.db"))
    from bowhead_core.sqlite_domain_runtime import connect
    from bowhead_memory.candidates import MemoryCandidateStore

    instances.append(MemoryCandidateStore(str(domain_directory / "bowhead.db"), connect_factory=connect))
    return instances


@pytest.mark.asyncio
async def test_core_and_kb_startup_initializers_keep_schema_and_seed_business_metadata(domain_directory):
    from bowhead_core._db import bootstrap_shared_db
    from bowhead_core._ref_audit import init_ref_audit

    await bootstrap_shared_db(domain_directory / "bowhead.db")
    await init_ref_audit(domain_directory / "bowhead.db")
    from bowhead_core.approval_store import ApprovalStore
    from bowhead_core.export_snapshot import ExportSnapshotStore
    from bowhead_core.export_store import ExportStore
    from bowhead_core.turn_log_store import TurnLogStore

    snapshots = ExportSnapshotStore(domain_directory / "bowhead.db")
    for store in (ApprovalStore(str(domain_directory / "bowhead.db")), TurnLogStore(str(domain_directory / "bowhead.db"))):
        assert str(store.db_path) == str(domain_directory / "bowhead_execution.db")
    for store in (snapshots, ExportStore(domain_directory / "bowhead.db", snapshots)):
        assert str(store.db_path) == str(domain_directory / "bowhead_content.db")
    instances = _startup_stores(domain_directory)
    expected = json.loads((domain_directory / "sqlite-layout.json").read_text())["targets"]
    for store in instances:
        initialize = getattr(store, "initialize", None) or store.init
        await initialize()
        for domain, filename in DOMAIN_TARGET_FILES.items():
            with closing(sqlite3.connect(domain_directory / filename)) as connection:
                assert schema_digest(connection) == expected[domain]["schema_digest"], f"{type(store).__name__} changed {domain} during startup"
    with closing(sqlite3.connect(domain_directory / "bowhead.db")) as core:
        assert core.execute("SELECT value FROM memory_migration_state WHERE key=?", (MEMORY_POLICY_CUTOVER_KEY,)).fetchone() == ("complete",)
        assert core.execute("SELECT singleton FROM team_subagent_schema_meta").fetchone() == (1,)


@pytest.mark.asyncio
async def test_all_legacy_initializers_match_formal_source_before_automatic_cutover(tmp_path, monkeypatch):
    import anyio
    from bowhead_core._db import close_pooled_readers
    from bowhead_core.sqlite_domains import ORIGINAL_TARGET_FILES

    directory = tmp_path / "sqlite"
    await anyio.to_thread.run_sync(lambda: upgrade_legacy(f"sqlite:///{directory / 'bowhead.db'}"))
    expected = {}
    for domain, filename in ORIGINAL_TARGET_FILES.items():
        with closing(sqlite3.connect(directory / filename)) as db:
            expected[domain] = schema_digest(db)
    for store in _startup_stores(directory):
        await (getattr(store, "initialize", None) or store.init)()
        for domain, filename in ORIGINAL_TARGET_FILES.items():
            with closing(sqlite3.connect(directory / filename)) as db:
                assert schema_digest(db) == expected[domain], f"{type(store).__name__} added unversioned schema to {domain}"
    await close_pooled_readers()
    await anyio.to_thread.run_sync(lambda: upgrade(f"sqlite:///{directory / 'bowhead.db'}"))
    assert json.loads((directory / "sqlite-layout.json").read_text())["status"] == "ready"
