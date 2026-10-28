"""记忆重做 §5 —— 迁移作业的**执行**（判据在 `memory_legacy_notes_migration`）。

放在库里而不是脚本里，理由同 triage：`scripts/` 下的东西没人跑测试，
而这段要写生产库。可重入 —— 跑第二遍是 no-op（靠 `commit_memory_write` 的
逐字判重 + per-user 标记）。
"""

from __future__ import annotations

from typing import Any

from bowhead_core.memory_legacy_notes_migration import ACTION_MIGRATE, LEGACY_SOURCE_TYPE
from bowhead_core.user_memory import UserMemoryStore


def _backfill_would_stay_visible(*, kind: str, value: Any) -> bool:
    """补分类之后这一行还看得见吗？看不见就别补。

    判据直接问 `resolve_memory_policy` —— **不重抄一份可见组合表**：
    抄一份就等着它和 `_VISIBLE_POLICIES` 漂移，而漂移的后果是整站 503。
    """
    from bowhead_core.memory_kind_policy import resolve_memory_policy
    from bowhead_core.user_memory import CURRENT_MEMORY_POLICY_VERSION, MemoryRole, _value_schema_for

    policy = resolve_memory_policy(
        memory_role=MemoryRole.PRODUCT_MEMORY,
        memory_policy_version=CURRENT_MEMORY_POLICY_VERSION,
        kind=kind,
        value_schema=_value_schema_for(value),
        value=value,
    )
    return bool(policy.user_visible and policy.display_type)


async def run_migration(
    db_path: str,
    plans: list[tuple[Any, list]],
    unclassified: list[Any],
) -> tuple[int, int, list[tuple[str, str, str]]]:
    """执行迁移，返回 `(新建条数, 补分类条数, 补完会隐身而跳过的行)`。

    ★ 写入走 `commit_memory_write` 而不是裸 INSERT：它带着判重与裁决表。

    ⚠ **可重入不能只靠逐字判重**。用户在记忆页把某条迁移条目改了之后，文本不再逐字相同 ⇒
    判重落空、`(kind, key)` 身份查找命中 ⇒ `source='user_directive'` 推出 `high`，
    而裁决表只禁止 `low` 盖 `high` ⇒ 这条 UPDATE 会**把用户的编辑覆盖回旧 bullet**，
    脚本还照旧打印「已存在 ⇒ 重跑是 no-op」。所以这里显式按 `(kind, key)` 先查一遍：
    已经有了就**一律跳过**，不比值。迁移的职责是「把 notes 搬成条目」，
    搬过之后那条条目归用户，不归本作业。2026-09-04 收口 review 逮到。

    ★ `source` 用 `user_directive`：那 6 条 bullet 是 `remember` 工具写的
    （`memory_notes.source='user_directive'`，行级），按 §5.2 一律 `high`。
    """
    store = UserMemoryStore(db_path)
    skipped_invisible: list[tuple[str, str, str]] = []
    created = 0

    for row, decisions in plans:
        user_id = row["subject_id"]
        existing_keys = {(e.kind, e.key) for e in await store.list_for_user(user_id)}
        for d in decisions:
            if d.action != ACTION_MIGRATE:
                continue
            if (d.kind, d.key) in existing_keys:
                # Already migrated. **Do not compare values**: a difference means the user edited it.
                continue
            result = await store.commit_memory_write(
                user_id=user_id,
                kind=d.kind,
                key=d.key,
                value=d.text,
                # Write identity ⇒ high (§5.2: remember wrote this row)
                source="user_directive",
                memory_type=d.memory_type,
                # Always global: legacy entries lacked space semantics; inferring space from stored values fabricates attribution
                # and violates the memory-revive S2 boundary. Users can later switch to space scope on /memory.
                workspace_id=None,
                # ★ Origin cannot be reconstructed; state this honestly (§5.2): `memory_notes.source_turn_id` is **row-level**,
                # recording only the last write's turn, not each bullet's turn. Never invent one.
                created_source_turn_id=None,
                # ★ Pass `source` and `created_source_type` separately:
                # the former determines confidence (`user_directive` ⇒ high, §5.2); the latter is **provenance**,
                # used by details to show migration from legacy memory. Initially only actor_type was passed,
                # landing in `last_modified_actor_type` ⇒ `created_source_type` became
                # `user_directive`, so migrated entries impersonated direct user statements without provenance in the UI.
                created_source_type=LEGACY_SOURCE_TYPE,
                actor_type=LEGACY_SOURCE_TYPE,
            )
            if result.action == "created":
                created += 1
        await store.mark_legacy_notes_migrated(user_id)

    classified = 0
    for r in unclassified:
        # Complete role/schema/policy_version via upsert_product_memory, writing values back verbatim.
        # ⚠ Internally it uses `upsert`, which now **does not downgrade high** (review fix round),
        # so classification backfill cannot overwrite confidence manually edited by the user.
        value = r["value"]
        try:
            import json as _json

            parsed = _json.loads(value)
        except Exception:
            parsed = value
        # ⚠ **Do not** use `commit_memory_write` for classification backfill: its first stage checks verbatim duplicates,
        # and this same row is in the scan with identical value ⇒ self-match, return `confirmed`,
        # only bump `last_confirmed_at`; never complete role/schema/confidence.
        # `upsert` has no duplicate check, making it the correct classification-backfill path.
        # ⚠ **Before classification backfill, ensure the resulting row remains visible**.
        # `upsert_product_memory` infers schema via `_value_schema_for(value)`, assigning strings
        # `text_v1`, while visible `kind='note'` requires `('note','note_text_v1')`;
        # `('note','text_v1')` falls into `_HIDDEN_POLICY`. `commit_memory_write` knows this
        # and special-cases it (`schema_value = "note_text_v1" if kind == "note"`); this backfill path
        # previously did not, backfilling a row into **permanent invisibility**:
        # (a) `list_product_for_user` silently drops it, the exact loss this job must prevent;
        # (b) worse, `v2_readiness()` **has no user filter**; one invisible product row
        # makes `/memory/overview` and `/memory/items` return 503 for **all users**.
        # Same for dict/list values (`object_v1`/`list_v1` also lack visible combinations).
        # If backfill cannot succeed, **skip it** and report to humans; never create apparently successful but actually invisible data.
        if not _backfill_would_stay_visible(kind=r["kind"], value=parsed):
            skipped_invisible.append((r["subject_id"], r["kind"], r["key"]))
            continue
        await store.upsert_product_memory(
            user_id=r["subject_id"],
            kind=r["kind"],
            key=r["key"],
            value=parsed,
            source=r["source"] or "legacy_backfill",
        )
        classified += 1

    await store.mark_policy_cutover_complete()
    return created, classified, skipped_invisible


__all__ = ["run_migration"]
