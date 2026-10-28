"""Deterministic purpose guard for conversational Subagent authoring."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

_MAX_PURPOSE = 2000
_GENERIC_CREATION_TERMS = (
    "subagent",
    "assistant",
    "agent",
    "bot",
    # UI now consistently calls them 智能体, but users still request 新增子代理 with the older 子代理 term; this is creation intent, not purpose.
    "子代理",
    "子智能体",
    "智能体",
    "智能體",
    "助手",
    "代理",
    "我想",
    "我要",
    "帮我",
    "幫我",
    "建个",
    "建個",
    "请",
    "請",
    "创建",
    "創建",
    "新建",
    "新增",
    "做",
    "弄",
    "一个",
    "一個",
    "create",
    "build",
    "make",
    "new",
    "i want to",
    "want to",
    "an",
    "a",
)
_MANUAL_MARKERS = (
    "手动配置",
    "手動配置",
    "自己配置",
    "自己填",
    "直接打开创建器",
    "直接打開創建器",
    "进入高级配置",
    "進入高級配置",
    "manual config",
    "configure manually",
)
_CANCEL_MARKERS = (
    "不创建了",
    "不創建了",
    "取消创建",
    "取消創建",
    "算了",
    "cancel",
    "never mind",
)
_NON_WORD = re.compile(r"[\W_]+", re.UNICODE)


class PurposeGateOutcome(StrEnum):
    PURPOSE_REQUIRED = "purpose_required"
    PURPOSE_INSUFFICIENT = "purpose_insufficient"
    SUFFICIENT = "sufficient"
    MANUAL = "manual"
    CANCELLED = "cancelled"
    TOO_LONG = "too_long"


@dataclass(frozen=True)
class PurposeEvidence:
    source_or_object: bool
    action: bool
    expected_result: bool


@dataclass(frozen=True)
class PurposeGateDecision:
    outcome: PurposeGateOutcome
    normalized_purpose: str
    policy_version: Literal["purpose-v1"] = "purpose-v1"


def _normalize(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    without_controls = "".join(char for char in normalized if unicodedata.category(char) not in {"Cc", "Cf"})
    return " ".join(without_controls.split())


def _contains_marker(value: str, markers: tuple[str, ...]) -> bool:
    folded = value.casefold()
    return any(marker.casefold() in folded for marker in markers)


def _only_generic_creation_terms(value: str) -> bool:
    remaining = value.casefold()
    for term in sorted(_GENERIC_CREATION_TERMS, key=len, reverse=True):
        folded = term.casefold()
        if folded.isascii():
            remaining = re.sub(
                rf"(?<![a-z0-9]){re.escape(folded)}(?![a-z0-9])",
                " ",
                remaining,
            )
        else:
            remaining = remaining.replace(folded, " ")
    return _NON_WORD.sub("", remaining) == ""


def evaluate_purpose_evidence(
    purpose: str,
    evidence: PurposeEvidence,
    *,
    policy_version: Literal["purpose-v1"] = "purpose-v1",
) -> PurposeGateDecision:
    if policy_version != "purpose-v1":
        raise ValueError(f"unsupported purpose policy: {policy_version}")
    normalized = _normalize(purpose)
    if len(normalized) > _MAX_PURPOSE:
        return PurposeGateDecision(PurposeGateOutcome.TOO_LONG, normalized)
    if _contains_marker(normalized, _CANCEL_MARKERS):
        return PurposeGateDecision(PurposeGateOutcome.CANCELLED, normalized)
    if _contains_marker(normalized, _MANUAL_MARKERS):
        return PurposeGateDecision(PurposeGateOutcome.MANUAL, normalized)
    if not normalized or _only_generic_creation_terms(normalized):
        return PurposeGateDecision(PurposeGateOutcome.PURPOSE_REQUIRED, normalized)
    evidence_count = sum(
        (
            evidence.source_or_object,
            evidence.action,
            evidence.expected_result,
        )
    )
    if evidence_count < 2 or not (evidence.action or evidence.expected_result):
        return PurposeGateDecision(
            PurposeGateOutcome.PURPOSE_INSUFFICIENT,
            normalized,
        )
    return PurposeGateDecision(PurposeGateOutcome.SUFFICIENT, normalized)


__all__ = [
    "PurposeEvidence",
    "PurposeGateDecision",
    "PurposeGateOutcome",
    "evaluate_purpose_evidence",
]
