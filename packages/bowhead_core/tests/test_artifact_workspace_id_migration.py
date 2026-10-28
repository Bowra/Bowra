"""artifacts.workspace_id —— 加列迁移 + 写入点 + 回填梯子（空间隔离 sprint 批3）。

本批**只观测**：落库、可回填、可量化；**不改任何授权判定**。
所以这里断言的是「归属落得对」和「推不出就留 NULL」，
**不**断言任何 403/404——那是批4 翻 fail-close 时的事。

★ NULL 是本批要交付给批4 的那个量。任何把 NULL 兜底成某个值的改动都会把它量没了，
  所以「推不出时保持 NULL」是硬断言，不是风格偏好。
"""

from __future__ import annotations

import ast
import asyncio
import re
import sqlite3
from importlib import import_module
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from bowhead_core.artifact_store import (
    ARTIFACT_WORKSPACE_PROVENANCE_SQL,
    ArtifactStore,
    LocalBackend,
)

from bowhead_core import migrations

pytestmark = pytest.mark.integration

# ★ Use import_module, not a literal: `test_migration_head_pins_are_consistent` forbids
# bare revision literals under packages/ (hardcoded copies silently become stale at the next revision).
MIGRATION = import_module("bowhead_core.migrations.versions.0096_artifact_workspace_id")

REPO_ROOT = Path(__file__).resolve().parents[3]
LADDER = ARTIFACT_WORKSPACE_PROVENANCE_SQL.strip()


def _config(path: Path, target: str = "core") -> Config:
    config = Config()
    config.set_main_option("script_location", str(Path(migrations.__file__).resolve().parent))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    config.attributes["bowhead_target"] = target
    return config


def _store(path: Path, blobs: Path) -> ArtifactStore:
    return ArtifactStore(db_path=str(path), backend=LocalBackend(root_dir=blobs))


def _seed_space(db: sqlite3.Connection) -> None:
    """一个空间 + 会话 + turn + canvas，供梯子各级命中。"""
    db.execute(
        "INSERT INTO sessions(id,workspace_id,user_id,started_at,last_active_at) VALUES ('s1','ws_alpha','u1','2026-09-01','2026-09-01')",
    )
    db.execute(
        "INSERT INTO turns(id,session_id,seq,user_text,started_at) VALUES ('t1','s1',1,'hi','2026-09-01')",
    )
    db.execute(
        "INSERT INTO canvases(id,session_id,workspace_id,user_id,created_at,updated_at) VALUES ('c1','s1','ws_alpha','u1','2026-09-01','2026-09-01')",
    )


# ───────────────────────── Migration itself ────────────────────────────────


def test_upgrade_adds_nullable_column_keeps_rows_and_is_repeatable(tmp_path: Path) -> None:
    """存量库：加列、老行留 NULL（=「推不出归属」）、重复执行幂等。"""
    path = tmp_path / "core.db"
    config = _config(path)
    command.upgrade(config, MIGRATION.down_revision)
    with sqlite3.connect(path) as db:
        cols = {r[1] for r in db.execute("PRAGMA table_info(artifacts)")}
        assert "workspace_id" not in cols, "前置不成立：升级前就有本列，这个测试证明不了任何事"
        db.execute(
            "INSERT INTO artifacts(id,filename,mime,size,source,path,created_at) VALUES (?,?,?,?,?,?,?)",
            ("art_legacy", "old.csv", "text/csv", 12, "bi_fetch_rows", "/tmp/old.csv", "2026-09-01"),
        )

    command.upgrade(config, "head")
    command.upgrade(config, "head")  # Idempotent: second call must not fail
    command.stamp(config, MIGRATION.down_revision)
    # Replay only this revision, then restore ledger to head: later historical revisions (e.g. 0063 table creation, MCP Prompt structural preflight) may not rerun on upgraded databases.
    command.upgrade(config, MIGRATION.revision)  # Replaying the same revision must not fail either
    with sqlite3.connect(path) as db:
        # Replay really reaches this revision (checking version after stamp head is trivially true and meaningless).
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (MIGRATION.revision,)
    command.stamp(config, "head")

    with sqlite3.connect(path) as db:
        info = {r[1]: (r[2].upper(), r[3], r[4]) for r in db.execute("PRAGMA table_info(artifacts)")}
        assert "workspace_id" in info
        # ★ Must be nullable with no default: NOT NULL DEFAULT '' fabricates real ownership from unresolved attribution,
        # making batch 4's measured NULL proportion always 0 (false success).
        assert info["workspace_id"] == ("TEXT", 0, None)
        assert db.execute("SELECT workspace_id FROM artifacts WHERE id='art_legacy'").fetchone() == (None,)
        idx = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        assert "idx_artifacts_workspace" in idx


def test_both_maintenance_paths_agree(tmp_path: Path) -> None:
    """双路维护：alembic 链和 store.initialize() 的 ALTER 循环各自都能补上本列。

    ⚠ 路 B 必须从**缺列的老表**起步——直接建新库测不到 ALTER 那一路
    （新库的列来自 SCHEMA 的 CREATE TABLE，把 ALTER 整条删掉照样绿）。
    """
    via_alembic = tmp_path / "alembic.db"
    command.upgrade(_config(via_alembic), "head")
    with sqlite3.connect(via_alembic) as db:
        assert "workspace_id" in {r[1] for r in db.execute("PRAGMA table_info(artifacts)")}
    # initialize must not fail on an already migrated database (ALTER loop must be idempotent).
    asyncio.run(_store(via_alembic, tmp_path / "b1").initialize())

    via_store = tmp_path / "store.db"
    with sqlite3.connect(via_store) as db:
        db.execute(
            "CREATE TABLE artifacts ("
            "id TEXT PRIMARY KEY, filename TEXT NOT NULL, mime TEXT NOT NULL, size INTEGER NOT NULL, "
            "source TEXT NOT NULL, path TEXT NOT NULL, canvas_id TEXT, created_by TEXT, created_at TEXT NOT NULL)"
        )
        db.execute(
            "INSERT INTO artifacts(id,filename,mime,size,source,path,created_at) VALUES (?,?,?,?,?,?,?)",
            ("art_old", "legacy.csv", "text/csv", 9, "bi_fetch_rows", "/tmp/legacy.csv", "2026-08-01"),
        )
        assert "workspace_id" not in {r[1] for r in db.execute("PRAGMA table_info(artifacts)")}

    asyncio.run(_store(via_store, tmp_path / "b2").initialize())
    with sqlite3.connect(via_store) as db:
        assert "workspace_id" in {r[1] for r in db.execute("PRAGMA table_info(artifacts)")}
        assert db.execute("SELECT workspace_id FROM artifacts WHERE id='art_old'").fetchone() == (None,)


@pytest.mark.parametrize("target", ["files", "doc_types", "extracted_records", "extracted_rows", "per_row_jobs", "team_templates", "matrix"])
def test_non_core_target_untouched(tmp_path: Path, target: str) -> None:
    path = tmp_path / f"{target}.db"
    command.upgrade(_config(path, target), "head")
    with sqlite3.connect(path) as db:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
        assert "artifacts" not in tables, f"{target} 目标不该有 artifacts 表"


def test_downgrade_refuses_instead_of_silently_dropping(tmp_path: Path) -> None:
    """生产不 downgrade：回滚靠同版本库归档。显式拒绝好过悄悄改结构。"""
    with pytest.raises(NotImplementedError):
        MIGRATION.downgrade()


# ───────────────────────── Write sites ─────────────────────────────────────


@pytest.mark.asyncio
async def test_put_bytes_resolves_workspace_from_session(tmp_path: Path) -> None:
    path = tmp_path / "core.db"
    command.upgrade(_config(path), "head")
    with sqlite3.connect(path) as db:
        _seed_space(db)

    store = _store(path, tmp_path / "blob")
    await store.initialize()

    via_session = await store.put_bytes(content=b"a", filename="a.csv", mime="text/csv", source="bi_fetch_rows", created_by="u1", session_id="s1")
    assert (await store.get(via_session.id)).workspace_id == "ws_alpha"

    via_turn = await store.put_bytes(content=b"b", filename="b.csv", mime="text/csv", source="bi_fetch_rows", created_by="u1", created_by_turn="t1")
    assert (await store.get(via_turn.id)).workspace_id == "ws_alpha"

    via_canvas = await store.put_bytes(content=b"c", filename="c.png", mime="image/png", source="canvas_export", created_by="u1", canvas_id="c1")
    assert (await store.get(via_canvas.id)).workspace_id == "ws_alpha"


@pytest.mark.asyncio
async def test_put_bytes_keeps_null_when_unattributable(tmp_path: Path) -> None:
    """★ 推不出就是 NULL。这条是批4 判据数据的地基，不能被任何兜底值顶掉。"""
    path = tmp_path / "core.db"
    command.upgrade(_config(path), "head")
    store = _store(path, tmp_path / "blob")
    await store.initialize()

    orphan = await store.put_bytes(content=b"x", filename="x.txt", mime="text/plain", source="web_search", created_by="u1")
    assert (await store.get(orphan.id)).workspace_id is None

    # Missing session ⇒ still NULL; never invent ownership from session_id.
    dangling = await store.put_bytes(content=b"y", filename="y.txt", mime="text/plain", source="web_search", created_by="u1", session_id="s_missing")
    assert (await store.get(dangling.id)).workspace_id is None


@pytest.mark.asyncio
async def test_explicit_workspace_id_overrides_derivation(tmp_path: Path) -> None:
    """显式归属（presign 那类手上就有 ctx 的路）优先于反推。"""
    path = tmp_path / "core.db"
    command.upgrade(_config(path), "head")
    with sqlite3.connect(path) as db:
        _seed_space(db)
    store = _store(path, tmp_path / "blob")
    await store.initialize()

    art = await store.put_bytes(
        content=b"z", filename="z.txt", mime="text/plain", source="upload", created_by="u1", session_id="s1", workspace_id="ws_explicit"
    )
    assert (await store.get(art.id)).workspace_id == "ws_explicit"


@pytest.mark.asyncio
async def test_store_without_session_tables_degrades_to_null(tmp_path: Path) -> None:
    """store-only 老库没有 sessions/turns/canvases：归属推不出，但**写入不能失败**。"""
    path = tmp_path / "store_only.db"
    store = _store(path, tmp_path / "blob")
    await store.initialize()  # Creates only the artifacts-related tables
    art = await store.put_bytes(content=b"q", filename="q.txt", mime="text/plain", source="upload", created_by="u1", session_id="s1")
    assert (await store.get(art.id)).workspace_id is None


# ───────────────────────── Backfill ladder ─────────────────────────────────


def _backfill(path: Path) -> int:
    with sqlite3.connect(path) as db:
        cur = db.execute(f"UPDATE artifacts AS a SET workspace_id = ({LADDER}) WHERE a.workspace_id IS NULL AND ({LADDER}) IS NOT NULL")  # noqa: S608
        return cur.rowcount


def test_backfill_ladder_fills_only_what_it_can_prove(tmp_path: Path) -> None:
    path = tmp_path / "core.db"
    command.upgrade(_config(path), "head")
    with sqlite3.connect(path) as db:
        _seed_space(db)
        rows = [
            ("art_turn", "t1", None),  # ② turn → session
            ("art_canvas", None, "c1"),  # ③ canvas → session
            ("art_orphan", None, None),  # Cannot infer attribution
        ]
        for aid, turn, canvas in rows:
            db.execute(
                "INSERT INTO artifacts(id,filename,mime,size,source,path,created_at,created_by_turn,canvas_id) VALUES (?,?,?,?,?,?,?,?,?)",
                (aid, f"{aid}.csv", "text/csv", 1, "bi_fetch_rows", f"/tmp/{aid}", "2026-09-01", turn, canvas),
            )
        # ① Upload binding; empty string (team-level upload) must **not** count as attribution.
        db.execute(
            "INSERT INTO artifacts(id,filename,mime,size,source,path,created_at) VALUES ('art_bind','u.csv','text/csv',1,'upload','/tmp/u','2026-09-01')",
        )
        db.execute(
            "INSERT INTO artifacts(id,filename,mime,size,source,path,created_at) VALUES ('art_team','tm.csv','text/csv',1,'kb_upload','/tmp/tm','2026-09-01')",
        )
        db.execute(
            "INSERT INTO artifact_upload_bindings(artifact_id,workspace_id,user_id,created_at) VALUES ('art_bind','ws_bind','u1','2026-09-01')",
        )
        db.execute(
            "INSERT INTO artifact_upload_bindings(artifact_id,workspace_id,user_id,created_at) VALUES ('art_team','','u1','2026-09-01')",
        )

    _backfill(path)
    with sqlite3.connect(path) as db:
        got = dict(db.execute("SELECT id, workspace_id FROM artifacts"))
    assert got["art_bind"] == "ws_bind"
    assert got["art_turn"] == "ws_alpha"
    assert got["art_canvas"] == "ws_alpha"
    # ★ Empty-string binding = team upload, not space ID ⇒ must remain NULL (same semantics as files.py:644).
    assert got["art_team"] is None
    assert got["art_orphan"] is None


def test_backfill_is_idempotent_and_never_overwrites(tmp_path: Path) -> None:
    """可重跑：第二次匹配 0 行；已有归属（写入点落的）不被反推覆盖。"""
    path = tmp_path / "core.db"
    command.upgrade(_config(path), "head")
    with sqlite3.connect(path) as db:
        _seed_space(db)
        db.execute(
            "INSERT INTO artifacts(id,filename,mime,size,source,path,created_at,created_by_turn) "
            "VALUES ('art_turn','a.csv','text/csv',1,'bi_fetch_rows','/tmp/a','2026-09-01','t1')",
        )
        # Writer already stored **different** attribution; backfill must not change it.
        db.execute(
            "INSERT INTO artifacts(id,filename,mime,size,source,path,created_at,created_by_turn,workspace_id) "
            "VALUES ('art_kept','b.csv','text/csv',1,'bi_fetch_rows','/tmp/b','2026-09-01','t1','ws_written')",
        )

    assert _backfill(path) == 1
    assert _backfill(path) == 0, "重跑应该匹配 0 行（幂等）"
    with sqlite3.connect(path) as db:
        got = dict(db.execute("SELECT id, workspace_id FROM artifacts"))
    assert got["art_turn"] == "ws_alpha"
    assert got["art_kept"] == "ws_written"


def test_missing_session_row_does_not_fall_through_to_a_later_rung(tmp_path: Path) -> None:
    """★ 与 files.py 同形：session_id 解析出来了但 sessions 行不在 ⇒ 归属为空，
    **不再回退下一级**。口径不一致的话，回填出的分布和今天的授权推导就不是同一把尺子。
    """
    path = tmp_path / "core.db"
    command.upgrade(_config(path), "head")
    with sqlite3.connect(path) as db:
        _seed_space(db)
        # Turn points to a missing session while linked canvas **can** resolve attribution.
        db.execute("INSERT INTO turns(id,session_id,seq,user_text,started_at) VALUES ('t_ghost','s_ghost',1,'x','2026-09-01')")
        db.execute(
            "INSERT INTO artifacts(id,filename,mime,size,source,path,created_at,created_by_turn,canvas_id) "
            "VALUES ('art_ghost','g.csv','text/csv',1,'bi_fetch_rows','/tmp/g','2026-09-01','t_ghost','c1')",
        )
    _backfill(path)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT workspace_id FROM artifacts WHERE id='art_ghost'").fetchone() == (None,)


# ───────────────────────── Write-site inventory ────────────────────────────


def test_every_artifact_writer_decides_about_workspace_id() -> None:
    """Every shared artifact SQL writer declares Space or explicit team attribution."""
    writers: dict[str, str] = {}
    for path in (REPO_ROOT / "packages").rglob("*.py"):
        rel = path.relative_to(REPO_ROOT).as_posix()
        if "/tests/" in rel or "/migrations/versions/" in rel:
            continue
        source = path.read_text(encoding="utf-8")
        if re.search(r"INSERT\s+INTO\s+artifacts", source, re.I):
            writers[rel] = source

    assert writers, "The inventory must find existing artifact writers"
    missing = sorted(rel for rel, src in writers.items() if "workspace_id" not in src)
    assert missing == [], f"Artifact writers must declare Space or team attribution: {missing}"


#: **Write-side** attribution inputs, matching `artifact_store._resolve_write_workspace_id` parameters one for one:
#: explicit `workspace_id`, or `session_id` / `created_by_turn` / `canvas_id` resolving to a session.
_ATTRIBUTION_INPUTS = frozenset({"workspace_id", "session_id", "created_by_turn", "canvas_id"})

#: Product write surfaces: scan only these two source trees.
#: ⚠ **Exclude `bowhead_cli`**: `composer_a_rollback_drill.py` is an operational drill harness;
#: its artifacts reach no user read surface, and it separately writes `artifact_upload_bindings` (ladder ①) for attribution.
_PRODUCT_SRC = ("packages/bowhead_core/src/", "packages/bowhead_gateway/src/")


def _put_bytes_calls(source: str) -> list[ast.Call]:
    """用 **AST** 找 `*.put_bytes(...)` 调用点。

    ★ 第一版用文本 find + 括号配平，2026-09-17 实测**8 个全是假阳性**：
      `artifact_store.py:635` 与 `image_gen_tool.py:6` 命中的是**注释/docstring 里的散文**
      （`image_gen_tool` 真正的调用点其实老老实实传了 `session_id=` + `created_by_turn=`），
      其余命中 `insert_staged_artifacts_in_transaction`——那一族的归属来自**入参对象**
      `staged.session_id`，压根不走关键字实参。
      判据够不着会被当成「有缺口」，天天报假警的守卫下一个人就把它关掉了。
    ⇒ 改用 AST：散文和函数定义**结构上**就不是 `ast.Call`，不需要任何豁免名单。
    """
    return [
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "put_bytes"
    ]


def test_every_put_bytes_call_site_supplies_an_attribution_input() -> None:
    """★★ 判据轴改了：按**调用点**清点，不再按「文件里有没有 INSERT INTO artifacts」。

    2026-09-17 review 抓到旧轴的结构性盲区：`kb.py` 三处 `put_bytes`（`:737`/`:909`/`:986`）
    既不传 `workspace_id` 也不传 `session_id`，归属恒 NULL ⇒ 翻闸后经 `/api/v1/kb/files`
    上传的文件与团队级 bundle **全部 403**。而 `grep -c "INSERT INTO artifacts" kb.py` == **0**
    ⇒ 旧守卫**永远看不见这个文件**，再加多少写入点它也不会红。
    [[feedback_gate_that_cannot_parse_must_not_pass]]

    ⇒ 两条轴都留着：上面那条管「写 SQL 的文件有没有表态」，这条管「**调用方给没给输入**」。

    判据 = 每个调用点至少给一个归属输入（与 `_resolve_write_workspace_id` 的入参同源）。
    只要求「有输入」而不是「必须有 `workspace_id`」：`created_by_turn=` / `canvas_id=`
    同样能经梯子解析出归属，强行要求 `workspace_id=` 会把它们误判成缺口。

    ⚠ `insert_staged_artifacts_in_transaction` **不在**本轴内：它的归属来自入参
    `staged.session_id`（由 `artifact_store` 内部解析），调用点看不到也不该看到。
    """
    offenders: list[str] = []
    scanned = 0
    for path in (REPO_ROOT / "packages").rglob("*.py"):
        rel = path.relative_to(REPO_ROOT).as_posix()
        if "/tests/" in rel or not any(rel.startswith(root) for root in _PRODUCT_SRC):
            continue
        for call in _put_bytes_calls(path.read_text(encoding="utf-8")):
            scanned += 1
            if {kw.arg for kw in call.keywords} & _ATTRIBUTION_INPUTS:
                continue
            offenders.append(f"{rel}:{call.lineno}")

    # Positive control: scanner actually parsed call sites (otherwise zero offenders just means nothing was read).
    assert scanned > 5, f"put_bytes 调用点没扫到几个，判据轴坏了：scanned={scanned}"
    assert offenders == [], "这些 `put_bytes` **调用点**一个归属输入都没给（归属会恒为 NULL，翻闸后 403）：\n" + "\n".join(offenders)
