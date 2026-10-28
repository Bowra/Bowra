"""记忆重做 §5 —— 存量 notes → entries 的迁移作业（tech-design §5.1 / §5.2）。

fixture 是 116 生产库 `usr_e9b84046e672` 的**真 6 条 bullet**（discovery README §1 逐字读出）。
这不是我编的样例 —— 本 sprint 已经被「自己编的串只覆盖顺利路径」坑过一轮（批2 词表）。

三条最要紧的性质：
- **映射表外的 bullet 不迁**，报「需要人工起 key」。key 是 FR3 的主题锚，起错一次
  这条记忆就永远判不出同主题；少迁一条只要人补一次映射。
- **notes 行不清空**（回滚位）：闸一关就得能读回原文。所以「还读不读 notes 段」
  靠 per-user 标记判，不靠「notes 是不是空的」。
- **可重入**：写入走 `commit_memory_write`，重跑时逐字判重命中 ⇒ 不新增行。
"""

from __future__ import annotations

import pytest
from bowhead_core.memory_legacy_notes_migration import (
    ACTION_DISCARD,
    ACTION_MIGRATE,
    ACTION_NEEDS_HUMAN,
    decide_bullet,
    plan_notes_migration,
)
from bowhead_core.memory_notes import MemoryNotesStore
from bowhead_core.memory_pack import build_memory_pack
from bowhead_core.user_memory import UserMemoryStore

#: The actual 6 entries from 116 `usr_e9b84046e672` (README §1).
REAL_NOTES = """• 偏好：内部看盈利只用净利率（net profit margin），毛利率基本不看。
• 偏好：用户看数据只盯环比趋势，不在意绝对值大小。KPI 以 delta 为主。
• 偏好：排名类图表偏好用横向条形图（horizontal bar）而非竖向柱状图
• 偏好：In KPI cards, show revenue in millions (¥M) rather than raw numbers.
• Rule: When comparing brands, always show year-over-year growth alongside absolute values.
• 偏好：用户希望我以后都用英文回答（answer in English）"""


def test_the_six_real_bullets_land_exactly_where_the_design_says():
    """§5.2 逐条点名的 5 个 key，以及那条僵尸。"""
    decisions = plan_notes_migration(REAL_NOTES)
    assert len(decisions) == 6
    migrated = [(d.key, d.kind, d.memory_type) for d in decisions if d.action == ACTION_MIGRATE]
    assert migrated == [
        ("profit_metric", "preference", "semantic"),
        ("trend_focus", "preference", "semantic"),
        # Presentation/format family ⇒ procedural.
        ("ranking_chart_type", "preference", "procedural"),
        ("kpi_money_unit", "preference", "procedural"),
        # `Rule:` prefix ⇒ constraint (§5.2: read directly from the bullet label prefix).
        ("brand_compare_yoy", "constraint", "semantic"),
    ]


def test_the_language_zombie_is_discarded_and_named():
    """★ 语言偏好今天由 `output_language` 设置承载。迁进记忆就是和设置打架 ——
    记忆说英文、设置说中文时，模型听谁**没有判据**。"""
    [zombie] = [d for d in plan_notes_migration(REAL_NOTES) if d.action == ACTION_DISCARD]
    assert "英文" in zombie.text
    assert "output_language" in zombie.reason


def test_an_unmapped_bullet_is_never_given_an_invented_key():
    """★★ 映射表外的一律 `needs_human`。**绝不按内容自动起 key**：
    `kind='note'` 的老 `_stable_note_key` 就是内容哈希，换一个字就是另一个 key
    ⇒ 自由文本记忆永远判不出同主题（§4.2 a3 就是被这条坑出来的）。
    少迁一条，人补一次映射即可；迁错一条，是把两个主题永久焊在一个 key 上。"""
    d = decide_bullet("偏好：报告结尾必须附上数据口径说明")
    assert d.action == ACTION_NEEDS_HUMAN
    assert d.key == ""


def test_label_prefix_and_glyph_do_not_affect_the_judgement():
    """判据比的是语义正文 —— 中英标签、字形、标点都不该影响归属。"""
    for line in (
        "• 偏好：排名类图表偏好用横向条形图（horizontal bar）而非竖向柱状图",
        "📌 Preference: 排名类图表用 horizontal bar",
        "排名类图表用横向条形图",
    ):
        assert decide_bullet(line.lstrip("•📌 ")).key == "ranking_chart_type", line


@pytest.fixture
async def migrated(tmp_path):
    """跑一遍真作业：种 116 形状 → 迁移 → 返回两个 store。"""
    from bowhead_core.memory_legacy_migrate_exec import run_migration

    db = str(tmp_path / "bowhead.db")
    memory = UserMemoryStore(db)
    await memory.initialize()
    notes = MemoryNotesStore(db)
    await notes.initialize()
    await notes.upsert_content(user_id="u1", content=REAL_NOTES, source="user_directive")
    row = {"subject_id": "u1"}
    created, _, _ = await run_migration(db, [(row, plan_notes_migration(REAL_NOTES))], [])
    return memory, notes, created


@pytest.mark.asyncio
async def test_migration_creates_five_high_confidence_global_entries(migrated):
    memory, _, created = migrated
    assert created == 5
    entries = await memory.list_product_for_user("u1")
    assert len(entries) == 5
    for e in entries:
        assert e.confidence == "high", e.key  # That row was written by remember (§5.2).
        assert e.workspace_id is None, "存量条目一律全局——按行里的值反推空间就是编造"
        # ★ Provenance cannot be reconstructed (notes.source_turn_id is row-level); honestly leave it blank, never invent one.
        assert e.created_source_turn_id is None, e.key


@pytest.mark.asyncio
async def test_the_notes_row_is_not_cleared_because_it_is_the_rollback_position(migrated):
    """★ §5.0/§7：迁移**不清空 notes**。闸一关 `build_memory_pack` 要能读回原文。"""
    _, notes, _ = migrated
    note = await notes.get(user_id="u1")
    assert note is not None and "净利率" in note.content


@pytest.mark.asyncio
async def test_a_migrated_user_stops_getting_the_notes_section(migrated, monkeypatch):
    """闸开 + 已迁移 ⇒ 不读 notes 段，否则同一条偏好在 prompt 里出现两次。"""
    memory, notes, _ = migrated
    pack = await build_memory_pack(user_id="u1", user_memory_store=memory, notes_store=notes)
    assert "[mem_" in pack
    # ★ Assert **exactly once**, not absence: migrated entry **values** contain the original bullet,
    # so it must appear once. Injecting notes too repeats it, the actual duplicate injection.
    # (My first version used `not in pack`, wrongly rejecting the legitimate single occurrence.)
    assert pack.count("内部看盈利只用净利率（net profit margin），毛利率基本不看。") == 1
    # notes is wrapped by `wrap_user_content(..., source_type="user_profile")`;
    # after migration, that entire section must be absent.
    assert "user_profile" not in pack


@pytest.mark.asyncio
async def test_an_unmigrated_user_still_gets_the_notes_section(migrated, monkeypatch):
    """★ 标记必须是 **per-user** 的。全局一个开关会让**没迁的用户**也不再读 notes，
    他们的记忆当场从提示词里消失。"""
    memory, notes, _ = migrated
    await notes.upsert_content(user_id="u_other", content="• 偏好：还没迁的老用户", source="user_directive")
    pack = await build_memory_pack(user_id="u_other", user_memory_store=memory, notes_store=notes)
    assert "还没迁的老用户" in pack


@pytest.mark.asyncio
async def test_running_the_migration_twice_adds_nothing(migrated):
    """可重入靠 `commit_memory_write` 的逐字判重，不靠脚本自己记「我跑过了」。"""
    from bowhead_core.memory_legacy_migrate_exec import run_migration

    memory, _, _ = migrated
    before = len(await memory.list_for_user("u1"))
    created, _, _ = await run_migration(memory.db_path, [({"subject_id": "u1"}, plan_notes_migration(REAL_NOTES))], [])
    assert created == 0
    assert len(await memory.list_for_user("u1")) == before


@pytest.mark.asyncio
async def test_the_job_writes_the_policy_cutover_marker_on_an_upgraded_db(tmp_path):
    """★ `_mark_empty_policy_cutover` 只在**零 user 行的全新库**写这个标记 ⇒
    任何从旧库升上来的部署都没有它，`v2_readiness()` 会一直返 incomplete。
    补写它是 §5 作业的活 —— 也是翻 `FEATURE_MEMORY_CANONICAL` 的前置。

    ⚠ 这条**必须构造「旧库」形态**：直接用干净 tmp 库的话，`initialize()` 那一刻库是空的，
    `_mark_empty_policy_cutover` 已经把标记写好了 —— 于是「作业补写了标记」这条断言
    **为了错误的理由通过**（我第一版就是这样，突变「删掉补写」根本打不红）。
    """
    import aiosqlite
    from bowhead_core.memory_legacy_migrate_exec import run_migration
    from bowhead_core.user_memory import MEMORY_POLICY_CUTOVER_KEY

    db = str(tmp_path / "legacy.db")
    memory = UserMemoryStore(db)
    await memory.initialize()
    notes = MemoryNotesStore(db)
    await notes.initialize()
    await notes.upsert_content(user_id="u1", content=REAL_NOTES, source="user_directive")

    # Simulate an upgraded old DB: user rows exist, no cutover marker.
    await memory.upsert(user_id="u1", kind="preference", key="legacy", value="老行", source="user_explicit")
    async with aiosqlite.connect(db) as conn:
        await conn.execute("DELETE FROM memory_migration_state WHERE key=?", (MEMORY_POLICY_CUTOVER_KEY,))
        await conn.commit()
    ok, why = await memory.v2_readiness()
    assert not ok and why == "memory_policy_cutover_incomplete", "前置没造出来，这条用例就没在测东西"

    row = {"subject_id": "u1"}
    unclassified = [{"subject_id": "u1", "kind": "preference", "key": "legacy", "value": '"老行"', "source": "user_explicit"}]
    await run_migration(db, [(row, plan_notes_migration(REAL_NOTES))], unclassified)

    ok, why = await memory.v2_readiness()
    assert ok, why


@pytest.mark.asyncio
async def test_backfill_also_fills_confidence_on_an_already_classified_row(tmp_path):
    """★ 116 实测发现的缺口：那条 `constraint` 已经是 product_memory/v1/text_v1，
    但 migration 0024 的 `confidence` / `memory_type` 两列**还空着**。
    只查「没分类的」会让报表印「补分类 0 条」，而它的置信度仍然是**碰巧** NULL 不是决定
    —— 而 `can_overwrite` 把 NULL 当 low，后台推断因此能盖掉它。
    §5.1 明写「product 的补 memory_type/confidence」。
    """

    from bowhead_core.memory_legacy_migrate_exec import run_migration

    try:
        db = str(tmp_path / "b.db")
        memory = UserMemoryStore(db)
        await memory.initialize()
        # Reproduce that 116 row: classified, but both new columns empty.
        created = await memory.upsert_product_memory(
            user_id="u1", kind="constraint", key="growth_rule", value="增长贡献按环比算", source="dreaming_light_approved"
        )
        import aiosqlite

        async with aiosqlite.connect(db) as conn:
            await conn.execute("UPDATE memory_entries SET confidence=NULL, memory_type=NULL WHERE id=?", (created.id,))
            await conn.commit()

        rows = [{"subject_id": "u1", "kind": "constraint", "key": "growth_rule", "value": '"增长贡献按环比算"', "source": "dreaming_light_approved"}]
        _, classified, _ = await run_migration(db, [], rows)
        assert classified == 1
        [entry] = [e for e in await memory.list_product_for_user("u1") if e.id == created.id]
        # Candidate approval provenance ⇒ low. The key is derivation from **writer identity**, not the exact value; no longer NULL.
        assert entry.confidence == "low"
    finally:
        pass


@pytest.mark.asyncio
async def test_migrated_rows_are_labelled_as_migrated_not_as_something_you_said(migrated):
    """★ `source` 与 `created_source_type` 是两件事，必须分开传：
    前者定置信度（`user_directive` ⇒ high，§5.2），后者是**来源**。

    初版只传了 `actor_type`（落到 `last_modified_actor_type`），`created_source_type`
    被写成了 `source`＝`user_directive` ⇒ 详情面会把迁移进来的条目显示成
    「你亲口说的、没有来源」，而不是「迁移自旧版记忆」。设计稿上那个 tag 当场没数据。
    """
    from bowhead_core.memory_legacy_notes_migration import LEGACY_SOURCE_TYPE

    memory, _, _ = migrated
    entries = await memory.list_product_for_user("u1")
    assert entries
    for e in entries:
        assert e.created_source_type == LEGACY_SOURCE_TYPE, (e.key, e.created_source_type)
        # Still high: confidence derives from `source`; separating these fields must not break it.
        assert e.confidence == "high", e.key


@pytest.mark.asyncio
async def test_migrated_rows_carry_a_memory_type_but_backfilled_ones_stay_unlabelled(tmp_path):
    """★ 详情面有一枚「呈现口径/业务口径」chip 直接读 `memory_type`。

    - 从 notes 迁进来的：映射表里人工判过 ⇒ 有值；
    - 补分类的存量行：**没人判过 ⇒ 留 NULL**，界面显示「未标注」。
      `upsert` 刻意**不**用 `normalize_memory_type(None)` 兜成 `semantic` ——
      那会把一个没人判过的值当成事实展示给用户。（§6.1 的 fail-safe 说的是
      **写入**路径上模型没给值，不是替存量行凭空补一个。）
    """

    from bowhead_core.memory_legacy_migrate_exec import run_migration

    try:
        db = str(tmp_path / "m.db")
        memory = UserMemoryStore(db)
        await memory.initialize()
        notes = MemoryNotesStore(db)
        await notes.initialize()
        await notes.upsert_content(user_id="u1", content=REAL_NOTES, source="user_directive")
        await memory.upsert(user_id="u1", kind="constraint", key="legacy_rule", value="老约束", source="dreaming_light_approved")
        unclassified = [{"subject_id": "u1", "kind": "constraint", "key": "legacy_rule", "value": '"老约束"', "source": "dreaming_light_approved"}]
        await run_migration(db, [({"subject_id": "u1"}, plan_notes_migration(REAL_NOTES))], unclassified)

        by_key = {e.key: e for e in await memory.list_product_for_user("u1")}
        assert by_key["kpi_money_unit"].memory_type == "procedural"
        assert by_key["profit_metric"].memory_type == "semantic"
        assert by_key["legacy_rule"].memory_type is None, "没人判过的行不该被凭空补一个口径"
    finally:
        pass


@pytest.mark.asyncio
async def test_backfill_refuses_to_classify_a_row_that_would_go_invisible(tmp_path):
    """★ 收口 review 第 2 条：补分类可能把一行「补」进**永久隐身**状态。

    `upsert_product_memory` 用 `_value_schema_for(value)` 推 schema，字符串给 `text_v1`；
    而 `kind='note'` 的可见组合是 `('note','note_text_v1')` —— `('note','text_v1')`
    落到 `_HIDDEN_POLICY`。`commit_memory_write` 早就特判过这件事，补分类这条路没有。

    后果有两层，第二层才是要命的：
      (a) `list_product_for_user` 静默丢掉它 —— 正是本作业要防的那种丢失；
      (b) `v2_readiness()` **没有用户过滤** ⇒ 一行不可见的 product 行会让
          `/memory/overview` 与 `/memory/items` 对**所有用户** 503。

    ★ 突变验证：删掉 `_backfill_would_stay_visible` 那道判断 ⇒ 本用例红
    （classified 变 1、skipped 变空，而且该行补完之后 `list_product_for_user` 看不见它）。
    """
    from bowhead_core.memory_legacy_migrate_exec import run_migration

    memory = UserMemoryStore(tmp_path / "m.db")
    await memory.initialize()
    e = await memory.upsert(user_id="u1", kind="note", key="n1", value="一段自由文本")

    rows = [{"subject_id": "u1", "kind": "note", "key": "n1", "value": '"一段自由文本"', "source": "user_directive"}]
    created, classified, skipped = await run_migration(str(memory._db_path), [], rows)  # noqa: SLF001

    assert classified == 0, "补完会隐身的行不该被补"
    assert [(u, k, key) for (u, k, key) in skipped] == [("u1", "note", "n1")], "必须报给人看，不能静默跳过"
    # Negative control: still backfill structurally matching rows.
    await memory.upsert(user_id="u1", kind="preference", key="p1", value="横向条形图")
    ok_rows = [{"subject_id": "u1", "kind": "preference", "key": "p1", "value": '"横向条形图"', "source": "user_directive"}]
    _, classified2, skipped2 = await run_migration(str(memory._db_path), [], ok_rows)  # noqa: SLF001
    assert classified2 == 1 and skipped2 == []
    assert e is not None


@pytest.mark.asyncio
async def test_a_trashed_legacy_row_does_not_deadlock_the_memory_page_for_everyone(tmp_path):
    """★ 收口 review 第 8 条：`v2_readiness` 的存量扫描不看 `deleted_at`，而补分类作业只处理
    `deleted_at IS NULL` ⇒ 一条躺在回收站里的未分类旧行会把闸**永久**卡住，
    作业够不着它，只能等 30 天硬删的 cron。

    而这道闸**没有用户过滤** —— 卡住的是**所有人**的 `/memory/overview` 与 `/memory/items`。

    ★ 突变验证：把 `deleted_at IS NULL` 从那条查询里去掉 ⇒ 本用例红。
    """
    memory = UserMemoryStore(tmp_path / "m2.db")
    await memory.initialize()
    await memory.mark_policy_cutover_complete()

    e = await memory.upsert(user_id="u1", kind="preference", key="p1", value="v1")
    await memory.soft_delete(user_id="u1", entry_id=e.id)
    # Revert it to the legacy-unclassified shape.
    import aiosqlite

    async with aiosqlite.connect(memory._db_path) as db:  # noqa: SLF001
        await db.execute("UPDATE memory_entries SET memory_role='unclassified', memory_policy_version=0 WHERE id=?", (e.id,))
        await db.commit()

    ready, reason = await memory.v2_readiness()
    assert ready is True, f"回收站里的旧行不该把整站记忆页卡死（reason={reason}）"


@pytest.mark.asyncio
async def test_rerunning_the_migration_does_not_overwrite_a_user_edit(tmp_path):
    """★ 收口 review 第 7 条：「重跑是 no-op」此前只对**没被改过**的行成立。

    用户在记忆页改了某条迁移条目之后，文本不再逐字相同 ⇒ 判重落空、`(kind,key)`
    身份查找命中 ⇒ `source='user_directive'` 推出 `high`，裁决表只禁止 low 盖 high
    ⇒ UPDATE 把用户的编辑**覆盖回旧 bullet**，而脚本照旧打印「已存在 ⇒ 重跑是 no-op」。

    迁移的职责是「把 notes 搬成条目」；搬过之后那条条目归用户，不归本作业。

    ★ 突变验证：去掉 `(kind, key) in existing_keys` 那道跳过 ⇒ 本用例红
    （用户改的值被写回旧 bullet）。
    """
    from bowhead_core.memory_legacy_migrate_exec import run_migration
    from bowhead_core.memory_legacy_notes_migration import plan_notes_migration

    memory = UserMemoryStore(tmp_path / "m3.db")
    await memory.initialize()
    row = {"subject_id": "u1"}
    plans = [(row, plan_notes_migration(REAL_NOTES))]

    created, _, _ = await run_migration(str(memory._db_path), plans, [])  # noqa: SLF001
    assert created == 5

    # User edits one entry on the memory page.
    [target] = [e for e in await memory.list_for_user("u1") if e.key == "profit_metric"]
    await memory.commit_memory_write(user_id="u1", kind=target.kind, key="profit_metric", value="用户改过的口径ZZZ", source="user_explicit")
    [edited] = [e for e in await memory.list_for_user("u1") if e.key == "profit_metric"]
    assert "用户改过的口径ZZZ" in edited.value

    # Rerun must actually be a no-op, never overwrite the user edit.
    created2, _, _ = await run_migration(str(memory._db_path), plans, [])  # noqa: SLF001
    assert created2 == 0
    [after] = [e for e in await memory.list_for_user("u1") if e.key == "profit_metric"]
    # ⚠ Assert a string **absent from the old bullet**. I initially used "毛利率", but the old bullet contained
    # "毛利率基本不看", so a mutation overwriting the value still passed, disabling this regression guard.
    assert "用户改过的口径ZZZ" in after.value, "重跑把用户的编辑覆盖回旧 bullet 了"
    assert "净利率" not in after.value
