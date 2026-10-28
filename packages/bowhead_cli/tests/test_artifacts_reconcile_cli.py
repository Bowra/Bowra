"""bowhead artifacts reconcile：默认只预演；--apply 落标记；--restore-all 回退。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import aiosqlite
from bowhead_cli.main import app
from bowhead_core.artifact_store import ArtifactStore, build_artifact_backend_from_env
from typer.testing import CliRunner


def _status(db: Path, aid: str) -> str:
    async def read() -> str:
        async with aiosqlite.connect(db) as conn, conn.execute("SELECT status FROM artifacts WHERE id=?", (aid,)) as cur:
            row = await cur.fetchone()
        return row[0]

    return asyncio.run(read())


def test_reconcile_cli_dry_run_apply_and_restore(tmp_path: Path, monkeypatch) -> None:
    db = tmp_path / "data" / "sqlite" / "bowhead.db"
    db.parent.mkdir(parents=True)
    monkeypatch.setenv("BOWHEAD_ARTIFACT_BACKEND", "local")
    monkeypatch.chdir(tmp_path)

    async def seed() -> str:
        # Use the same wiring as the command: derive the artifact root from the main database path.
        store = ArtifactStore(db_path=str(db), backend=build_artifact_backend_from_env(db))
        await store.initialize()
        lost = await store.put_bytes(content=b"x", filename="lost.csv", mime="text/csv", created_by="u1")
        for i in range(5):
            await store.put_bytes(content=b"y", filename=f"ok{i}.csv", mime="text/csv", created_by="u1")
        Path(lost.path).unlink()
        return lost.id

    lost_id = asyncio.run(seed())
    runner = CliRunner()
    args = ["artifacts", "reconcile", "--db", str(db), "--json"]

    dry = runner.invoke(app, args)
    assert dry.exit_code == 0, dry.output
    assert json.loads(dry.output)["absent"] == 1
    assert _status(db, lost_id) == "ready"

    applied = runner.invoke(app, [*args, "--apply"])
    assert applied.exit_code == 0, applied.output
    assert json.loads(applied.output)["marked"] == 1
    assert _status(db, lost_id) == "missing"

    restored = runner.invoke(app, ["artifacts", "reconcile", "--db", str(db), "--restore-all"])
    assert restored.exit_code == 0, restored.output
    assert _status(db, lost_id) == "ready"
