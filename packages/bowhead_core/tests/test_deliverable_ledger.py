"""said-not-done WP-1 —— 交付物账单。

账单是 Seam B 的**事实来源**：它说"本轮 files=0"，闸才敢拦住「PPT 已生成」那句谎报。
所以它的每一项取数口径都得是真的，不能是 fake 喂出来的形状
（[[feedback_fake_looser_than_real_collaborator]]：替身比真实现宽松 = 假绿）。

⇒ `files` 一项用**真 ArtifactStore** 测（不是 mock），因为它是 19 次漏做的对账项。
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from bowhead_core.artifact_store import ArtifactStore, LocalBackend
from bowhead_core.deliverable_ledger import (
    DeliverableLedger,
    build_deliverable_ledger,
    collect_ok_tools,
    produced_facts,
    produced_files_note,
)


@pytest.fixture
async def store() -> ArtifactStore:
    tmp = Path(tempfile.mkdtemp())
    s = ArtifactStore(backend=LocalBackend(root_dir=tmp / "files"), db_path=tmp / "t.db")
    await s.initialize()
    return s


def _inv(tool: str, *, ok: bool = True, nested: list | None = None):
    return SimpleNamespace(tool_name=tool, result={"ok": ok}, nested=nested or [])


# ── collect_ok_tools: nested invocations are critical ────────────────────────────────────


def test_ok_tools_collects_top_level() -> None:
    assert collect_ok_tools([_inv("bi_query"), _inv("python_sandbox")]) == frozenset({"bi_query", "python_sandbox"})


def test_ok_tools_walks_nested_delegated_turn() -> None:
    """★ 委派轮：peer 的真实调用挂在 inv.nested 上。

    只读顶层的话，这个函数会在**恰恰最需要它的委派轮上全瞎** ——
    `chat_fabrication_guard` 踩过同一个坑（trace t_0e818cb5）。
    """
    spawn = _inv(
        "subagent_spawn",
        nested=[_inv("python_sandbox"), _inv("add_chart")],
    )
    got = collect_ok_tools([spawn])
    assert "python_sandbox" in got, "nested 没走到 → 委派轮全瞎"
    assert got == frozenset({"subagent_spawn", "python_sandbox", "add_chart"})


def test_ok_tools_recurses_deeply() -> None:
    deep = _inv("a", nested=[_inv("b", nested=[_inv("c")])])
    assert collect_ok_tools([deep]) == frozenset({"a", "b", "c"})


def test_ok_tools_excludes_failed_calls() -> None:
    assert collect_ok_tools([_inv("skill_invoke", ok=False)]) == frozenset()


def test_ok_tools_keeps_unknown_shape_conservatively() -> None:
    """结果不是 dict（老形状）时保守算成功。

    这里漏报比误报危险：误以为"没调过工具"会让闸去拦一个其实干了活的轮次。
    """
    odd = SimpleNamespace(tool_name="legacy_tool", result="whatever", nested=[])
    assert collect_ok_tools([odd]) == frozenset({"legacy_tool"})


def test_ok_tools_empty_input() -> None:
    assert collect_ok_tools(None) == frozenset()
    assert collect_ok_tools([]) == frozenset()


# ── files: use real ArtifactStore ────────────────────────────────────────────────


async def test_files_counts_turn_deliverables(store: ArtifactStore) -> None:
    await store.put_bytes(
        content=b"x",
        filename="Bowhead Canvas.pdf",
        mime="application/pdf",
        kind="deliverable",
        created_by="u_1",
        created_by_turn="t_1",
    )
    led = await build_deliverable_ledger(turn_id="t_1", artifact_store=store)
    assert led.files == 1
    assert led.produced_anything is True


async def test_delivered_files_carry_their_identifiers_and_data_files_do_not(store: ArtifactStore) -> None:
    report = await store.put_bytes(
        content=b"x", filename="report.html", mime="text/html", kind="deliverable", created_by="u_1", created_by_turn="t_1"
    )
    await store.put_bytes(content=b"y", filename="rows.csv", mime="text/csv", kind="sandbox_output", created_by="u_1", created_by_turn="t_1")
    led = await build_deliverable_ledger(turn_id="t_1", artifact_store=store)
    # Only what was delivered is named with its identifier; a data or work file is counted and found by lookup.
    assert led.delivered == ((report.id, "report.html"),)
    assert led.files >= 1


async def test_files_zero_is_the_prod_miss_shape(store: ArtifactStore) -> None:
    """07-23 那两轮：skill 跑成功、答案说"PDF 已经导出好了"、库里 0 个交付物。"""
    led = await build_deliverable_ledger(
        turn_id="t_miss",
        artifact_store=store,
        invocations=[_inv("skill_invoke")],
    )
    assert led.files == 0
    assert led.ok_tools == frozenset({"skill_invoke"})  # Tier 2 still detects attempted work.
    assert led.produced_anything is False


async def test_files_scoped_to_this_turn(store: ArtifactStore) -> None:
    await store.put_bytes(
        content=b"x",
        filename="d.pptx",
        mime="application/vnd.openxmlformats-officedocument.presentationml.presentation",
        kind="deliverable",
        created_by="u_1",
        created_by_turn="t_other",
    )
    led = await build_deliverable_ledger(turn_id="t_1", artifact_store=store)
    assert led.files == 0


def _reuse(artifact_id, *, ok=True):
    return SimpleNamespace(tool_name="python_exec", arguments={"inputs": {"source": {"artifact_id": artifact_id}}}, result={"ok": ok}, nested=[])


async def test_reused_source_is_scoped_to_exact_successful_input_and_not_counted_as_new(tmp_path):
    from test_results_artifact_query import _store

    store = await _store(tmp_path)
    await store.put_bytes(
        content=b"pdf",
        filename="output.pdf",
        mime="application/pdf",
        source="python_sandbox",
        kind="sandbox_output",
        created_by="user_1",
        created_by_turn="t_new",
    )
    ledger = await build_deliverable_ledger(
        turn_id="t_new", user_id="user_1", session_id="session_1", artifact_store=store, invocations=[_reuse("art_deliverable")]
    )
    assert ledger.files == 1 and ledger.file_names == ("output.pdf",)
    assert ledger.reused_files == 1 and ledger.reused_file_names == ("交付报告.pptx",)


@pytest.mark.parametrize(
    "tool,args",
    [
        ("python_exec", {"input": {"inputs": {"source": "art_deliverable"}}}),
        ("skill_invoke", {"name": "python_sandbox", "input": {"inputs": {"source": {"artifact_id": "art_deliverable"}}}}),
    ],
)
async def test_supported_sandbox_input_shapes_establish_exact_reuse(tmp_path, tool, args):
    from test_results_artifact_query import _store

    store = await _store(tmp_path)
    invocation = SimpleNamespace(tool_name=tool, arguments=args, result={"ok": True}, nested=[])
    ledger = await build_deliverable_ledger(turn_id="t_new", user_id="user_1", session_id="session_1", artifact_store=store, invocations=[invocation])
    assert ledger.reused_files == 1


@pytest.mark.parametrize("artifact_id", ["art_other", "art_upload", "art_internal", "art_pending", "art_missing"])
async def test_reused_source_cannot_expose_other_owner_upload_private_or_unready_files(tmp_path, artifact_id):
    from test_results_artifact_query import _store

    store = await _store(tmp_path)
    ledger = await build_deliverable_ledger(
        turn_id="t_new", user_id="user_1", session_id="session_1", artifact_store=store, invocations=[_reuse(artifact_id)]
    )
    assert ledger.reused_files == 0 and not ledger.reused_file_names


@pytest.mark.parametrize("session_id,ok", [("session_other", True), ("session_1", False)])
async def test_other_session_or_failed_sandbox_cannot_establish_reuse(tmp_path, session_id, ok):
    from test_results_artifact_query import _store

    store = await _store(tmp_path)
    ledger = await build_deliverable_ledger(
        turn_id="t_new", user_id="user_1", session_id=session_id, artifact_store=store, invocations=[_reuse("art_deliverable", ok=ok)]
    )
    assert ledger.reused_files == 0


async def test_reuse_read_failure_preserves_new_output_verification(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock

    from test_results_artifact_query import _store

    store = await _store(tmp_path)
    await store.put_bytes(
        content=b"pdf",
        filename="output.pdf",
        mime="application/pdf",
        source="python_sandbox",
        kind="sandbox_output",
        created_by="user_1",
        created_by_turn="t_new",
    )
    monkeypatch.setattr(store, "get_result_artifact_for_scope", AsyncMock(side_effect=OSError("Read unavailable")))
    ledger = await build_deliverable_ledger(
        turn_id="t_new", user_id="user_1", session_id="session_1", artifact_store=store, invocations=[_reuse("art_deliverable")]
    )
    assert ledger.files_verified and ledger.files == 1 and not ledger.reused_files_verified


async def test_results_workspace_counts_visible_sandbox_outputs(store: ArtifactStore) -> None:
    for filename, mime, kind in (
        ("sales.csv", "text/csv", "sandbox_output"),
        ("chart.png", "image/png", "sandbox_output"),
        ("report.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "deliverable"),
    ):
        await store.put_bytes(
            content=b"x",
            filename=filename,
            mime=mime,
            source="python_sandbox",
            kind=kind,
            created_by="u_1",
            created_by_turn="t_multi",
        )

    led = await build_deliverable_ledger(turn_id="t_multi", artifact_store=store)
    assert led.files == 3
    assert set(led.file_names) == {"sales.csv", "chart.png", "report.docx"}


async def test_file_names_from_real_store(store: ArtifactStore) -> None:
    await store.put_bytes(
        content=b"x",
        filename="Bowhead Canvas.pdf",
        mime="application/pdf",
        kind="deliverable",
        created_by="u_1",
        created_by_turn="t_1",
    )
    led = await build_deliverable_ledger(turn_id="t_1", artifact_store=store)
    assert led.file_names == ("Bowhead Canvas.pdf",)


def test_file_names_capped() -> None:
    led = DeliverableLedger(files=3, file_names=("a.pdf", "b.pdf"))
    assert len(led.file_names) <= 10


# ── sections / memories ─────────────────────────────────────────────────────


async def test_sections_from_canvas_store() -> None:
    cs = SimpleNamespace(count_sections_for_turn=lambda tid: _async(11))
    led = await build_deliverable_ledger(turn_id="t_1", canvas_store=cs)
    assert led.sections == 11
    assert led.produced_anything is True


async def test_sections_count_in_place_revision_real_store(tmp_path: Path) -> None:
    """116 dogfood R11（`an observed task`）：续轮只就地改上一轮的段、一个新段都不 append。
    只数 append 会判 0 ⇒ 答案被追加「本轮没有内容落到画布上」，而画布确实改了。用**真** CanvasStore 测。"""
    from bowhead_core.canvas_store import CanvasStore

    cs = CanvasStore(tmp_path / "canvas.db")
    await cs.initialize()
    cv = await cs.ensure_canvas(session_id="s1", workspace_id="w", user_id="u")
    sec = await cs.append_section(canvas_id=cv.id, kind="narrative", payload={"markdown": "旧"}, by_turn_id="t_prev")
    await cs.patch_section(section_id=sec.id, patch={"markdown": "新"}, expected_rev=1, by_turn_id="t_now")

    led = await build_deliverable_ledger(turn_id="t_now", canvas_store=cs)
    assert led.sections == 1 and led.produced_anything is True
    quiet = await build_deliverable_ledger(turn_id="t_chat_only", canvas_store=cs)
    assert quiet.sections == 0  # Untouched canvas still yields 0; gate still catches false completion claims.


async def test_memories_only_when_this_turn_wrote_it() -> None:
    """memory_notes 是一用户一行（upsert），所以只能看最后写入方是不是本 turn。"""
    mine = SimpleNamespace(get=lambda **kw: _async(SimpleNamespace(source_turn_id="t_1")))
    other = SimpleNamespace(get=lambda **kw: _async(SimpleNamespace(source_turn_id="t_prev")))

    led = await build_deliverable_ledger(turn_id="t_1", user_id="u_1", memory_notes_store=mine)
    assert led.memories == 1

    led2 = await build_deliverable_ledger(turn_id="t_1", user_id="u_1", memory_notes_store=other)
    assert led2.memories == 0, "上一轮写的记忆不能算到本轮头上"


async def test_memory_miss_shape_06_23() -> None:
    """06-23：用户说「需要你记住」，零工具调用，答案却说「我记住了」。"""
    empty = SimpleNamespace(get=lambda **kw: _async(None))
    led = await build_deliverable_ledger(turn_id="t_1", user_id="u_1", memory_notes_store=empty)
    assert led.memories == 0
    assert led.produced_anything is False


# ── best-effort: no individual failure may crash finalization ──────────────────────────


async def test_store_exception_degrades_to_zero_not_raise() -> None:
    boom = SimpleNamespace(list_deliverables_for_turn=lambda tid: _raise())
    led = await build_deliverable_ledger(
        turn_id="t_1",
        artifact_store=boom,
        invocations=[_inv("bi_query")],
    )
    assert led.files == 0
    assert led.ok_tools == frozenset({"bi_query"})  # Other fields unaffected


async def test_missing_turn_id_returns_tools_only() -> None:
    led = await build_deliverable_ledger(turn_id=None, invocations=[_inv("bi_query")])
    assert led == DeliverableLedger(
        ok_tools=frozenset({"bi_query"}),
        files_verified=False,
        sections_verified=False,
        memories_verified=False,
    )


# ── helpers ─────────────────────────────────────────────────────────────────


async def _async(value):
    return value


async def _raise():
    raise RuntimeError("store down")


# ── worked_but_produced_nothing: require dispatched work too, or all small talk would count ──


def test_plain_chat_turn_is_not_counted() -> None:
    """普通聊天轮（查个数、解释概念）本来就不产持久物 —— 不该进这个指标。

    这是本指标最初的设计缺陷：只用 `not produced_anything` 会把每一轮闲聊都算上，
    指标稀释到没有意义。
    """
    led = DeliverableLedger(ok_tools=frozenset({"bi_query"}))
    assert led.produced_anything is False
    assert led.worked_but_produced_nothing is False, "闲聊轮不该计数"


def test_dispatched_but_nothing_persisted_is_counted() -> None:
    """派了 peer 却什么都没留下 —— 正是要量的 C 形态。"""
    led = DeliverableLedger(ok_tools=frozenset({"subagent_spawn", "bi_query"}))
    assert led.worked_but_produced_nothing is True


def test_dispatched_and_produced_is_not_counted() -> None:
    led = DeliverableLedger(sections=9, ok_tools=frozenset({"subagent_spawn"}))
    assert led.worked_but_produced_nothing is False


def test_a_successful_remember_counts_even_though_it_writes_the_other_table():
    """★★★ 每一次成功的 `remember` 都曾被判成谎报。

    账本原来只查 `memory_notes`（一个用户一行，看 `source_turn_id == turn_id`），
    而 `remember` 写的是 **`memory_entries`** —— 写入侧搬家了、对账侧留在旧表。
    后果是防谎报守卫给用户追加一句「这条信息**没有**被写入长期记忆」，
    而同一轮的工具回执写着「已存为长期记忆」：**同一段话自相矛盾**。

    116（1.2.59）真机复现：an observed task 的 `tool_result` 与 `final_answer`
    一个说存了一个说没存。

    ★ 突变验证：把 `count_memory_receipts` 的调用去掉（退回只查 notes）⇒ 本用例红。
    """
    from bowhead_core.deliverable_ledger import count_memory_receipts

    class _Inv:
        def __init__(self, name, result, nested=None):
            self.tool_name, self.result, self.nested = name, result, nested

    ok = [_Inv("remember", {"ok": True, "id": "mem_abc", "action": "updated"})]
    assert count_memory_receipts(ok) == 1

    # ★ Accept only **storage-returned ids**, not tool execution: false positives here mean
    # assuming a write occurred and permitting real false claims, riskier than underreporting.
    assert count_memory_receipts([_Inv("remember", {"ok": True})]) == 0
    assert count_memory_receipts([_Inv("remember", {"ok": False, "id": "mem_x"})]) == 0
    assert count_memory_receipts([_Inv("search", {"ok": True, "id": "x"})]) == 0

    # ⚠ Delegated turns: peers' actual calls are nested; top-level-only reads are blind precisely where most needed.
    nested = [_Inv("spawn", {"ok": True}, nested=[_Inv("remember", {"ok": True, "id": "mem_n"})])]
    assert count_memory_receipts(nested) == 1


@pytest.mark.asyncio
async def test_the_ledger_actually_uses_the_receipts_not_just_the_helper():
    """★★ 接线用例。上一条只测了 `count_memory_receipts` 这个纯函数 ——
    把 `build_deliverable_ledger` 里那句调用换成 `memories = 0`，上一条**照样绿**。
    「测了函数没测接线」是本仓反复出现的形状，这条补的就是那半。

    ★ 突变验证：`memories = count_memory_receipts(invocations)` → `memories = 0`
    ⇒ 本用例红。
    """
    from bowhead_core.deliverable_ledger import build_deliverable_ledger

    class _Inv:
        def __init__(self, name, result):
            self.tool_name, self.result, self.nested = name, result, None

    led = await build_deliverable_ledger(
        turn_id="t_1",
        user_id="u1",
        # Deliberately **omit** memory_notes_store: remember writes memory_entries;
        # ledger must recognize writes solely by receipt or every successful write becomes a false claim.
        memory_notes_store=None,
        invocations=[_Inv("remember", {"ok": True, "id": "mem_abc", "action": "created"})],
    )
    assert led.memories == 1
    assert led.memories_verified is True, "有回执就算核过——不能因为旧表读不到就说'未验证'"


# ── Facts returned to Lead with a helper result ────────────────────────────────


def test_produced_facts_name_the_files_and_keep_unknown_items_unknown() -> None:
    facts = produced_facts(DeliverableLedger(files=1, file_names=("r.pdf",), sections=9))
    assert facts == {
        "files": 1,
        "file_names": ["r.pdf"],
        "reused_files": 0,
        "reused_file_names": [],
        "canvas_sections": 9,
        "memories": 0,
        "verified": {"files": True, "reused_files": True, "canvas_sections": True, "memories": True},
    }
    # A later Turn revises a file by identifier, so delivered files are named with theirs.
    delivered = produced_facts(DeliverableLedger(files=3, file_names=("r.pdf", "rows.csv"), delivered=(("art_1", "r.pdf"),)))
    assert delivered["delivered_files"] == [{"artifact_id": "art_1", "name": "r.pdf"}]
    unread = produced_facts(DeliverableLedger(files=1, files_verified=False, delivered=(("art_1", "r.pdf"),)))
    assert unread["files"] is None and unread["verified"]["files"] is False and "delivered_files" not in unread


@pytest.mark.parametrize(
    ("language", "marker"),
    [
        ("zh-CN", "用户拿不到"),
        ("en-US", "the user cannot get it"),
        ("de-DE", "nicht geliefert"),
        ("fr-FR", "n’a pas été livré"),
        ("ja-JP", "the user cannot get it"),
    ],
)
def test_files_note_is_stated_in_the_answer_language_whatever_the_helper_wrote(language: str, marker: str) -> None:
    note = produced_files_note(DeliverableLedger(files=0), language)
    assert "produced.file_names" in note and marker in note
    assert "reused_file_names" not in note


def test_files_note_names_reused_files_only_when_files_were_reused_and_is_absent_when_unread() -> None:
    reused = DeliverableLedger(files=1, file_names=("output.pdf",), reused_files=1, reused_file_names=("output.pptx",))
    assert "produced.reused_file_names" in produced_files_note(reused, "en-US")
    assert produced_files_note(DeliverableLedger(files_verified=False), "zh-CN") == ""
