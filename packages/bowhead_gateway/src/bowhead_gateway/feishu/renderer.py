"""Feishu card rendering for Bowhead turn events."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from bowhead_core.agents._generated.legacy_trace_names import LEGACY_TRACE_NAMES
from bowhead_core.agents._generated.tool_display import TOOL_DISPLAY
from bowhead_core.agents._generated.tool_renames import canonical_tool_name
from bowhead_core.chart.types import capped_series, downsampled_points

from bowhead_gateway.feishu.client import _content_string
from bowhead_gateway.im.blocks import resolve_markers
from bowhead_gateway.im.error_messages import im_error_message, im_interface_message, normalize_im_locale

# Unescaped pipes are the actual table-column separators.
_UNESCAPED_PIPE_RE = re.compile(r"(?<!\\)\|")
# CommonMark backslash escape = `\` + ASCII punctuation.
_MD_ESCAPE_RE = re.compile(r"\\([!-/:-@\[-`{-~])")

# ⚠ These regexes and split-column / unescape / separator-row criteria are duplicated in **four places**:
#   - `bowhead_core/report_writer_loop.py` (`_TABLE_SEP_RE` / `_UNESCAPED_PIPE_RE` / `_split_row`, canonical)
#   - `bowhead_skill/catalog/canvas-docx/scripts/render_from_canvas.py`
#   - `bowhead_skill/catalog/canvas-pdf/scripts/render_from_canvas.py`
#   - `bowhead_gateway/feishu/renderer.py` (this file)
# Not simply missing extraction: the two Skill scripts execute independently in sandboxes and **cannot import bowhead_core**.
# Update all four when any criterion changes; `report_writer_loop` is authoritative.

MAX_CARD_MARKDOWN_CHARS = 8000
MAX_STEP_PREVIEW_CHARS = 600
MAX_TABLES_PER_CARD = 5
MAX_TABLE_ROWS = 10
MAX_CHARTS_PER_CARD = 3
MAX_CHART_ROWS = 120
# Answer-card **request-body** byte cap. Feishu card-message requests cap at 30KB (error 230025); exceeding it
# prevents delivery of the whole card, leaving users nothing. Measure the real request body (card first stringified
# by `client._content_string`, then the entire request JSON-encoded; quote escaping adds about 12%), not raw component bytes.
# Reserve 2KB for outer fields such as uuid. Over budget, replace charts with notes starting at the **last chart** (answer_card).
#
# Measure the whole card, without a separate chart budget: more chart rows (backend no longer limits to 50) can
# exceed the cap alongside long text. A fixed chart budget still overflows with long text,
# while unnecessarily replacing charts that would fit alongside short text.
MAX_CARD_BODY_BYTES = 28_000


@dataclass
class FeishuStepView:
    step_id: str
    title: str
    state: str = "running"
    detail: str = ""
    output: str = ""
    step_type: str = ""
    tool_name: str | None = None
    parent_step_id: str | None = None
    concurrency_group_id: str | None = None


@dataclass
class FeishuTurnCardState:
    title: str = ""
    status: str = "running"
    narrative: str = ""
    final_answer: str = ""
    inline_artifacts: list[dict[str, Any]] = field(default_factory=list)
    steps: dict[str, FeishuStepView] = field(default_factory=dict)
    locale: object = None

    def step_count(self) -> int:
        return len(self.steps)


def processing_card(*, text: str, locale: object = None) -> dict[str, Any]:
    return _card(
        title=im_interface_message("processing_title", locale),
        template="blue",
        elements=[
            _markdown(_clip(f"**{im_interface_message('question', locale)}**\n{text or im_interface_message('received', locale)}", 1200)),
            _note(im_interface_message("processing_body", locale)),
        ],
    )


def onboarding_card(*, title: str, body: str) -> dict[str, Any]:
    return _card(
        title=title,
        template="yellow",
        elements=[_markdown(body)],
    )


def binding_required_card(*, bind_url: str, feishu_email: str | None, locale: object = None) -> dict[str, Any]:
    email_line = (
        im_interface_message("binding_email", locale, email=_escape_md(feishu_email))
        if feishu_email
        else im_interface_message("binding_no_email", locale)
    )
    body = "\n\n".join([im_interface_message("binding_intro", locale), email_line, im_interface_message("binding_scope", locale)])
    return _card(
        title=im_interface_message("binding_title", locale),
        template="yellow",
        elements=[
            _markdown(body),
            {
                "tag": "action",
                "actions": [
                    _url_button(label=im_interface_message("binding_open", locale), url=bind_url),
                ],
            },
            _note(im_interface_message("binding_done", locale)),
        ],
    )


def error_card(*, title: str, body: str) -> dict[str, Any]:
    return _card(
        title=title,
        template="red",
        elements=[_markdown(body)],
    )


def answer_card(
    text: str,
    *,
    artifacts: list[dict[str, Any]] | None = None,
    trust_line: str | None = None,
    lifecycle_line: str | None = None,
    caveat: str | None = None,
    sources_heading: str | None = None,
    sources_lines: list[str] | None = None,
    locale: object = None,
    answer_locale: object = None,
) -> dict[str, Any]:
    """The Feishu answer card. The title and the empty-answer fallback follow ``answer_locale``
    (the turn's answer language, default ``locale``); notes and source headings follow ``locale``.
    ``trust_line`` / ``caveat`` / ``sources_lines`` are populated from the canvas section tree on a report turn and omitted (silent) on a
    casual chat turn. The answer text is run through ``resolve_markers`` so ``[cite:N]`` / ``[num:…]``
    never leak into the card (R-1, §4.4 — the prior #1 Feishu defect)."""
    resolved = resolve_markers(text or "", locale=normalize_im_locale(locale))

    def build(charts_as_notes: int) -> dict[str, Any]:
        elements: list[dict[str, Any]] = []
        if trust_line:
            elements.append(_note(trust_line))
        if lifecycle_line:
            elements.append(_note(lifecycle_line))
        if caveat:
            elements.append(_note(caveat))
        elements.extend(
            _answer_elements(
                resolved or im_interface_message("completed", answer_locale or locale),
                artifacts=artifacts,
                charts_as_notes=charts_as_notes,
                locale=locale,
            )
        )
        if sources_lines:
            elements.append(_hr())
            elements.append(_markdown(f"**{sources_heading or im_interface_message('sources', locale)}**\n" + "\n".join(sources_lines)))
        return _card(title=im_interface_message("answer_title", answer_locale or locale), template="green", elements=elements)

    # Over the request-body cap, replace charts with notes from the last backward; otherwise the whole card fails delivery.
    # If all charts are replaced and text alone still exceeds the cap, return unchanged: that preexisting issue is not masked here.
    card = build(0)
    for charts_as_notes in range(1, MAX_CHARTS_PER_CARD + 1):
        if card_request_bytes(card) <= MAX_CARD_BODY_BYTES:
            break
        card = build(charts_as_notes)
    return card


def card_request_bytes(card: dict[str, Any]) -> int:
    """这张卡作为消息发出去时请求体有多少字节——与 `client.reply_message` 同一种编码。"""
    body = {"msg_type": "interactive", "content": _content_string(card)}
    return len(json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def turn_status_card(state: FeishuTurnCardState) -> dict[str, Any]:
    template = "green" if state.status == "completed" else "red" if state.status == "failed" else "blue"
    locale = state.locale
    title = state.title or im_interface_message("processing_title", locale)
    if state.status == "completed":
        title = im_interface_message("status_completed_one" if state.step_count() == 1 else "status_completed", locale, count=state.step_count())
    elif state.status == "failed":
        title = im_interface_message("status_failed", locale)
    elements: list[dict[str, Any]] = []
    # R-1: strip/resolve inline markers from the process narrative too (not just the answer card).
    narrative = resolve_markers(state.narrative, locale=normalize_im_locale(locale)).strip()
    has_final_answer = bool(state.final_answer.strip())
    show_narrative = bool(narrative) and not (
        state.status == "completed"
        and has_final_answer
        and _looks_like_duplicate_answer(narrative, resolve_markers(state.final_answer, locale=normalize_im_locale(locale)))
    )
    if state.status == "completed" and has_final_answer:
        elements.append(_note(im_interface_message("answer_sent", locale)))
        if show_narrative:
            elements.append(_markdown(_clip(f"**{im_interface_message('process', locale)}**\n{narrative}", 1200)))
    elif show_narrative:
        elements.append(_markdown(_clip(narrative, 1200)))
    if not state.steps:
        if state.status == "completed":
            elements.append(_note(im_interface_message("no_tool_steps", locale)))
        elif state.status == "failed":
            elements.append(_note(im_interface_message("no_steps", locale)))
        else:
            elements.append(_note(im_interface_message("waiting_step", locale)))
    for step in state.steps.values():
        elements.extend(_step_elements(step))
    return _card(title=title, template=template, elements=elements)


def clarification_card(payload: dict[str, Any], *, allowed_open_id: str, locale: object = None) -> dict[str, Any]:
    candidates = payload.get("candidates")
    actions: list[dict[str, Any]] = []
    if isinstance(candidates, list):
        for cand in candidates[:6]:
            if not isinstance(cand, dict):
                continue
            value = str(cand.get("value") or "")
            label = str(cand.get("label") or value)
            if not value:
                continue
            actions.append(
                _button(
                    label=label[:24],
                    value={
                        "bowhead_action": "clarification_resolve",
                        "request_id": payload.get("request_id"),
                        "turn_id": payload.get("turn_id"),
                        "chosen_value": value,
                        "allowed_open_id": allowed_open_id,
                        "locale": normalize_im_locale(locale),
                    },
                ),
            )
    intro = str(payload.get("rationale") or payload.get("token") or im_interface_message("clarification_intro", locale))
    elements: list[dict[str, Any]] = [_markdown(_clip(intro, 1200))]
    if actions:
        elements.append({"tag": "action", "actions": actions})
    else:
        elements.append(_note(im_interface_message("clarification_no_options", locale)))
    return _card(title=im_interface_message("clarification_title", locale), template="yellow", elements=elements)


def workspace_picker_card(
    workspaces: list[dict[str, Any]],
    *,
    allowed_open_id: str,
    tenant_key: str,
    chat_id: str,
    locale: object = None,
) -> dict[str, Any]:
    actions: list[dict[str, Any]] = []
    for workspace in workspaces[:10]:
        workspace_id = str(workspace.get("workspace_id") or "")
        if not workspace_id:
            continue
        name = str(workspace.get("name") or workspace_id)
        role = str(workspace.get("role") or "member")
        role_label = im_interface_message(f"role_{role}" if role in {"owner", "admin", "member"} else "role_member", locale)
        actions.append(
            _button(
                label=_clip(f"{name} ({role_label})", 24),
                value={
                    "bowhead_action": "workspace_select",
                    "allowed_open_id": allowed_open_id,
                    "tenant_key": tenant_key,
                    "chat_id": chat_id,
                    "workspace_id": workspace_id,
                    "locale": normalize_im_locale(locale),
                },
            ),
        )
    elements: list[dict[str, Any]] = [
        _markdown(im_interface_message("space_picker_intro", locale)),
    ]
    if actions:
        elements.append({"tag": "action", "actions": actions})
        elements.append(_note(im_interface_message("space_picker_selected", locale)))
    else:
        elements.append(_note(im_interface_message("space_picker_none", locale)))
    return _card(title=im_interface_message("space_picker_title", locale), template="yellow", elements=elements)


def retry_skip_ack_card(*, action: str, locale: object = None) -> dict[str, Any]:
    label = im_interface_message("retry_received", locale) if action == "retry_step" else im_interface_message("skip_received", locale)
    return _card(
        title=label,
        template="blue",
        elements=[_markdown(im_interface_message("step_ack", locale))],
    )


def apply_event_to_state(  # noqa: PLR0911, PLR0912
    state: FeishuTurnCardState,
    event_type: str,
    payload: dict[str, Any],
) -> bool:
    if event_type == "step_list_card_created":
        state.status = "running"
        return True
    if event_type == "narrative.delta":
        delta = str(payload.get("delta") or "")
        if delta:
            state.narrative = _clip(state.narrative + delta, 2500)
            return True
        return False
    if event_type == "step_started":
        step_id = str(payload.get("step_id") or "")
        if not step_id:
            return False
        title = _step_title(payload, state.locale)
        step = state.steps.get(step_id) or FeishuStepView(step_id=step_id, title=title)
        step.title = title
        step.state = "running"
        step.detail = str(payload.get("input_preview") or payload.get("arguments_preview") or "")
        step.step_type = str(payload.get("step_type") or "")
        step.tool_name = payload.get("tool_name")
        step.parent_step_id = payload.get("parent_step_id")
        step.concurrency_group_id = payload.get("concurrency_group_id")
        state.steps[step_id] = step
        return True
    if event_type == "step_progress":
        step_id = str(payload.get("step_id") or "")
        step = state.steps.get(step_id)
        if not step:
            return False
        delta = str(payload.get("content_delta") or "")
        if delta:
            step.detail = _clip(step.detail + delta, MAX_STEP_PREVIEW_CHARS)
        completed = payload.get("completed")
        total = payload.get("total")
        if completed is not None and total is not None:
            step.output = im_interface_message("progress", state.locale, completed=completed, total=total)
        return True
    if event_type == "step_done":
        step_id = str(payload.get("step_id") or "")
        step = state.steps.get(step_id)
        if not step:
            return False
        state_value = str(payload.get("state") or "success")
        step.state = "failed" if state_value == "failure" else state_value
        step.output = str(payload.get("output_preview") or payload.get("result_preview") or "")
        error = payload.get("error")
        if isinstance(error, dict):
            step.output = im_error_message(str(error.get("code") or error.get("error_code") or "lead_internal_error"), state.locale)
        return True
    if event_type == "step_list_card_finalized":
        state.status = str(payload.get("state") or "completed")
        return True
    if event_type == "final_answer":
        state.final_answer = str(payload.get("text") or "")
        artifacts = payload.get("inline_artifacts")
        state.inline_artifacts = artifacts if isinstance(artifacts, list) else []
        state.status = "completed"
        return True
    if event_type in {"turn_failed", "turn_aborted", "error"}:
        state.status = "failed"
        return True
    return event_type in {"turn.step_retry_requested", "turn.step_skip_requested"}


def _step_title(payload: dict[str, Any], locale: object) -> str:
    """Localize generated tool headers while preserving user-authored step titles."""
    tool_name = str(payload.get("tool_name") or "")
    canonical = canonical_tool_name(LEGACY_TRACE_NAMES.get(tool_name, tool_name))
    header = str(payload.get("header_zh") or "")
    entry = TOOL_DISPLAY.get(canonical)
    if entry is not None:
        writing_headers = {
            "python_sandbox": "写代码 (Python)",
            "bi_query": "写查询 (SQL)",
            "kb_search": "查知识库 (KB)",
            "web_search": "搜网络 (Web)",
        }
        generated_headers = {"", tool_name, entry["display_zh"], f"调用 {entry['display_zh']}"}
        if canonical in writing_headers:
            generated_headers.add(writing_headers[canonical])
        if header in generated_headers:
            language = normalize_im_locale(locale).split("-", 1)[0]
            return str(entry.get(f"display_{language}") or entry["display_en"])
    if tool_name.startswith("teammcp."):
        operation = tool_name.rsplit(".", 1)[-1]
        if header in {"", tool_name, f"团队工具 · {operation}", f"调用 团队工具 · {operation}"}:
            return im_interface_message("team_tool", locale, operation=operation)
    return header or tool_name or im_interface_message("step_title", locale)


def _step_elements(step: FeishuStepView) -> list[dict[str, Any]]:
    icon = "•"
    if step.state in {"success", "done"}:
        icon = "✓"
    elif step.state in {"failure", "failed"}:
        icon = "✕"
    elif step.state == "partial":
        icon = "!"
    title = f"{icon} **{_escape_md(step.title)}**"
    lines = [title]
    if step.detail:
        lines.append(_clip(_escape_md(step.detail), MAX_STEP_PREVIEW_CHARS))
    if step.output:
        lines.append(f"> {_clip(_escape_md(step.output), MAX_STEP_PREVIEW_CHARS)}")
    return [_markdown("\n".join(lines))]


def _answer_elements(
    text: str,
    *,
    artifacts: list[dict[str, Any]] | None = None,
    charts_as_notes: int = 0,
    locale: object = None,
) -> list[dict[str, Any]]:
    elements: list[dict[str, Any]] = []
    buffer: list[str] = []
    lines = text.splitlines()
    idx = 0
    tables_used = 0
    while idx < len(lines):
        table = _parse_markdown_table(lines, idx)
        if table is None or tables_used >= MAX_TABLES_PER_CARD:
            buffer.append(lines[idx])
            idx += 1
            continue
        if buffer and "".join(buffer).strip():
            elements.append(_markdown_component(_clip("\n".join(buffer).strip(), MAX_CARD_MARKDOWN_CHARS)))
        buffer = []
        elements.append(_table_component(table[0], table[1]))
        tables_used += 1
        idx = table[2]
    if buffer and "".join(buffer).strip():
        elements.append(_markdown_component(_clip("\n".join(buffer).strip(), MAX_CARD_MARKDOWN_CHARS)))
    chart_elements = _chart_components_from_artifacts(artifacts or [], charts_as_notes=charts_as_notes, locale=locale)
    if chart_elements:
        if elements:
            elements.append(_hr())
        elements.extend(chart_elements)
    return elements or [_markdown_component(im_interface_message("completed", locale))]


def _chart_components_from_artifacts(
    artifacts: list[dict[str, Any]],
    *,
    charts_as_notes: int = 0,
    locale: object = None,
) -> list[dict[str, Any]]:
    """Render card charts and disclose any that do not fit.

    At most MAX_CHARTS_PER_CARD charts fit in one card. charts_as_notes replaces
    trailing charts with web-view guidance when the answer card exceeds its limit.
    """
    drawable: list[tuple[list[dict[str, Any]], dict[str, Any]]] = []
    for artifact in artifacts:
        if not isinstance(artifact, dict) or artifact.get("kind") != "chart":
            continue
        spec = artifact.get("spec")
        if not isinstance(spec, dict):
            continue
        views = spec.get("views")
        specs = (
            [view["spec"] for view in views[:6] if isinstance(view, dict) and isinstance(view.get("spec"), dict) and not view["spec"].get("views")]
            if isinstance(views, list) and views
            else [spec]
        )
        # Each child consumes a chart slot; unsupported tables/KPI consume none.
        for child in specs:
            components = _chart_component_from_bowhead_spec(child, locale=locale)
            if components:
                drawable.append((components, child))
    # Count occupied chart slots, not components: one chart may include an explanatory `_note`.
    groups = drawable[:MAX_CHARTS_PER_CARD]
    overflow = drawable[MAX_CHARTS_PER_CARD:]
    keep = len(groups) - max(0, charts_as_notes)
    charts: list[dict[str, Any]] = []
    for index, (components, spec) in enumerate(groups):
        charts.extend(components if index < keep else [_too_big_for_card_note(spec, locale=locale)])
    if overflow:
        charts.append(_overflow_charts_note([spec for _c, spec in overflow], locale=locale))
    return charts


#: Maximum chart titles named in the overflow note; too many titles inflate the card themselves.
_MAX_NAMED_OVERFLOW_CHARTS = 3
#: Per-title character cap when naming charts. `_note` hard-truncates at 300 chars; **navigation guidance is last**.
#: Without title truncation, three long titles consume the entire Web-view instruction (review: 3×420-char titles
#: removed it). Longest title in the 116 corpus is 60 chars, so normally this does not trigger.
_MAX_OVERFLOW_TITLE_CHARS = 24


def _overflow_charts_note(specs: list[dict[str, Any]], *, locale: object = None) -> dict[str, Any]:
    """一张卡放不下这么多图时，用**一条**提示交代还有哪些，而不是逐张加一条。"""
    titles = [str(s.get("title") or im_interface_message("chart", locale)) for s in specs]

    def _short(x: str) -> str:
        x = " ".join(x.split())  # Newlines would split the note into two lines.
        return x if len(x) <= _MAX_OVERFLOW_TITLE_CHARS else x[: _MAX_OVERFLOW_TITLE_CHARS - 1] + "…"

    named = im_interface_message("list_separator", locale).join(
        im_interface_message("quote_title", locale, title=_short(x)) for x in titles[:_MAX_NAMED_OVERFLOW_CHARTS]
    )
    if len(titles) > _MAX_NAMED_OVERFLOW_CHARTS:
        named += " " + im_interface_message("other_titles", locale)
    return _note(im_interface_message("charts_overflow", locale, count=len(titles), titles=named))


def _too_big_for_card_note(spec: dict[str, Any], *, locale: object = None) -> dict[str, Any]:
    title = _clip(str(spec.get("title") or im_interface_message("chart", locale)), _MAX_OVERFLOW_TITLE_CHARS)
    rows = spec.get("rows")
    have = len(rows) if isinstance(rows, list) else 0
    total = spec.get("row_count")
    total = total if isinstance(total, int) and not isinstance(total, bool) and total > have else have
    return _note(im_interface_message("chart_too_big", locale, title=title, total=total))


def _chart_component_from_bowhead_spec(spec: dict[str, Any], *, locale: object = None) -> list[dict[str, Any]]:
    """Render every row of a chart, or direct the reader to the web view.

    A chart beyond MAX_CHART_ROWS or the card byte limit must not be silently
    truncated. Charts whose source already contains a top-N selection retain the
    same partial-data notice shown in the web interface.
    """
    rows = spec.get("rows")
    columns = spec.get("columns")
    if not isinstance(rows, list) or not rows:
        return []
    if not isinstance(columns, list) or not columns:
        return []
    dict_rows = [row for row in rows if isinstance(row, dict)]
    if len(dict_rows) > MAX_CHART_ROWS:
        return [_too_big_for_card_note(spec, locale=locale)]
    clean_rows = [{str(k): v for k, v in row.items() if _is_chart_scalar(v)} for row in dict_rows]
    if not clean_rows:
        return []
    chart_spec = _to_feishu_vchart_spec(spec, clean_rows, columns, locale=locale)
    if chart_spec is None:
        return []
    components: list[dict[str, Any]] = [
        {
            "tag": "chart",
            "aspect_ratio": "16:9",
            "color_theme": "brand",
            "preview": True,
            "chart_spec": chart_spec,
        }
    ]
    # When backend caps series (`chart/infer.py`'s `_cap_time_series`), missing rows **are** those omitted series:
    # retaining 15 of 59 provinces reduces rows 708→180. Describing rows as the first 180 items
    # confuses series count with row count. These describe the same reduction; use only the correct one, matching Web chart notes.
    sampled = downsampled_points(spec)
    if sampled is not None:
        pts_shown, pts_total = sampled
        components.append(_note(im_interface_message("chart_sampled", locale, total=pts_total, shown=pts_shown)))
    capped = capped_series(spec)
    if capped is not None:
        shown, stotal = capped
        components.append(_note(im_interface_message("chart_capped", locale, total=stotal, shown=shown)))
        # Yield only when **all row reduction comes from series capping** (`top_n` absent), matching Web chart captions.
        if spec.get("top_n") is None:
            return components
    if sampled is not None:
        return components  # Missing rows are sampled time points, not merely the first N items.
    total = spec.get("row_count")
    if isinstance(total, int) and not isinstance(total, bool) and total > len(dict_rows):
        components.append(_note(im_interface_message("chart_rows", locale, shown=len(dict_rows), total=total)))
    return components


def _to_feishu_vchart_spec(
    spec: dict[str, Any],
    rows: list[dict[str, Any]],
    columns: list[Any],
    *,
    locale: object = None,
) -> dict[str, Any] | None:
    kind = str(spec.get("kind") or "table")
    col_defs = [col for col in columns if isinstance(col, dict)]
    if kind in {"table", "kpi"}:
        return None
    if kind == "pie":
        cat = _first_col_key(col_defs, types={"categorical", "date"})
        val = _first_col_key(col_defs, types={"numeric"})
        if not cat or not val:
            return None
        return _base_vchart(
            chart_type="pie",
            title=str(spec.get("title") or im_interface_message("chart", locale)),
            rows=rows,
            extra={"categoryField": cat, "valueField": val},
        )
    if kind == "scatter":
        nums = _col_keys(col_defs, types={"numeric"})
        if len(nums) < 2:
            return None
        return _base_vchart(
            chart_type="scatter",
            title=str(spec.get("title") or im_interface_message("chart", locale)),
            rows=rows,
            extra={"xField": nums[0], "yField": nums[1]},
        )
    x_key = _first_role_key(col_defs, "x") or _first_col_key(
        col_defs,
        types={"date", "categorical"},
    )
    y_keys = _role_keys(col_defs, "y") or _col_keys(col_defs, types={"numeric"})
    if not x_key or not y_keys:
        return None
    color_key = _first_role_key(col_defs, "color")
    chart_type = "area" if kind == "area_stacked" else "line" if kind == "line" else "bar"
    if len(y_keys) > 1 and not color_key:
        rows, color_key, y_key = _wide_to_long(rows, x_key=x_key, y_keys=y_keys[:4])
    else:
        y_key = y_keys[0]
    extra: dict[str, Any] = {"xField": x_key, "yField": y_key}
    if color_key:
        extra["seriesField"] = color_key
    if kind == "hbar":
        extra["direction"] = "horizontal"
        extra["xField"] = y_key
        extra["yField"] = x_key
    if kind in {"stacked_bar", "area_stacked"} and color_key:
        extra["stack"] = True
    return _base_vchart(
        chart_type=chart_type,
        title=str(spec.get("title") or im_interface_message("chart", locale)),
        rows=rows,
        extra=extra,
    )


def _base_vchart(
    *,
    chart_type: str,
    title: str,
    rows: list[dict[str, Any]],
    extra: dict[str, Any],
) -> dict[str, Any]:
    return {
        "type": chart_type,
        "title": {"visible": True, "text": title[:80]},
        "data": {"values": rows},
        "legends": {"visible": True},
        "tooltip": {"visible": True},
        **extra,
    }


def _wide_to_long(
    rows: list[dict[str, Any]],
    *,
    x_key: str,
    y_keys: list[str],
) -> tuple[list[dict[str, Any]], str, str]:
    out: list[dict[str, Any]] = []
    for row in rows:
        for y_key in y_keys:
            out.append({x_key: row.get(x_key), "series": y_key, "value": row.get(y_key)})
    return out, "series", "value"


def _first_role_key(columns: list[dict[str, Any]], role: str) -> str | None:
    return next((str(col.get("key")) for col in columns if col.get("role") == role and col.get("key")), None)


def _role_keys(columns: list[dict[str, Any]], role: str) -> list[str]:
    return [str(col.get("key")) for col in columns if col.get("role") == role and col.get("key")]


def _first_col_key(columns: list[dict[str, Any]], *, types: set[str]) -> str | None:
    return next(
        (str(col.get("key")) for col in columns if str(col.get("type") or "") in types and col.get("key")),
        None,
    )


def _col_keys(columns: list[dict[str, Any]], *, types: set[str]) -> list[str]:
    return [str(col.get("key")) for col in columns if str(col.get("type") or "") in types and col.get("key")]


def _is_chart_scalar(value: Any) -> bool:
    return value is None or isinstance(value, str | int | float | bool)


def _parse_markdown_table(
    lines: list[str],
    start: int,
) -> tuple[list[str], list[list[str]], int] | None:
    if start + 1 >= len(lines):
        return None
    header = _split_table_row(lines[start])
    separator = _split_table_row(lines[start + 1])
    if len(header) < 2 or len(separator) != len(header):
        return None
    if not all(_is_table_separator(cell) for cell in separator):
        return None
    rows: list[list[str]] = []
    idx = start + 2
    while idx < len(lines) and len(rows) < MAX_TABLE_ROWS:
        row = _split_table_row(lines[idx])
        if len(row) != len(header):
            break
        rows.append(row)
        idx += 1
    if not rows:
        return None
    return header, rows, idx


def _split_table_row(line: str) -> list[str]:
    """只在**未转义**的竖线处切列。

    裸 `split("|")` 会把单元格内的字面竖线 `\\|` 当分隔符，该行列数 +1；而
    `_parse_markdown_table` 用列数做硬约束 ⇒ `break` ⇒ **该行及其之后的所有行被静默丢掉**
    （出现在表头行则整张表退化成裸文本）。上游 `markdown-pm.ts::_serializeTableCell`
    正是把 `|` 写成 `\\|`，往返越多越容易撞上。
    """
    stripped = line.strip()
    if "|" not in stripped:
        return []
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|") and not stripped.endswith("\\|"):
        stripped = stripped[:-1]
    return [cell.strip() for cell in _UNESCAPED_PIPE_RE.split(stripped)]


def _is_table_separator(cell: str) -> bool:
    """GFM spec：每格**一个或多个**连字符即合法，`|-|-|` / `|:-:|:-:|` 都是表。

    要求 `len >= 3` 会把这类合法表整张判不出来，退化成 `tag:"markdown"` 的裸文本。
    """
    value = cell.strip()
    if value.startswith(":"):
        value = value[1:]
    if value.endswith(":"):
        value = value[:-1]
    return bool(value) and set(value) <= {"-"}


def _table_component(headers: list[str], rows: list[list[str]]) -> dict[str, Any]:
    columns = [
        {
            "name": f"col_{idx}",
            "display_name": _strip_markdown(header)[:80],
            "data_type": "text",
            "horizontal_align": "left",
            "vertical_align": "top",
            "width": "auto",
        }
        for idx, header in enumerate(headers[:50])
    ]
    return {
        "tag": "table",
        "page_size": min(MAX_TABLE_ROWS, max(1, len(rows))),
        "row_height": "middle",
        "header_style": {
            "text_align": "left",
            "text_size": "normal",
            "background_style": "none",
            "text_color": "grey",
            "bold": True,
            "lines": 1,
        },
        "columns": columns,
        "rows": [{f"col_{idx}": _strip_markdown(cell) for idx, cell in enumerate(row[: len(columns)])} for row in rows],
    }


def _card(*, title: str, template: str, elements: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "config": {
            "wide_screen_mode": True,
            "update_multi": True,
        },
        "header": {
            "template": template,
            "title": {"tag": "plain_text", "content": title[:80]},
        },
        "elements": elements,
    }


def _markdown(content: str) -> dict[str, Any]:
    return {
        "tag": "div",
        "text": {"tag": "lark_md", "content": _clip(content, MAX_CARD_MARKDOWN_CHARS)},
    }


def _markdown_component(content: str) -> dict[str, Any]:
    return {
        "tag": "markdown",
        "content": _clip(content, MAX_CARD_MARKDOWN_CHARS),
    }


def _note(content: str) -> dict[str, Any]:
    return {
        "tag": "note",
        "elements": [{"tag": "plain_text", "content": content[:300]}],
    }


def _hr() -> dict[str, Any]:
    return {"tag": "hr"}


def _button(label: str, value: dict[str, Any]) -> dict[str, Any]:
    return {
        "tag": "button",
        "text": {"tag": "plain_text", "content": label},
        "type": "default",
        "value": value,
    }


def _url_button(label: str, url: str) -> dict[str, Any]:
    return {
        "tag": "button",
        "text": {"tag": "plain_text", "content": label},
        "type": "primary",
        "url": url,
    }


def _clip(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 20].rstrip() + "\n…"


def _escape_md(text: str) -> str:
    return text.replace("<", "&lt;").replace(">", "&gt;")


def _strip_markdown(text: str) -> str:
    """剥掉内联标记 → 纯文本（表格单元格用；卡片的 table 组件不解析 markdown）。

    末尾反转义：单元格是**渲染终点**，`\\-37.5%` / `1\\. 第一` / `a\\|b` 里的反斜杠
    必须还原，否则原样印给用户。放在最后是因为放前面会让 `\\*` 变回 `*` 再被剥掉。
    """
    stripped = text.replace("**", "").replace("__", "").replace("`", "").strip()
    return _MD_ESCAPE_RE.sub(r"\1", stripped)


def _looks_like_duplicate_answer(narrative: str, final_answer: str) -> bool:
    narrative_norm = _normalize_for_compare(narrative)
    answer_norm = _normalize_for_compare(final_answer)
    if not narrative_norm or not answer_norm:
        return False
    return answer_norm in narrative_norm or narrative_norm in answer_norm


def _normalize_for_compare(text: str) -> str:
    return "".join(_strip_markdown(text).split())
