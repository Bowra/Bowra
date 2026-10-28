"""kb-matrix M4 FR-41 — 内置列组模板 seed（per-ws 幂等，catalog_seed 范式）。

把 M2 起活在 FE 常量里的 5 张模板卡（财务 3 + 市场 2）升格为 seed doc_types：
与用户自建/转正模板**同源可管理**（模板管理页统一分组），套用走 new-matrix flow
（列由字段预填 + 用户选文件）。

与 Sprint-25 catalog（``system_catalog_seed``，抽取型 doc_type）**刻意分开**：
本 seed 用独立 ``SYSTEM_TEMPLATE_SEED_USER``——kind 派生（doc_type_store.
derive_doc_type_kind）靠它分桶，catalog 型不得误进「内置模板」组（r3 review P2-3）。

安全（journey F11）：seed 恒 ``auto_extract_enabled=False`` + 决策 12 生态 gate
同享（不进分类候选/bind picker/kb_list_tables）——**seed 落库绝不改变任何空间的
上传/分类行为**。

列定义与 FE ``new-matrix-flow.TEMPLATES`` 保持同步（wave-2 FE 改从本 seed 拉取后
常量退役）；question 列经 ``build_graduation_fields`` 同款降解（string + 问题进
extraction_hint）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from bowhead_core.kb.doc_type_store import SYSTEM_TEMPLATE_SEED_USER
from bowhead_core.kb.graduation import build_graduation_fields
from bowhead_core.kb.telemetry import emit_audit

if TYPE_CHECKING:
    from bowhead_core.kb.doc_type_store import KbDocTypeStore
    from bowhead_core.kb.lifespan_marker_store import KbLifespanMarkerStore

logger = logging.getLogger(__name__)

TEMPLATE_SEED_MARKER_PREFIX = "template_seeded_at_ws:"


def template_marker_key(workspace_id: str) -> str:
    return f"{TEMPLATE_SEED_MARKER_PREFIX}{workspace_id}"


@dataclass(frozen=True)
class TemplateSeedResult:
    workspace_id: str
    inserted: int
    skipped_already_present: int
    skipped_via_marker: bool


# Sync frontend new-matrix-flow.TEMPLATES (key → user-facing name fixed by design brief).
TEMPLATE_SEEDS: tuple[dict[str, Any], ...] = (
    {
        "name": "合同审阅",
        "description": "内置模板：供应商合同批量审阅（供应商/金额/日期/付款/竞业条款）",
        "columns": [
            {"name": "供应商名称", "type": "string", "question": "合同的供应商/乙方名称是什么？"},
            {"name": "合同金额", "type": "currency", "question": "合同总金额是多少？"},
            {"name": "签约日期", "type": "date", "question": "合同的签署日期是哪天？"},
            {"name": "到期日期", "type": "date", "question": "合同的到期/终止日期是哪天？"},
            {"name": "付款方式", "type": "string", "question": "合同约定的付款方式是什么？"},
            {"name": "是否含竞业限制条款", "type": "question", "question": "合同是否包含竞业限制/排他条款？具体如何约定？"},
        ],
    },
    {
        "name": "银行流水核对",
        "description": "内置模板：银行流水逐笔核对（对手/金额/日期/摘要/异常）",
        "columns": [
            {"name": "交易对手", "type": "string", "question": "这笔流水的交易对手是谁？"},
            {"name": "金额", "type": "currency", "question": "交易金额是多少？"},
            {"name": "交易日期", "type": "date", "question": "交易日期是哪天？"},
            {"name": "摘要", "type": "question", "question": "这笔交易的摘要/用途是什么？"},
            {"name": "异常备注", "type": "question", "question": "这笔交易有无异常之处？"},
        ],
    },
    {
        "name": "发票台账",
        "description": "内置模板：发票台账（销售方/金额/税额/日期/号码）",
        "columns": [
            {"name": "销售方", "type": "string", "question": "发票的销售方名称是什么？"},
            {"name": "金额", "type": "currency", "question": "发票金额（不含税）是多少？"},
            {"name": "税额", "type": "currency", "question": "发票税额是多少？"},
            {"name": "开票日期", "type": "date", "question": "开票日期是哪天？"},
            {"name": "发票号码", "type": "string", "question": "发票号码是什么？"},
        ],
    },
    {
        "name": "竞品报道分析",
        "description": "内置模板：竞品报道/评测分析（竞品/渠道/主张/价格/倾向/引语）",
        "columns": [
            {"name": "竞品名称", "type": "string", "question": "这篇材料涉及的竞品/公司名称是什么？"},
            {"name": "发布渠道", "type": "string", "question": "这篇材料的发布渠道/媒体是什么？"},
            {"name": "核心主张", "type": "question", "question": "竞品的核心主张/卖点是什么？"},
            {"name": "提到的价格", "type": "string", "question": "材料中提到的价格/定价信息是什么？"},
            {"name": "情感倾向", "type": "string", "question": "这篇材料对竞品的情感倾向（正面/中性/负面）是什么？"},
            {"name": "典型引语", "type": "question", "question": "材料中最有代表性的一句原话引语是什么？"},
        ],
    },
    {
        "name": "用户访谈提炼",
        "description": "内置模板：用户访谈纪要提炼（角色/痛点/诉求/倾向/引语）",
        "columns": [
            {"name": "受访者角色", "type": "string", "question": "受访者的角色/职位是什么？"},
            {"name": "核心痛点", "type": "question", "question": "受访者表达的核心痛点是什么？"},
            {"name": "功能诉求", "type": "question", "question": "受访者提出了哪些功能诉求？"},
            {"name": "情感倾向", "type": "string", "question": "受访者的整体情感倾向是什么？"},
            {"name": "典型引语", "type": "question", "question": "访谈中最有代表性的一句原话是什么？"},
        ],
    },
)


async def seed_templates_for_ws(
    workspace_id: str,
    doc_type_store: KbDocTypeStore,
    marker_store: KbLifespanMarkerStore,
) -> TemplateSeedResult:
    """Seed 5 张内置列组模板到单空间。幂等（marker + UNIQUE 双闸，照 catalog_seed）。"""
    marker_key = template_marker_key(workspace_id)
    if await marker_store.has_marker(marker_key):
        return TemplateSeedResult(workspace_id=workspace_id, inserted=0, skipped_already_present=0, skipped_via_marker=True)

    inserted = 0
    skipped = 0
    failed = 0
    for tpl in TEMPLATE_SEEDS:
        try:
            await doc_type_store.create(
                workspace_id=workspace_id,
                name=str(tpl["name"]),
                created_by=SYSTEM_TEMPLATE_SEED_USER,
                description=str(tpl["description"]),
                fields=build_graduation_fields(list(tpl["columns"])),
                cardinality="many",  # Like graduation/save-template (wave-2 consistency; ecosystem gate inert).
                auto_extract_enabled=False,  # journey F11: seed never changes upload/classification.
            )
            inserted += 1
        except Exception as exc:
            msg = str(exc)
            if "UNIQUE constraint failed" in msg or "IntegrityError" in type(exc).__name__:
                skipped += 1
                continue
            logger.warning("template seed failed ws=%s name=%s err=%s", workspace_id, tpl["name"], exc)
            failed += 1
    # Security review P3 (wave-2): full-batch hard failure (e.g. transient DB lock) never consumes marker;
    # after lazy loading, first-list transient failure + marker would permanently lose builtins for space, no self-healing.
    # Partial success / UNIQUE skips still mark, per actual catalog_seed behavior.
    if inserted + skipped > 0 or failed == 0:
        try:
            await marker_store.set(marker_key, {"inserted": inserted, "skipped": skipped, "failed": failed})
        except Exception:
            logger.warning("template seed marker write failed ws=%s", workspace_id, exc_info=True)
    emit_audit(
        "kb.template_seeded",
        {"workspace_id": workspace_id, "inserted": inserted, "skipped": skipped, "failed": failed},
    )
    return TemplateSeedResult(workspace_id=workspace_id, inserted=inserted, skipped_already_present=skipped, skipped_via_marker=False)
