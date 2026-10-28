from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

from bowhead_cli.main import app
from bowhead_cli.migrate import upgrade
from bowhead_core.artifact_store import ArtifactStore, LocalBackend
from bowhead_core.kb.files_store import KbFilesStore
from typer.testing import CliRunner


def dump(path: Path) -> str:
    with sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True) as db:
        return "\n".join(db.iterdump())


def test_inventory_pages_are_read_only_and_report_unknown_binding(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BOWHEAD_ARTIFACT_BACKEND", "local")
    db = tmp_path / "data" / "sqlite" / "bowhead.db"
    db.parent.mkdir(parents=True)
    upgrade(f"sqlite:///{db}", "files")

    async def seed() -> None:
        artifacts = ArtifactStore(str(db), LocalBackend(tmp_path / "data" / "artifacts"))
        await artifacts.initialize()
        files = KbFilesStore(db.parent / "kb_files.db")
        for i in range(6):
            art = await artifacts.put_bytes(content=b"hello", filename=f"private-{i}.txt", mime="text/plain", created_by="u1")
            await files.create(
                artifact_id=art.id,
                filename=art.filename,
                mime_type=art.mime,
                size_bytes=art.size,
                scope="personal",
                scope_id="u1",
                workspace_id="ws_test",
                team_id="team_other" if i == 5 else "team_test",
                uploaded_by="u1",
            )
            if i == 0:
                Path(art.path).unlink()

    asyncio.run(seed())
    before = [dump(db), dump(db.parent / "kb_files.db")]
    runner = CliRunner()
    args = ["kb", "inventory", "--db", str(db), "--team-id", "team_test", "--limit", "2"]
    seen: list[str] = []
    after = ""
    all_items = []
    while True:
        result = runner.invoke(app, [*args, "--after", after])
        assert result.exit_code == 0, result.output
        page = json.loads(result.output)
        assert page["read_only"] and not page["index_checked"]
        assert page["binding_unverified"] == page["checked"]
        assert page["rebuild_estimate_seconds"] is None
        assert "private-" not in result.output and str(tmp_path) not in result.output
        all_items.extend(page["items"])
        seen.extend(item["file_id"] for item in page["items"])
        if not page["has_more"]:
            break
        assert page["next_after"] > after
        after = page["next_after"]
    assert len(seen) == len(set(seen)) == 5
    assert sum(item["health"]["source_health"] == "confirmed_absent" for item in all_items) == 1
    assert [dump(db), dump(db.parent / "kb_files.db")] == before


def test_inventory_missing_databases_does_not_create_them(tmp_path: Path) -> None:
    result = CliRunner().invoke(app, ["kb", "inventory", "--db", str(tmp_path / "absent" / "bowhead.db")])
    assert result.exit_code != 0
    assert not (tmp_path / "absent").exists()
