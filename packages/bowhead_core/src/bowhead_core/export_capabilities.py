"""Closed per-format capability policy for Canvas ExportIR v1."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from bowhead_core.canvas_export_projection import SURFACE_KIND_POLICIES

ExportTarget = Literal[
    "pdf_full",
    "pptx_full",
    "pptx_summary",
    "xlsx_data",
]
CapabilityAction = Literal[
    "render",
    "fallback",
    "appendix",
    "summary_candidate",
    "exclude",
]


@dataclass(frozen=True, slots=True)
class SectionCapability:
    pdf_full: CapabilityAction
    pptx_full: CapabilityAction
    pptx_summary: CapabilityAction
    xlsx_data: CapabilityAction

    def for_target(self, target: ExportTarget) -> CapabilityAction:
        if target == "pdf_full":
            return self.pdf_full
        if target == "pptx_full":
            return self.pptx_full
        if target == "pptx_summary":
            return self.pptx_summary
        return self.xlsx_data


_RENDER_ALL = SectionCapability("render", "render", "render", "render")
_SUMMARY_CANDIDATE = SectionCapability(
    "render",
    "render",
    "summary_candidate",
    "render",
)
_INTERACTIVE = SectionCapability("fallback", "fallback", "exclude", "fallback")

SECTION_CAPABILITIES: dict[str, SectionCapability] = {
    "eyebrow": _RENDER_ALL,
    "h1": _RENDER_ALL,
    "deck": _RENDER_ALL,
    "meta": _RENDER_ALL,
    "kpi_row": _RENDER_ALL,
    "section": _SUMMARY_CANDIDATE,
    "narrative": _SUMMARY_CANDIDATE,
    "data_table": SectionCapability("render", "render", "appendix", "render"),
    "callout": _RENDER_ALL,
    "next_list": SectionCapability("render", "render", "exclude", "render"),
    "datarc_iframe": _INTERACTIVE,
    "html_artifact": _INTERACTIVE,
    "external_iframe": _INTERACTIVE,
    "image": _SUMMARY_CANDIDATE,
    "design": _SUMMARY_CANDIDATE,
    "chart": _SUMMARY_CANDIDATE,
    "spec_block": _SUMMARY_CANDIDATE,
    "code_block": SectionCapability("render", "appendix", "appendix", "render"),
    "sources": SectionCapability("render", "render", "appendix", "render"),
    "insight_card": _RENDER_ALL,
    "recommendations": _RENDER_ALL,
}

if SECTION_CAPABILITIES.keys() != SURFACE_KIND_POLICIES.keys():
    raise RuntimeError("export capability registry must cover the exact surface kind registry")


def capability_for_kind(
    kind: str,
    target: ExportTarget,
    *,
    include_appendix: bool,
) -> CapabilityAction:
    capability = SECTION_CAPABILITIES.get(kind)
    if capability is None:
        return "fallback"
    action = capability.for_target(target)
    if action == "appendix" and not include_appendix:
        return "exclude"
    return action


__all__ = [
    "CapabilityAction",
    "ExportTarget",
    "SECTION_CAPABILITIES",
    "SectionCapability",
    "capability_for_kind",
]
