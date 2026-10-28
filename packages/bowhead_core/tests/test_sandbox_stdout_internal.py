"""沙箱 stdout 不占用户面文件清单的位置（artifact 照落，只是不算交付物）。

背景（116 干净窗口实测，2026-09-06）：session 文件清单里 26.8% 是
`sandbox-stdout.txt`（中位数 887 字节，12% 不到 100 字节，内容多为 `done` /
`start` / 中间 DataFrame dump），而同一批清单里真正的交付物只有 1.9%。

stdout **必须继续落 artifact**：result 里内联的 stdout 只是有界的模型预览，完整文本
只以 artifact 形式存在，且 `answer_decomposer._sandbox_output_artifact_id` 拿它当
stdout 里那些数字的溯源锚点。所以能改的只有「算不算用户的交付物」。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import aiosqlite
import pytest
from bowhead_core.artifact_store import ArtifactStore, LocalBackend
from bowhead_core.results_artifact_policy import (
    SANDBOX_STDOUT_PRODUCER,
    is_results_internal_artifact,
    results_internal_artifact_sql,
)
from bowhead_core.skill_bridge import _wrap_sandbox_stdout_marking

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
async def store(tmp_path: Path) -> ArtifactStore:
    db_path = str(tmp_path / "art.db")
    s = ArtifactStore(db_path=db_path, backend=LocalBackend(root_dir=tmp_path / "blob"))
    await s.initialize()
    async with aiosqlite.connect(db_path) as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY, workspace_id TEXT, user_id TEXT,
                started_at TEXT, last_active_at TEXT
            );
            CREATE TABLE IF NOT EXISTS turns (id TEXT PRIMARY KEY, session_id TEXT);
            CREATE TABLE IF NOT EXISTS turn_attachments (
                turn_id TEXT, artifact_id TEXT, seq INTEGER
            );
            CREATE TABLE IF NOT EXISTS canvases (id TEXT PRIMARY KEY, session_id TEXT);
            CREATE TABLE IF NOT EXISTS session_attachments (
                session_id TEXT NOT NULL, artifact_id TEXT NOT NULL,
                pinned_at TEXT, detached_at TEXT,
                PRIMARY KEY(session_id, artifact_id)
            );
            INSERT OR IGNORE INTO sessions(id, workspace_id, user_id) VALUES ('s_1', '1', 'u1');
        """)
        await db.commit()
    return s


async def _sandbox_pair(store: ArtifactStore) -> tuple[str, str]:
    """造一次真实沙箱调用的两个产物：stdout + 用户真要的 csv。"""
    stdout = await store.put_bytes(
        content=b"done\n",
        filename="sandbox-stdout.txt",
        mime="text/plain",
        source="python_sandbox",
        created_by="u1",
        session_id="s_1",
    )
    table = await store.put_bytes(
        content=b"prov,rev\nGD,1\n",
        filename="prov_rank.csv",
        mime="text/csv",
        source="python_sandbox",
        created_by="u1",
        session_id="s_1",
    )
    return stdout.id, table.id


@pytest.mark.asyncio
async def test_marked_stdout_leaves_the_user_facing_snapshot(store: ArtifactStore) -> None:
    """★ 本次改动的用户可见判据：标过之后，session 清单里只剩真产物。"""
    stdout_id, table_id = await _sandbox_pair(store)

    before = await store.list_for_session(session_id="s_1", user_id="u1", results_visible_only=True)
    assert {a.id for a in before} == {stdout_id, table_id}, "标记前两份都应在清单里（否则这个测试证明不了任何事）"

    assert await store.mark_internal_producer(stdout_id, SANDBOX_STDOUT_PRODUCER) is True

    after = await store.list_for_session(session_id="s_1", user_id="u1", results_visible_only=True)
    assert {a.id for a in after} == {table_id}, "stdout 应退出用户面清单，真产物必须留下"


@pytest.mark.asyncio
async def test_marking_hides_from_snapshot_but_never_deletes(store: ArtifactStore) -> None:
    """数据没丢，只是不算交付物——不传 results_visible_only 时仍然列得出、读得到。"""
    stdout_id, table_id = await _sandbox_pair(store)
    await store.mark_internal_producer(stdout_id, SANDBOX_STDOUT_PRODUCER)

    everything = await store.list_for_session(session_id="s_1", user_id="u1")
    assert {a.id for a in everything} == {stdout_id, table_id}

    art = await store.get(stdout_id)
    assert art is not None and art.deleted_at is None
    assert await store.open_read(art) == b"done\n", "溯源锚点必须仍然读得出来"


@pytest.mark.asyncio
async def test_mark_internal_producer_refuses_arbitrary_values(store: ArtifactStore) -> None:
    """不能退化成通用 setter —— 那等于给了任意调用方一个「把文件藏起来」的口子。"""
    stdout_id, _ = await _sandbox_pair(store)
    with pytest.raises(ValueError, match="internal producer"):
        await store.mark_internal_producer(stdout_id, "deliverable")
    assert await store.mark_internal_producer("art_does_not_exist", SANDBOX_STDOUT_PRODUCER) is False


def test_python_predicate_and_sql_twin_agree_on_the_new_producer() -> None:
    """两个判据必须等价：SQL 孪生体漏掉新常量 = 清单照旧被淹。"""
    artifact = SimpleNamespace(
        id="art_x",
        source="python_sandbox",
        kind="sandbox_output",
        producer_kind=SANDBOX_STDOUT_PRODUCER,
    )
    assert is_results_internal_artifact(artifact) is True
    clause, params = results_internal_artifact_sql("a")
    assert SANDBOX_STDOUT_PRODUCER in params, f"SQL 参数里没有新的内部生产者: {params}"
    assert "producer_kind" in clause


# ── wrapper ────────────────────────────────────────────────────────────────


class _RecordingStore:
    def __init__(self) -> None:
        self.marked: list[tuple[str, str]] = []

    async def mark_internal_producer(self, artifact_id: str, producer_kind: str) -> bool:
        self.marked.append((artifact_id, producer_kind))
        return True


def _handler_returning(result: Any) -> Any:
    async def _h(_args: dict[str, Any], _ctx: Any) -> Any:
        return result

    return _h


@pytest.mark.asyncio
async def test_wrapper_marks_only_the_stdout_output() -> None:
    """判据是 data_role，不是文件名。

    正对照就在同一份结果里：`sandbox-stdout.txt` 这个**名字**出现在一个没有
    data_role 的产物上（用户自己在 workdir 里写的同名文件），它绝不能被标。
    """
    recording = _RecordingStore()
    result = {
        "ok": True,
        "output_artifacts": [
            {"artifact_id": "art_real_stdout", "name": "sandbox-stdout-data-1.txt", "data_role": "stdout"},
            {"artifact_id": "art_user_file", "name": "sandbox-stdout.txt"},
            {"artifact_id": "art_table", "name": "prov_rank.csv", "data_role": "structured"},
        ],
    }
    wrapped = _wrap_sandbox_stdout_marking(_handler_returning(result))
    got = await wrapped({}, SimpleNamespace(artifact_store=recording))

    assert got is result, "包装器不得改写工具结果"
    assert recording.marked == [("art_real_stdout", SANDBOX_STDOUT_PRODUCER)]


@pytest.mark.asyncio
async def test_wrapper_is_fail_soft() -> None:
    """标不上只是清单里多一条，绝不能把已经成功的沙箱调用打挂。"""

    class _Boom:
        async def mark_internal_producer(self, *_a: Any, **_k: Any) -> bool:
            raise RuntimeError("db down")

    result = {"ok": True, "output_artifacts": [{"artifact_id": "art_1", "data_role": "stdout"}]}
    wrapped = _wrap_sandbox_stdout_marking(_handler_returning(result))
    assert await wrapped({}, SimpleNamespace(artifact_store=_Boom())) is result

    # A ctx without artifact_store (non-sandbox skill / test stub) must not crash either.
    assert await wrapped({}, SimpleNamespace()) is result


@pytest.mark.asyncio
async def test_wrapper_ignores_results_without_sandbox_outputs() -> None:
    """挂在 skill_invoke 上会看到所有 skill 的结果——没有 stdout 产物时必须完全不动手。"""
    recording = _RecordingStore()
    for result in ({"ok": True}, {"ok": True, "output_artifacts": []}, {"ok": False}, "not-a-dict"):
        wrapped = _wrap_sandbox_stdout_marking(_handler_returning(result))
        await wrapped({}, SimpleNamespace(artifact_store=recording))
    assert recording.marked == []


def test_wrapper_is_actually_wired_into_registration() -> None:
    """★ 防「写好了没接线」。

    本 sprint 的调研在这个子系统里数出六套「本该管住量」的机制全是死的，其中两套
    （`artifact_stdout` 表、`turn_attachments` 表）都是**建好了、有测试、但生产没有
    写入方**。上面那些 wrapper 单测直接调 `_wrap_sandbox_stdout_marking`，即使它从
    未被注册也照样全绿 —— 所以必须单独钉住「它确实挂在沙箱工具上」。

    判据用 AST 而不是跑一次真注册：`build_skill_tools` 会去扫真实 catalog，
    在单测里跑它既慢又把无关的 skill 解析拖进来。
    """
    import ast
    import inspect

    from bowhead_core import skill_bridge

    tree = ast.parse(inspect.getsource(skill_bridge))
    register = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "register_skill_tools")
    called = {node.func.id for node in ast.walk(register) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
    assert "_wrap_sandbox_stdout_marking" in called, (
        "沙箱 stdout 补标 wrapper 没有挂进 register_skill_tools —— 包装器存在但没接线，用户面清单照旧被 stdout 淹"
    )
