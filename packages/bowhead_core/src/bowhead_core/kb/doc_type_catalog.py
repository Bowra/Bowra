"""kb-structured-tables Sprint 2 — predefined doc_type catalog (5 accounting types).

Seed-time intermediate representation. Inserted via `catalog_seed.seed_catalog_for_ws`.
Per-ws independent (UNIQUE(workspace_id, name)) — same catalog seeded into every ws.

Per PRD r2 §3 Phase A. 5 doc_types covering Bowhead prod corpus 主类型:
- 序时账 / 发生额及余额表  → cardinality=many, auto_extract_enabled=False (Sprint 2 chunks fallback)
- 资产评估报告 / 税务凭证 / 政府补助证书  → cardinality=one, auto_extract_enabled=True

`sample_keywords` 用于 classifier fast path (≥3 distinct match → commit), 严格无 regex
meta-char (per I-B8 validation in doc_type_store.create()).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from bowhead_core.kb.doc_type_store import ExtractField


@dataclass(frozen=True)
class CatalogDocType:
    """Catalog seed intermediate representation. Mapped to KbDocTypeDTO at insert.

    Sprint 25 added `dedup_key_fields` for per-row dedup_key_hash composition
    (required for cardinality=many). Empty list for cardinality=one.
    """

    name: str
    description: str
    fields: list[ExtractField]
    cardinality: Literal["one", "many"]
    auto_extract_enabled: bool
    sample_keywords: list[str]
    dedup_key_fields: list[str] = field(default_factory=list)  # Sprint 25


CATALOG: tuple[CatalogDocType, ...] = (
    CatalogDocType(
        name="明细账",  # Sprint 25 rename: 序时账 → 明细账 (account-detail terminology more precise).
        description="按日期/凭证号排列的会计明细账, 每行一条凭证. 也称序时账.",
        fields=[
            ExtractField(name="日期", type="date", description="凭证日期", required=True),
            ExtractField(name="凭证号", type="string", description="凭证编号", required=True),
            ExtractField(name="摘要", type="string", description="凭证摘要"),
            ExtractField(name="科目编码", type="string", description="会计科目编码"),
            ExtractField(name="科目名称", type="string", description="会计科目名称"),
            ExtractField(name="借方金额", type="currency", description="借方发生额"),
            ExtractField(name="贷方金额", type="currency", description="贷方发生额"),
            ExtractField(name="币种", type="string", description="币种 (RMB / USD / ...)"),
            ExtractField(name="方向", type="string", description="借 / 贷"),
        ],
        cardinality="many",
        auto_extract_enabled=True,  # Sprint 25: cardinality=many now extracts via RowExtractor
        sample_keywords=["凭证号", "借方金额", "贷方金额", "科目编码", "摘要", "序时账"],  # Keep 序时账 keyword for legacy corpus.
        dedup_key_fields=["日期", "凭证号", "科目编码", "借方金额", "贷方金额"],  # Multiple-entry debit/credit composite + row_seq tie-breaker.
    ),
    CatalogDocType(
        name="总账",  # Sprint 25 NEW: general ledger summarized by account (5 fields, cardinality=many).
        description="按科目汇总的总账, 显期初/期末余额 + 本期发生额. 每行一个科目.",
        fields=[
            ExtractField(name="科目编码", type="string", description="会计科目编码", required=True),
            ExtractField(name="科目名称", type="string", description="会计科目名称"),
            ExtractField(name="期初余额", type="currency", description="期初余额"),
            ExtractField(name="本期借方发生", type="currency", description="本期借方发生额"),
            ExtractField(name="本期贷方发生", type="currency", description="本期贷方发生额"),
            ExtractField(name="期末余额", type="currency", description="期末余额"),
        ],
        cardinality="many",
        auto_extract_enabled=True,
        sample_keywords=["期初余额", "期末余额", "本期借方发生", "本期贷方发生"],  # General-ledger identifying keywords.
        dedup_key_fields=["科目编码"],  # Unique account identifier.
    ),
    CatalogDocType(
        name="发生额及余额表",
        description="按会计期间汇总的科目余额表",
        fields=[
            ExtractField(name="会计期间", type="string", description="如 2024-Q1", required=True),
            ExtractField(name="科目编码", type="string", required=True),
            ExtractField(name="科目名称", type="string"),
            ExtractField(name="期初借方", type="currency"),
            ExtractField(name="期初贷方", type="currency"),
            ExtractField(name="本期借方发生", type="currency"),
            ExtractField(name="本期贷方发生", type="currency"),
            ExtractField(name="期末借方", type="currency"),
            ExtractField(name="期末贷方", type="currency"),
        ],
        cardinality="many",
        auto_extract_enabled=True,  # Sprint 25: cardinality=many now extracts
        sample_keywords=["期初余额", "本期发生", "期末余额", "科目编码"],
        dedup_key_fields=["会计期间", "科目编码"],
    ),
    CatalogDocType(
        name="利润表",  # Sprint 25 NEW: single-table cardinality=one (total/net profit aggregates).
        description="损益表 / 利润表, 单表呈现期间内总收入/成本/利润数据.",
        fields=[
            ExtractField(name="报告期", type="string", description="如 '2024-01' / '2024-Q1' / '2024年度'", required=True),
            ExtractField(name="营业收入", type="currency"),
            ExtractField(name="营业成本", type="currency"),
            ExtractField(name="管理费用", type="currency"),
            ExtractField(name="销售费用", type="currency"),
            ExtractField(name="财务费用", type="currency"),
            ExtractField(name="营业利润", type="currency"),
            ExtractField(name="利润总额", type="currency"),
            ExtractField(name="净利润", type="currency"),
        ],
        cardinality="one",  # Single table, existing cardinality=one path.
        auto_extract_enabled=True,
        sample_keywords=["营业收入", "营业成本", "利润总额", "净利润", "损益表", "利润表"],
        dedup_key_fields=[],  # cardinality=one needs no dedup_key.
    ),
    CatalogDocType(
        name="资产评估报告",
        description="财产/股权评估报告的法定输出文档",
        fields=[
            ExtractField(name="报告文号", type="string", required=True),
            ExtractField(name="评估机构", type="string", required=True),
            ExtractField(name="评估基准日", type="date", required=True),
            ExtractField(name="评估对象", type="string"),
            ExtractField(name="被评估单位", type="string"),
            ExtractField(name="评估结论金额", type="currency"),
            ExtractField(name="报告日期", type="date"),
        ],
        cardinality="one",
        auto_extract_enabled=True,
        sample_keywords=["评估机构", "评估基准日", "评估结论", "报告文号"],
    ),
    CatalogDocType(
        name="税务凭证",
        description="完税证明 / VAT 凭证",
        fields=[
            ExtractField(name="完税编号", type="string", required=True),
            ExtractField(name="纳税人", type="string", required=True),
            ExtractField(name="纳税编号", type="string"),
            ExtractField(name="税款所属期", type="string"),
            ExtractField(name="实缴税额", type="currency"),
            ExtractField(name="缴款日期", type="date"),
            ExtractField(name="主管税务机关", type="string"),
        ],
        cardinality="one",
        auto_extract_enabled=True,
        sample_keywords=["完税编号", "纳税人识别号", "税款所属期", "缴款日期"],
    ),
    CatalogDocType(
        name="资产负债表",  # Sprint 26 NEW: cardinality=one BS (Balance Sheet)
        description="某时点企业资产/负债/所有者权益 stock 表. 单期数 (期末/期初对比常见).",
        fields=[
            ExtractField(name="报告期", type="string", description="如 2024-12-31", required=True),
            ExtractField(name="流动资产合计", type="currency"),
            ExtractField(name="非流动资产合计", type="currency"),
            ExtractField(name="资产总计", type="currency"),
            ExtractField(name="流动负债合计", type="currency"),
            ExtractField(name="非流动负债合计", type="currency"),
            ExtractField(name="负债合计", type="currency"),
            ExtractField(name="实收资本", type="currency"),
            ExtractField(name="未分配利润", type="currency"),
            ExtractField(name="所有者权益合计", type="currency"),
            ExtractField(name="负债和所有者权益总计", type="currency"),
        ],
        cardinality="one",
        auto_extract_enabled=True,
        sample_keywords=["流动资产", "流动负债", "所有者权益", "资产总计", "负债总计", "资产负债表"],
        dedup_key_fields=[],
    ),
    CatalogDocType(
        name="现金流量表",  # Sprint 26 NEW: cardinality=one CF (Cash Flow Statement)
        description="某期内企业现金流: 经营/投资/筹资 3 大类活动 inflows/outflows + 净额.",
        fields=[
            ExtractField(name="报告期", type="string", description="如 2024-Q3", required=True),
            ExtractField(name="经营活动现金流入小计", type="currency"),
            ExtractField(name="经营活动现金流出小计", type="currency"),
            ExtractField(name="经营活动产生的现金流量净额", type="currency"),
            ExtractField(name="投资活动现金流入小计", type="currency"),
            ExtractField(name="投资活动现金流出小计", type="currency"),
            ExtractField(name="投资活动产生的现金流量净额", type="currency"),
            ExtractField(name="筹资活动现金流入小计", type="currency"),
            ExtractField(name="筹资活动现金流出小计", type="currency"),
            ExtractField(name="筹资活动产生的现金流量净额", type="currency"),
            ExtractField(name="现金及现金等价物净增加额", type="currency"),
        ],
        cardinality="one",
        auto_extract_enabled=True,
        sample_keywords=["经营活动现金流", "投资活动现金流", "筹资活动现金流", "现金等价物净增加", "现金流量表"],
        dedup_key_fields=[],
    ),
    CatalogDocType(
        name="政府补助证书",
        description="政府补助 / 资格证书 (e.g. 高新企业认证)",
        fields=[
            ExtractField(name="证书编号", type="string", required=True),
            ExtractField(name="企业名称", type="string"),
            ExtractField(name="批准机关", type="string"),
            ExtractField(name="发证日期", type="date"),
            ExtractField(name="有效期", type="string"),
        ],
        cardinality="one",
        auto_extract_enabled=True,
        sample_keywords=["证书编号", "批准机关", "发证日期", "有效期"],
    ),
)


def get_catalog() -> tuple[CatalogDocType, ...]:
    """Return the immutable catalog tuple. Callers iterate; do not mutate."""
    return CATALOG
