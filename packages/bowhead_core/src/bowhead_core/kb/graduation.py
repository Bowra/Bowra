"""kb-matrix M4 FR-40: 矩阵转正 doc_type 的纯装配逻辑（无 IO，可单测）。

转正 = 把矩阵（文件 × 列 × cells）固化为一个冻结 doc_type：
  列定义 → ExtractField[]（question 降解 string；select/multi_select/boolean 保真
  含 options；dimension M5 留位带过）；
  每文件 cells → 一条已包裹 v2 行（``create_many_prewrapped`` 直存——绕
  ``create_many`` 的 re-wrap，那会丢字段，r2/r3 review P0）。

编排（authz/scope 复验/两库补偿）在 gateway 路由（照 from_template 先例）；
自愈 sweep 在 ``sweep_pending_graduations``（gateway lifespan 后台任务消费）。

安全：全部 variant 的 ``source_excerpt``（+ ``user_edit*`` 变体的字符串
raw/normalized 值）过 ``row_extractor.sanitize_cell_text``——矩阵 AI cell 走
structured_extractor 只做 grounding-blank **无** NFKC/bidi 清洗，人工编辑值
更是首次进 chat 信道（r3 review P1-5）。
"""

from __future__ import annotations

import copy
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from bowhead_core.kb.doc_type_store import ExtractField
from bowhead_core.kb.row_extractor import sanitize_cell_text
from bowhead_core.kb.telemetry import emit_audit

if TYPE_CHECKING:
    from bowhead_core.kb.doc_type_store import KbDocTypeStore
    from bowhead_core.kb.extracted_rows_store import KbExtractedRowsStore
    from bowhead_core.kb.matrix_store import MatrixCellDTO

logger = logging.getLogger(__name__)

# Cell statuses eligible for post-graduation snapshots (needs_review keeps original sentinel—excluded from aggregates,
# honestly reported in coverage); failed/queued/running/empty → field absent (= null semantics).
_SNAPSHOT_CELL_STATUSES = ("done", "needs_review")

GRADUATION_EXTRACTOR_MODEL = "matrix_graduation"


class GraduationError(ValueError):
    """装配级错误（route 层映射结构化 4xx）。code ∈ duplicate_column_names /
    invalid_column_name / column_name_too_long。detail 面向 FE 的补充字段。"""

    def __init__(self, code: str, detail: dict[str, Any] | None = None) -> None:
        self.code = code
        self.detail = detail or {}
        super().__init__(code)


def validate_graduation_columns(columns: list[dict[str, Any]]) -> None:
    """转正列名前置校验（wave-1 review 正确性 P1-1/P1-2/P2-1 三连修）：

    ①重复列名（strip 后）→ 422：fields_v2 是 dict，同名 last-wins = 静默丢一列数据，
      且 chat type-gate 读第一个同名 field_def → SUM 可跑在 choice 数据上 = 确信错数。
    ②charset 超 rows 查询白名单（alnum/中文/_-）→ 422：「金额（万元）」这类全角括号
      财务惯用名转正得进去、但 where/sort/aggregate 永远 `field name 含非法字符`——
      转正时静默、第一个聚合问题才炸。用**同一个** `_validate_field_name` 判
      （单一权威，漂移即回归）。
    ③名称 >100（内核 MAX_FIELD_NAME_LEN）→ 422 结构化码（原来裸 ValueError 中文串
      当 code 泄给 FE）。
    """
    from bowhead_core.kb.doc_type_store import MAX_FIELD_NAME_LEN
    from bowhead_core.kb.extracted_rows_store import _validate_field_name

    seen: set[str] = set()
    dupes: list[str] = []
    bad_charset: list[str] = []
    too_long: list[str] = []
    for col in columns:
        name = str(col.get("name") or "").strip() or str(col.get("column_id") or "field")
        if name in seen:
            dupes.append(name)
        seen.add(name)
        if len(name) > MAX_FIELD_NAME_LEN:
            too_long.append(name)
            continue
        try:
            _validate_field_name(name)
        except ValueError:
            bad_charset.append(name)
    if dupes:
        raise GraduationError("duplicate_column_names", {"columns": sorted(set(dupes))})
    if too_long:
        raise GraduationError("column_name_too_long", {"columns": too_long, "max": MAX_FIELD_NAME_LEN})
    if bad_charset:
        raise GraduationError(
            "invalid_column_name",
            {"columns": bad_charset, "hint": "列名仅支持中英文、数字、_-（括号/空格/%等符号无法在对话查询中使用）"},
        )


def build_graduation_fields(columns: list[dict[str, Any]]) -> list[ExtractField]:
    """矩阵列定义 → 内核 ExtractField[]（FR-40 §4.2 step 3）。

    question 列**仅降解 question**（问题文本进 extraction_hint 保留语义）；
    select/multi_select/boolean 保真（⚠不复用 matrix_engine._degraded_field——
    它把新三型也降了，那是抽取执行的约束不是转正的，r3 review P3-9）。
    列数上限与列名校验由调用方先查（MAX_FIELDS_PER_SCHEMA 422 +
    validate_graduation_columns）。description 截断到内核 500 cap（矩阵 question
    上限 2000——review P2-1：不截断则长问题列转正 ValueError 中文串泄成 error code；
    完整问题文本已在 extraction_hint，description 是信息性的）。
    """
    from bowhead_core.kb.doc_type_store import MAX_FIELD_DESC_LEN

    fields: list[ExtractField] = []
    for col in columns:
        type_ = str(col.get("type") or "string")
        question = str(col.get("question") or "")
        hint = str(col.get("extraction_hint") or "")
        if type_ == "question":
            type_ = "string"
            hint = f"{question}\n{hint}".strip() if question else hint
        # Contract review P2 (wave-2): question(≤2000)+hint(≤500) can total 2501, but
        # the "apply as matrix" return path hits _ColumnIn's 500 cap → template application always 422. Truncate
        # as for description (full question semantics already preserved in description).
        hint = hint[:500]
        options = col.get("options") if type_ in ("select", "multi_select") else None
        fields.append(
            ExtractField(
                name=str(col.get("name") or "").strip() or str(col.get("column_id") or "field"),
                type=type_,
                description=question[:MAX_FIELD_DESC_LEN],
                extraction_hint=hint,
                cross_verify=bool(col.get("cross_verify")),
                options=list(options) if isinstance(options, list) and options else None,
                dimension=(col.get("dimension") if isinstance(col.get("dimension"), dict) else None),
            )
        )
    return fields


def normalize_date_iso(value: str) -> str | None:
    """date 值 → ISO（YYYY-MM-DD）；解析失败 → None（调用方保留原值，
    **不哨兵化**——存量 done 不得被打成 needs_review，r2 决策 6）。

    覆盖：ISO 幂等 / 「2026年4月12日」中式 / dateutil 常见西式。仅在**转正组装**
    路径调用（graduation-confined——不碰 LIVE 共享 coercion）。
    """
    text = str(value).strip()
    if not text:
        return None
    # Chinese format: 2026年4月12日 (**day required**—"2026年4月" has month precision; adding day 1 fabricates precision)
    import re

    m = re.fullmatch(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日?", text)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            return datetime(y, mo, d, tzinfo=UTC).date().isoformat()
        except ValueError:
            return None
    try:
        from dateutil import parser as _du_parser  # type: ignore[import-untyped]

        # Dual-default trick (review P1-3): dateutil fills missing components from default—
        # "2026-04" becomes 2026-04-<today's day>, fabricating precision. Parse once with each of
        # two different defaults; accept only identical results = complete components; otherwise (month/year/bare number) →
        # None, fall back to original (honesty: leave unnormalized rather than give ISO false precision).
        d1 = _du_parser.parse(text, yearfirst=True, dayfirst=False, fuzzy=False, default=datetime(2000, 1, 1, tzinfo=UTC))
        d2 = _du_parser.parse(text, yearfirst=True, dayfirst=False, fuzzy=False, default=datetime(2001, 2, 2, tzinfo=UTC))
    except (ValueError, OverflowError, TypeError, ImportError):
        return None
    if d1.date() != d2.date():
        return None
    return str(d1.date().isoformat())


def build_graduation_entries(
    files: list[dict[str, Any]],
    columns: list[dict[str, Any]],
    cells: list[MatrixCellDTO],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """cells → ``create_many_prewrapped`` entries（FR-40 §4.2 step 5）。

    每文件一 entry：``fields_v2 = {列名: sanitized variants}``（provenance/
    多来源/edited variant 保真）；``edited_fields`` = 该行人工编辑过的列名
    （rows 表 ``edited_by_user_json`` 有位，进 chat tool-result——F10 映射；
    verified ✓ v1 明文放弃：行级/格级粒度不合）。date 列 primary
    ``normalized.value`` 归 ISO（confined）。缺格/failed → 字段缺席（null 语义，
    聚合 COALESCE→NULL 排除 + 覆盖诚实报出）。
    """
    by_key: dict[tuple[str, str], MatrixCellDTO] = {(c.file_id, c.column_id): c for c in cells}
    entries: list[dict[str, Any]] = []
    stats = {"cells_included": 0, "cells_absent": 0, "cells_needs_review": 0}
    for f in files:
        file_id = str(f.get("file_id") or "")
        if not file_id:
            continue
        fields_v2: dict[str, list[dict[str, Any]]] = {}
        edited_fields: list[str] = []
        for col in columns:
            cell = by_key.get((file_id, str(col.get("column_id") or "")))
            if cell is None or cell.status not in _SNAPSHOT_CELL_STATUSES or not cell.variants:
                stats["cells_absent"] += 1
                continue
            fname = str(col.get("name") or "").strip() or str(col.get("column_id") or "field")
            variants = _sanitize_variants(cell.variants)
            if str(col.get("type") or "") == "date":
                _normalize_date_variants(variants)
            fields_v2[fname] = variants
            stats["cells_included"] += 1
            if cell.status == "needs_review":
                stats["cells_needs_review"] += 1
            if cell.edited_by_user:
                edited_fields.append(fname)
        entries.append(
            {
                "file_id": file_id,
                "fields_v2": fields_v2,
                "edited_fields": edited_fields,
                "page": None,
            }
        )
    return entries, stats


def compose_description(matrix_name: str, n_files: int, n_cols: int, date_str: str) -> str:
    """自动 description（r3 决策 13）：``kb_list_tables`` 选表靠它——空 description
    让 LLM 对着一堆表名瞎猜；模板管理页可改。矩阵名过 sanitize（用户可控串直入
    LLM 表清单，安全 review P2-2 同类）。"""
    return f"由矩阵「{sanitize_cell_text(matrix_name, max_chars=120)}」转正 · {n_files} 份文档 × {n_cols} 列 · {date_str}"


async def sweep_pending_graduations(
    dt_store: KbDocTypeStore,
    rows_store: KbExtractedRowsStore,
    *,
    older_than_iso: str,
) -> dict[str, int]:
    """pending_snapshot 自愈 sweep（r3 决策 5——两库非原子的崩溃窗口收口）：

    - rows 真 COUNT > 0（rows commit 后、flip 前崩）→ 补 ``update_row_count`` +
      flip 'snapshot'（**自愈**，数据无损）；
    - COUNT == 0（doc_type create 后、rows 前崩）→ soft_delete 回收（墓碑让名）。

    谓词只触 pending（``list_pending_graduations``）——绝不碰 template/snapshot
    （r3 review：r2 的 row_count=0 谓词会误删全部合法 template）。cache 列不可信，
    以 rows 库真 COUNT 为准（r3 review P1-3）。
    """
    flipped = 0
    reclaimed = 0
    for dto in await dt_store.list_pending_graduations(older_than_iso):
        try:
            true_count = await rows_store.count_for_doc_type(
                workspace_id=dto.workspace_id,
                doc_type_id=dto.doc_type_id,
            )
            if true_count > 0:
                # Absolute count + WHERE pending guard (review P2-4: relative delta doubles permanently
                # under concurrent self-healing; winner-only flip makes loser no-op). ⚠ Sweep has no matrix to
                # compare expected rows—multibatch crash windows can flip partial snapshots; log count for
                # diagnosis (route self-healing reconstructs missing rows; see graduate idempotent branch).
                won = await dt_store.flip_graduated_snapshot(dto.doc_type_id, row_count=true_count)
                if won:
                    flipped += 1
                    logger.info("kb_matrix.graduation_sweep_flipped dt=%s rows=%d", dto.doc_type_id, true_count)
            else:
                await dt_store.soft_delete(dto.doc_type_id)
                # Empty remnants usually have no rows; defensive cascade (review P2-3: orphan rows retain excerpts)
                try:
                    await rows_store.soft_delete_for_doc_type(dto.doc_type_id, workspace_id=dto.workspace_id)
                except Exception:
                    logger.warning("sweep reclaim rows cascade failed dt=%s", dto.doc_type_id, exc_info=True)
                reclaimed += 1
        except Exception:
            logger.warning("kb_matrix.graduation_sweep_failed dt=%s", dto.doc_type_id, exc_info=True)
    if flipped or reclaimed:
        emit_audit("kb.matrix_graduation_swept", {"flipped": flipped, "reclaimed": reclaimed})
    return {"flipped": flipped, "reclaimed": reclaimed}


def _sanitize_variants(variants: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """深拷贝 + sanitize：全部 variant 的 source_excerpt、raw 与 normalized 的
    **全部字符串值**（wave-1 安全 review P2-2：AI 变体的 normalized.value/
    raw_string/choice 同样经 to_dict / excluded_samples 进 chat 信道——矩阵抽取
    只做 grounding-blank 无 NFKC/bidi，转正是这些内容首次入 LLM 上下文，恶意
    PDF→string 列值→chat 是有界注入面，不分 AI/人工一律清）。"""
    out = copy.deepcopy(variants)
    for v in out:
        if not isinstance(v, dict):
            continue
        excerpt = v.get("source_excerpt")
        if isinstance(excerpt, str):
            v["source_excerpt"] = sanitize_cell_text(excerpt)
        raw = v.get("raw")
        if isinstance(raw, str):
            v["raw"] = sanitize_cell_text(raw)
        norm = v.get("normalized")
        if isinstance(norm, dict):
            for k, val in list(norm.items()):
                if isinstance(val, str):
                    norm[k] = sanitize_cell_text(val)
                elif isinstance(val, list):  # multi_select choices
                    norm[k] = [sanitize_cell_text(x) if isinstance(x, str) else x for x in val]
    return out


def _normalize_date_variants(variants: list[dict[str, Any]]) -> None:
    """date 列 primary variant 的 normalized.value 归 ISO（就地，失败保留原值）。"""
    for v in variants:
        if not (isinstance(v, dict) and v.get("is_primary")):
            continue
        norm = v.get("normalized")
        if isinstance(norm, dict) and isinstance(norm.get("value"), str):
            iso = normalize_date_iso(norm["value"])
            if iso:
                norm["value"] = iso
