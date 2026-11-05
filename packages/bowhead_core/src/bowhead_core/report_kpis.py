"""Explicit report KPI exclusions and reconciliation with saved report content."""

from __future__ import annotations

import re
from typing import Any

_KPI = r"(?:KPIs?|关键指标)(?:\s*(?:cards?|row|band|卡片|卡|带))?"
_LIST = rf"(?:(?:HTML|PDF|图片|图像)\s*[/、,，]\s*)*{_KPI}"
_EXCLUSION = re.compile(
    rf"(?:不要|不用|无需|不需要|不加|不展示|不添加)\s*(?:再|额外|自动)?\s*(?:添加|追加|加|展示|显示|生成|包含|提供|输出|附加|补充)?\s*{_LIST}"
    rf"|(?:do\s+not|don't|never)\s+(?:add|include|show|display|generate|create)\s+(?:any\s+)?{_KPI}\b"
    rf"|(?:\bwithout|\bomit|\bexclude)\s+(?:any\s+)?{_KPI}\b"
    rf"|(?:^|[。.!?;；\n])\s*(?:please\s+)?no\s+{_KPI}\b"
    rf"|(?:keine|ohne)\s+{_KPI}\b|sans\s+{_KPI}\b",
    re.IGNORECASE,
)
_ABSENCE = re.compile(
    rf"(?:无|没有|未添加|未生成|未包含|不包含|不含)\s*{_LIST}"
    rf"|\b(?:no|without|omitted|excluded)\s+(?:any\s+)?{_KPI}\b"
    rf"|\b(?:keine|ohne|sans)\s+{_KPI}\b",
    re.IGNORECASE,
)
_COUNT = re.compile(
    rf"(?:报告|report|Bericht|rapport)[^。.!?;；\n]{{0,35}}?(\d+)\s*(?:个|张)?\s*{_KPI}"
    rf"|(?:added|included|created|generated|展示|添加|生成|包含)\s*(\d+)\s*(?:个|张)?\s*{_KPI}",
    re.IGNORECASE,
)
_PRESENCE = re.compile(
    rf"(?:报告|\breport\b)[^。.!?;；\n]{{0,25}}?(?:包含|展示|显示|includes?|contains?|shows?)\s*{_KPI}"
    rf"|(?:已|已经)\s*(?:添加|生成|展示)\s*{_KPI}"
    rf"|\b(?:added|included|created|generated)\s+(?:the\s+)?{_KPI}\b",
    re.IGNORECASE,
)
_SENTENCES = re.compile(r"(?<=[。！？；!?;\n])|(?<=\.)(?=\s)")
_QUOTED = re.compile(r'`[^`\n]*`|"[^"\n]*"|“[^”\n]*”|「[^」\n]*」|‘[^’\n]*’')
_FENCED = re.compile(r"(?ms)^\s*(```|~~~)[^\n]*\n.*?^\s*\1[^\n]*$")


def _prose(text: str) -> str:
    """Ignore quoted examples and fenced code when reading explicit instructions."""
    lines: list[str] = []
    fenced = False
    for line in text.splitlines():
        if line.lstrip().startswith(("```", "~~~")):
            fenced = not fenced
        elif not fenced and not line.lstrip().startswith(">"):
            lines.append(line)
    return _QUOTED.sub("", "\n".join(lines))


def user_excludes_report_kpis(text: str) -> bool:
    """Recognize explicit KPI omission requests, never infer them from task type."""
    return bool(_EXCLUSION.search(_prose(text)))


def report_kpi_count(sections: list[Any]) -> int:
    """Count active saved KPI cells; hidden, rejected and historical sections do not count."""
    return sum(
        len(section.payload.get("cells") or [])
        for section in sections
        if section.kind == "kpi_row"
        and isinstance(section.payload, dict)
        and not any(getattr(section, flag, False) for flag in ("hidden_for_chat_mode", "superseded", "quarantined"))
    )


def reconcile_report_kpi_claims(text: str, *, count: int, language: str) -> str:
    """Replace explicit false report-structure claims with the saved KPI count."""
    facts = {
        "zh-CN": "报告包含 {count} 张 KPI 卡片。" if count else "报告未包含 KPI 卡片。",
        "en-US": "The report contains {count} KPI cards." if count else "The report contains no KPI cards.",
        "de-DE": "Der Bericht enthält {count} KPI-Karten." if count else "Der Bericht enthält keine KPI-Karten.",
        "fr-FR": "Le rapport contient {count} cartes KPI." if count else "Le rapport ne contient aucune carte KPI.",
    }
    if count == 1:
        facts.update(
            {"en-US": "The report contains 1 KPI card.", "de-DE": "Der Bericht enthält 1 KPI-Karte.", "fr-FR": "Le rapport contient 1 carte KPI."}
        )
    corrected: list[str] = []
    protected = [match.span() for match in _FENCED.finditer(text)]
    offset = 0
    for sentence in _SENTENCES.split(text):
        end = offset + len(sentence)
        if any(start < end and stop > offset for start, stop in protected):
            corrected.append(sentence)
            offset = end
            continue
        offset = end
        prose = _prose(sentence)
        declared = [int(match[1] or match[2]) for match in _COUNT.finditer(prose)]
        absence = _ABSENCE.search(prose)
        report_claim = absence is not None and (
            not prose[: absence.start()].strip()
            or bool(re.search(r"报告|\breport\b|\bBericht\b|\brapport\b|[，,:：]\s*$", prose[: absence.start()], re.IGNORECASE))
        )
        mismatch = count > 0 and report_claim or any(value != count for value in declared) or count == 0 and bool(_PRESENCE.search(prose))
        if mismatch:
            leading = sentence[: len(sentence) - len(sentence.lstrip())]
            trailing = sentence[len(sentence.rstrip()) :]
            corrected.append(leading + facts.get(language, facts["en-US"]).format(count=count) + trailing)
        else:
            corrected.append(sentence)
    return "".join(corrected)


async def reconcile_saved_report_kpis(store: Any, session_id: str, text: str, language_context: Any) -> str:
    """Read the actual report after composition; unavailable evidence leaves prose unchanged."""
    from bowhead_core.report_chrome_text import chrome_lang

    canvas = await store.get_canvas_by_session(session_id)
    if canvas is None:
        return text
    sections = await store.list_sections(canvas.id)
    if not any(s.kind in {"h1", "narrative", "chart", "kpi_row"} and not getattr(s, "superseded", False) for s in sections):
        return text
    return reconcile_report_kpi_claims(text, count=report_kpi_count(sections), language=chrome_lang(language_context))
