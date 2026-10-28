"""Regression: the DIRECT bundle path (_process_zip_bundle, used by
POST /kb/files/bundle AND /kb/admin/import) must fan out ALL children in ONE
background task via worker.parse_and_index_many — NOT one per-file
background_tasks.add_task(parse_and_index).

Per-file add_task is serially awaited by FastAPI, so a single slow scanned PDF
blocks the whole bundle and the worker's Semaphore(5) is never exploited. The
presign-bundle confirm path (kb_presign.py) already uses parse_and_index_many;
this locks the direct path to the same contract.
"""

from __future__ import annotations

import io
import sqlite3
import sys
import zipfile
from types import SimpleNamespace
from typing import TYPE_CHECKING

from bowhead_cli.migrate import upgrade
from bowhead_core.artifact_store import LocalBackend
from bowhead_core.kb.files_store import KbFilesStore
from bowhead_core.kb.permission import ScopeContext
from bowhead_core.migrations.baseline_schema import initial_schema
from bowhead_core.user_store import User
from bowhead_gateway.routes.kb import _process_zip_bundle
from fastapi import BackgroundTasks

from bowhead_core import ArtifactStore

if TYPE_CHECKING:
    from pathlib import Path


def _build_zip(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in entries.items():
            zf.writestr(name, content)
    return buf.getvalue()


class _FakeWorker:
    def __init__(self) -> None:
        self.single: list[str] = []
        self.many: list[list[str]] = []

    async def parse_and_index(self, file_id: str) -> None:
        self.single.append(file_id)

    async def parse_and_index_many(self, file_ids: list[str]) -> None:
        self.many.append(list(file_ids))


async def test_direct_bundle_enqueues_single_parse_and_index_many(initialize_migrated_kb_files_database, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setitem(
        sys.modules,
        "magic",
        SimpleNamespace(from_buffer=lambda payload, mime: "application/pdf" if payload.startswith(b"%PDF") else "text/plain"),
    )
    artifact_db_path = tmp_path / "bowhead.db"
    with sqlite3.connect(artifact_db_path) as connection:
        connection.executescript(initial_schema("core"))
        connection.commit()
    upgrade(f"sqlite+aiosqlite:///{artifact_db_path}", target="core")
    artifact_store = ArtifactStore(
        db_path=str(artifact_db_path),
        backend=LocalBackend(root_dir=tmp_path / "blobs"),
    )
    await artifact_store.initialize()
    initialize_migrated_kb_files_database(tmp_path / "kb_files.db")
    files_store = KbFilesStore(tmp_path / "kb_files.db")
    await files_store.initialize()
    worker = _FakeWorker()

    app_state = SimpleNamespace(
        artifact_store=artifact_store,
        kb_files_store=files_store,
        kb_worker=worker,
    )
    request = SimpleNamespace(app=SimpleNamespace(state=app_state))
    user = User(
        id="u_alice",
        email="alice@test.com",
        team_id="team_test",
        tier="ultra",
        email_verified=True,
    )
    ctx = ScopeContext(
        user_id="u_alice",
        workspace_id="ws_test",
        team_id="team_test",
        role="user",
    )

    # 3 ingestable PDFs + macOS junk that must be filtered out.
    raw = _build_zip(
        {
            "dir/": b"",
            "dir/a.pdf": b"%PDF-1.4\nalpha",
            "dir/b.pdf": b"%PDF-1.4\nbeta",
            "c.pdf": b"%PDF-1.4\ngamma",
            "dir/notes.bin": b"synthetic searchable text",
            "__MACOSX/._a.pdf": b"junk",
            ".DS_Store": b"junk",
        }
    )
    bt = BackgroundTasks()
    resp = await _process_zip_bundle(
        raw,
        "bundle.zip",
        request=request,
        background_tasks=bt,
        user=user,
        ctx=ctx,
        scope="personal",
        scope_id="u_alice",
        use_llm=False,
    )

    # 3 real files ingested, junk skipped.
    assert len(resp.uploaded) == 3
    assert resp.total_entries == 6  # ZIP directory entries do not count toward file-processing progress.
    assert any(item.filename == "dir/notes.bin" and item.reason_code == "kb_bundle_entry_type_unsupported" for item in resp.skipped)
    child_ids = [f.file_id for f in resp.uploaded]

    # EXACTLY ONE background task, and it is the concurrent fan-out — not 3
    # per-file parse_and_index tasks.
    assert len(bt.tasks) == 1
    task = bt.tasks[0]
    # bound methods are fresh objects per access → compare func + self, not `is`
    assert task.func.__name__ == "parse_and_index_many"
    assert task.func.__self__ is worker
    assert task.args[0] == child_ids

    # parse_and_index_many not invoked yet (it's a background task); but the
    # enqueue contract is what we assert. Run it to confirm fan-out semantics.
    await task.func(*task.args, **task.kwargs)
    assert worker.many == [child_ids]
    assert worker.single == []  # no per-file serial enqueue
