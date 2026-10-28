"""「这个 artifact 还有没有人在用」—— 唯一知识点。

## 为什么需要它

回收运行数据前必须回答「还有没有人在用」。系统此前拿
`session_attachments.detached_at IS NULL`（=有没有被 pin）冒充这个答案，而 pin 是
`put_bytes` 在产物**出生时自动打**的、只有用户手点才摘 ⇒ 恒为真 ⇒ TTL 恒回收 0 条
（116 实测：30 天以上的运行数据 3,785 条候选，被这一条挡下 3,781 条，任何天数都是 0）。

**「还在用」不是一个布尔，是一组真实引用边的并集。** 本模块就是那个并集。

## 边的清单

| 边             | 来源                                    |
| -------------- | --------------------------------------- |
| 报告/画布引用  | `turn_citations.artifact_id`            |
| 画布正文引用   | `canvas_sections.payload` 里出现的 id    |
| 用户显式 pin   | `session_attachments` 且 inject='full'  |
| 模板任务输入   | `document_artifact_refs`（documents 模块自有） |
| 知识库原文     | `kb_files`（**另一个数据库**，见下）      |

`kb_files` 在 `kb_files.db` 里，SQL join 不到，只能由同时握着两个 store 的 gateway GC
循环合流——这是既有约定，本模块不改它。

## ⚠ 画布那条为什么必须全文扫，不能枚举 JSON 路径

116 全量核过：`art_` id 散落在 **~20 条不同 JSON 路径、8 种 section kind** 里——
`entries[].artifact_id`（4,092 个，最大一族）、`spec.data_artifact_id`、
`spec.source_meta.{id,artifact_id,resolved_from_artifact_id}`、
`evidence[].{computation,table}_artifact_id`、`source_refs[]`、`background.artifact_id`、
`flattened_artifact_id`，**以及 `code` / `sql` / `stdout_preview` / `markdown` 这些自由文本**。

只取 `input_artifact_ids` / `output_artifact_ids` 会漏 **40%（4,328 个）**。
而漏一个 = 一份老报告的数据引用永久 404（正是 GC 里那段 KB 对账要防的「静默半死」，
只不过 canvas 这条链路此前没人防）。

所以这里**故意过度包含**：正文里提到就算在用。方向是安全的——多留几份数据的代价，
远小于删掉一份仍被引用的数据。
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import aiosqlite

#: Literal artifact id shape (same as `_ref_allowlist` / `answer_decomposer`).
_ARTIFACT_ID_RE = re.compile(r"art_[0-9a-zA-Z]{6,}")

#: User-intent pin: **the same predicate** as `routes/sessions._is_user_composer_pin`,
#: used there for composer pins and here for retention. Legacy NULL `inject` falls back to kind.
USER_INTENT_PIN_SQL = "COALESCE({alias}.inject, CASE WHEN {alias}.kind = 'user_upload' THEN 'full' ELSE 'list' END) = 'full'"


def is_user_intent_pin(artifact: object) -> bool:
    """Python 侧的同款判据（SQL 版见 :data:`USER_INTENT_PIN_SQL`）。"""
    inject = getattr(artifact, "inject", None)
    if inject is None:
        inject = "full" if getattr(artifact, "kind", None) == "user_upload" else "list"
    return str(inject) == "full"


async def _table_exists(db: aiosqlite.Connection, name: str) -> bool:
    """表在不在 —— **只判存在，不吞查询错误**。

    表不存在 ⇒ 它一行都没有 ⇒ 这条边确实没有引用（与
    `documents.retention.active_ref_sql` 对老库返字面量 "0" 同一条推理）。
    只有 store 单独初始化的库（部分单测、只跑 artifact 的部署）会缺表。

    ⚠ 刻意**不**用 try/except 包真正的查询：那样一次锁库或损坏就会被读成
    「没有引用」，把仍被引用的数据送进回收站。存在性判定是确定的，查询失败要炸出来。
    """
    async with db.execute("SELECT 1 FROM sqlite_schema WHERE type='table' AND name=?", (name,)) as cur:
        return await cur.fetchone() is not None


async def active_reference_sql(db: aiosqlite.Connection, *, alias: str = "artifacts") -> str:
    """返回一个 SQL 布尔表达式：该行**是否还被引用**。

    只含本库里 join 得到的边。画布正文那条走
    :func:`canvas_referenced_artifact_ids`（正文是 JSON，SQL 里判不了），
    `kb_files` 那条由 gateway 的 GC 循环合流。
    """
    from bowhead_core.documents.retention import active_ref_sql as _document_ref_sql

    terms: list[str] = [
        # Template task inputs (documents owns the reference table; returns literal "0" for old databases lacking it)
        await _document_ref_sql(db),
    ]
    # [N] references minted by reports/canvases
    if await _table_exists(db, "turn_citations"):
        terms.append(f"EXISTS(SELECT 1 FROM turn_citations tc WHERE tc.artifact_id = {alias}.id)")
    # User-intent pins (automatic pins at agent creation **do not count**; these are the ones being impersonated)
    if await _table_exists(db, "session_attachments"):
        terms.append(
            f"EXISTS(SELECT 1 FROM session_attachments sa JOIN artifacts pinned ON pinned.id = sa.artifact_id "
            f"WHERE sa.artifact_id = {alias}.id AND sa.detached_at IS NULL "
            f"AND {USER_INTENT_PIN_SQL.format(alias='pinned')})"
        )
    return "(" + " OR ".join(terms) + ")"


async def canvas_referenced_artifact_ids(db: aiosqlite.Connection) -> set[str]:
    """扫 canvas_sections 全文，取出现过的所有 artifact id。

    **不枚举 JSON 路径**（理由见模块 docstring：会漏 40%）。一次全表扫，GC 每晚一轮，
    116 上 8,758 条 section 的量级完全吃得下。

    ⚠ 这是**保活**判据，不是权限判据：过度包含只会让某份数据多留一阵，
    不会让任何人看到不该看的东西。
    """
    referenced: set[str] = set()
    if not await _table_exists(db, "canvas_sections"):
        return referenced
    async with db.execute("SELECT payload FROM canvas_sections") as cur:
        async for (payload,) in cur:
            if not payload or "art_" not in payload:
                continue
            referenced.update(_ARTIFACT_ID_RE.findall(payload))
    return referenced


__all__ = [
    "USER_INTENT_PIN_SQL",
    "active_reference_sql",
    "canvas_referenced_artifact_ids",
    "is_user_intent_pin",
]
