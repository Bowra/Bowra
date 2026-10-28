"""i18n v1 · ProductTerms — L1 产品术语表加载 / agent 子集筛选 / prompt 注入.

参考设计文档 §3.2 (五层文案分类) §3.3 (glossary 格式).

L1 = 产品自身术语（canvas/agent/skill/dashboard 等），跨 frontend + backend prompt
共用单一事实来源 (`config/glossary/product_terms.yaml`)，避免术语漂移.

- `bowhead_core.product_terms`       L1 产品术语（全产品共享，YAML 单源）

prompt 注入策略：
- 按 agent tag 筛子集（Lead / Analyst / Compute / all）
- 仅注入"该 agent 会用到"的 term 控制 cache miss 规模
- 输出格式：`- canvas → 画布` 简洁列表（节省 token）
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml

logger = logging.getLogger(__name__)

# Agent tag values
AGENT_LEAD = "lead"
AGENT_ANALYST = "analyst"
AGENT_COMPUTE = "compute"
AGENT_ALL = "all"  # Special: inject into all agent prompts.

VALID_AGENTS = {AGENT_LEAD, AGENT_ANALYST, AGENT_COMPUTE, AGENT_ALL}

LanguageCode = Literal["zh-CN", "en-US", "de-DE", "fr-FR"]


@dataclass(frozen=True)
class ProductTerm:
    key: str  # e.g. "canvas"
    en: str  # e.g. "Canvas"
    zh: str  # e.g. "画布"
    agents: frozenset[str]  # Injectable agent tags ("lead"/"analyst"/.../ "all" / empty = frontend only).
    note: str = ""  # Translation rationale

    def label(self, language: LanguageCode) -> str:
        return self.zh if language == "zh-CN" else self.en

    def applies_to_agent(self, agent: str) -> bool:
        """该 term 是否需要注入给给定 agent 的 prompt."""
        if not self.agents:
            return False  # Empty = frontend only.
        if AGENT_ALL in self.agents:
            return True
        return agent in self.agents


@dataclass(frozen=True)
class ProductTermsCatalog:
    version: int
    terms: dict[str, ProductTerm]

    def filter_for_agent(self, agent: str) -> list[ProductTerm]:
        """按 agent 筛要注入 prompt 的 term，按 key 字典序返."""
        return sorted(
            (t for t in self.terms.values() if t.applies_to_agent(agent)),
            key=lambda t: t.key,
        )

    def render_prompt_section(
        self,
        agent: str,
        *,
        output_language: LanguageCode = "en-US",
    ) -> str:
        """生成 prompt 末尾段里的 glossary subset 文本。

        例（agent=lead, output_language=zh-CN）：

            - agent → 智能体
            - canvas → 画布
            - dashboard → 仪表板
            ...

        输出语言只影响"目标译法"侧；源 key 永远是英文 stable identifier。
        """
        subset = self.filter_for_agent(agent)
        if not subset:
            return ""
        lines = [f"- {t.key} → {t.label(output_language)}" for t in subset]
        return "\n".join(lines)


def load_catalog(path: str | Path) -> ProductTermsCatalog:
    """从 YAML 加载 ProductTermsCatalog. 解析失败抛 ValueError."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"product_terms.yaml not found: {p}")
    raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"glossary YAML root must be a mapping, got {type(raw).__name__}")

    version = int(raw.get("version", 1))
    raw_terms = raw.get("terms")
    if not isinstance(raw_terms, dict):
        raise ValueError("glossary YAML missing `terms` mapping")

    terms: dict[str, ProductTerm] = {}
    for key, body in raw_terms.items():
        if not isinstance(body, dict):
            logger.warning("glossary.skip_invalid_term key=%s body=%r", key, body)
            continue
        en = body.get("en")
        zh = body.get("zh")
        if not en or not zh:
            raise ValueError(f"term {key!r} missing en or zh")
        agents_raw = body.get("agents") or []
        if not isinstance(agents_raw, list):
            raise ValueError(f"term {key!r} `agents` must be a list")
        agents: set[str] = set()
        for a in agents_raw:
            if a not in VALID_AGENTS:
                raise ValueError(
                    f"term {key!r} has invalid agent tag {a!r}; valid: {sorted(VALID_AGENTS)}",
                )
            agents.add(a)
        terms[key] = ProductTerm(
            key=str(key),
            en=str(en),
            zh=str(zh),
            agents=frozenset(agents),
            note=str(body.get("note") or ""),
        )

    if not terms:
        raise ValueError("glossary YAML has zero terms")

    return ProductTermsCatalog(version=version, terms=terms)


def default_catalog_path() -> Path:
    """约定路径：config/glossary/product_terms.yaml.

    通过 module 自身位置回溯。
    """
    # bowhead_core/src/bowhead_core/product_terms.py → back to repo-skeleton
    here = Path(__file__).resolve()
    repo_root = here.parents[4]  # src/bowhead_core/, src/, bowhead_core/, packages/,
    return repo_root / "config" / "glossary" / "product_terms.yaml"
