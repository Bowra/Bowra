"""记忆重做 §5 —— 存量 `memory_notes` bullet → `memory_entries` 的**判据**。

上游：`docs/sprints/memory-spike-2026-08-29/tech-design.md` §5.1 / §5.2。
形态是**一次性可重入作业**，不是惰性迁移（§5.0）：数据量极小（116 上就一行 6 条），
惰性迁移要在读路径长期背一段兼容分支，是「一个概念 N 处实现」的种子；
而且分类需要判断，跑在读热路径上不可接受。

★★ **key 是人工确认过的固定映射，不让任何模型现起名**（§5.2）。
`key` 是 FR3 的主题锚：起错一次，这条记忆就**永远判不出同主题**，取代永远做不了
（`kind='note'` 的老 `_stable_note_key` 是内容哈希，换一个字就是另一个 key ——
就是被这条坑过才有 §4.2 a3）。所以本模块的规矩是：

    表里没有的 bullet → **不迁**，报表里点名「需要人工起 key」。

绝不按内容自动生成 key，也绝不「差不多就归到某个 key 上」。少迁一条，人补一次映射就行；
迁错一条，是把两个不同主题永久焊在同一个 key 上。
"""

from __future__ import annotations

from dataclasses import dataclass

from bowhead_core.memory_write_policy import MEMORY_TYPE_PROCEDURAL, MEMORY_TYPE_SEMANTIC, normalize_for_dedup

#: Source marker for existing entries. ★ **Source cannot be recovered**: `memory_notes.source_turn_id` is **row-level**,
#: remembering only the last write's turn, not each bullet's (§5.2). Honestly mark it as migrated from legacy memory,
#: and leave `created_source_turn_id` NULL; do not invent a seemingly evidence-backed turn id.
LEGACY_SOURCE_TYPE = "legacy_notes_migration"

ACTION_MIGRATE = "migrate"
ACTION_DISCARD = "discard"
ACTION_NEEDS_HUMAN = "needs_human"


@dataclass(frozen=True)
class _BulletRule:
    """一条**人工确认过**的映射。`match` 是判别短语（规范化后做包含判断）。"""

    match: tuple[str, ...]
    key: str
    kind: str
    memory_type: str
    why: str


#: Six bullets for `usr_e9b84046e672` in 116 production (read verbatim in discovery README §1).
#: Discriminators use words **unique to each bullet**, not whole sentences: documentation truncated sentences with ellipses,
#: so exact whole-sentence matching would find none in the real database.
_RULES: tuple[_BulletRule, ...] = (
    _BulletRule(
        match=("净利率", "net profit margin"),
        key="profit_metric",
        kind="preference",
        # Metric conventions/definitions ⇒ semantic (metric definition)
        memory_type=MEMORY_TYPE_SEMANTIC,
        why="内部看盈利只用净利率，毛利率基本不看",
    ),
    _BulletRule(
        match=("环比趋势", "delta"),
        key="trend_focus",
        kind="preference",
        memory_type=MEMORY_TYPE_SEMANTIC,
        why="只盯环比趋势、不在意绝对值，KPI 以 delta 为主",
    ),
    _BulletRule(
        match=("横向条形图", "horizontal bar"),
        key="ranking_chart_type",
        kind="preference",
        # Presentation/formatting ⇒ procedural (chart type)
        memory_type=MEMORY_TYPE_PROCEDURAL,
        why="排名类图表用横向条形图而非竖向柱状图",
    ),
    _BulletRule(
        match=("revenue in millions", "kpi cards"),
        key="kpi_money_unit",
        kind="preference",
        memory_type=MEMORY_TYPE_PROCEDURAL,
        why="KPI 卡片里营收用百万（¥M）",
    ),
    _BulletRule(
        match=("year-over-year", "comparing brands"),
        key="brand_compare_yoy",
        # `Rule:` prefix ⇒ constraint (§5.2: read directly from the bullet's label prefix)
        kind="constraint",
        # Required sections belong to metric conventions ⇒ semantic
        memory_type=MEMORY_TYPE_SEMANTIC,
        why="品牌对比必须同时给同比增长与绝对值",
    ),
)

#: ★ Zombie entries: **discard without migrating**, naming them in the report (§5.2).
#: Language preference now resides in `output_language` settings (multilingual sprint batches 0/4).
#: Migration would conflict with settings: English in memory vs Chinese in settings gives the model **no decision rule**.
#: The same issue is blocked for new writes by the language family in write-gate criterion ①.
_ZOMBIE_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("以后都用英文回答", "answer in english", "用英文回答"), "语言偏好已由 output_language 设置承载，迁进记忆会和设置打架"),
)


@dataclass(frozen=True)
class BulletDecision:
    """一条 bullet 的去向。`action` ∈ migrate | discard | needs_human。"""

    text: str
    action: str
    key: str = ""
    kind: str = ""
    memory_type: str = ""
    reason: str = ""


def _matches(normalized: str, phrases: tuple[str, ...]) -> bool:
    return any(normalize_for_dedup(p) in normalized for p in phrases)


def decide_bullet(text: str) -> BulletDecision:
    """单条 bullet 的判定。**纯函数**，不碰库。"""
    body = str(text or "").strip()
    if not body:
        return BulletDecision(text=body, action=ACTION_DISCARD, reason="空行")
    normalized = normalize_for_dedup(body)

    for phrases, why in _ZOMBIE_RULES:
        if _matches(normalized, phrases):
            return BulletDecision(text=body, action=ACTION_DISCARD, reason=why)

    hits = [rule for rule in _RULES if _matches(normalized, rule.match)]
    if len(hits) == 1:
        rule = hits[0]
        return BulletDecision(
            text=body,
            action=ACTION_MIGRATE,
            key=rule.key,
            kind=rule.kind,
            memory_type=rule.memory_type,
            reason=rule.why,
        )
    if len(hits) > 1:
        # Multiple mappings mean insufficiently unique discriminators; defer to a human. **Never arbitrarily pick one**:
        # a wrong choice binds two topics permanently to one key.
        return BulletDecision(
            text=body,
            action=ACTION_NEEDS_HUMAN,
            reason=f"同时命中 {len(hits)} 条映射（{', '.join(r.key for r in hits)}），判别短语需要收紧",
        )
    return BulletDecision(
        text=body,
        action=ACTION_NEEDS_HUMAN,
        reason="映射表里没有这条 —— 需要人工起一个语义 key 后加进 _RULES",
    )


def plan_notes_migration(content: str) -> list[BulletDecision]:
    """把一整段 notes markdown 拆成逐条判定。

    复用 `memory_tools._strip_to_text` 剥掉 `• `/`📌 ` 字形与 kind 标签前缀
    （中英双语都剥）—— 判据比的是**语义正文**，与用哪个标签、哪个字形无关。
    """
    from bowhead_core.memory_tools import _strip_to_text

    out: list[BulletDecision] = []
    for line in str(content or "").split("\n"):
        if not line.strip():
            continue
        out.append(decide_bullet(_strip_to_text(line)))
    return out


__all__ = [
    "ACTION_DISCARD",
    "ACTION_MIGRATE",
    "ACTION_NEEDS_HUMAN",
    "LEGACY_SOURCE_TYPE",
    "BulletDecision",
    "decide_bullet",
    "plan_notes_migration",
]
