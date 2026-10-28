"""前端 track() 词表 ⊆ 后端 _ALLOWED —— 跨端指标 parity（常驻闸）。

为什么要有这条：`metrics_ingest._record_metric_event` 对不在取值集合里的 label
值一律改写成 `"other"`，事件照记、名字变了。于是「前端发了、后端悄悄改名」这类
不一致**没有任何信号**——批3 新加的 `expand_table` 就这么落进 other 桶，而它正是
批2 检视层密度决策最核心的用量信号（对抗性 review 逮到）。同一次扫描还捞出两条
存量：`edit_with_ai`、`presentation`。

打法照 `test_canvas_rank_parity.py`：从 TS 源码抠字面量，与 Python 常量对拍。
只能覆盖**字面量**参数（`kind: spec.kind` 这类变量抠不出来），但漏登记的动作名
基本都是字面量，这道闸对它们有效。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from bowhead_gateway.routes.metrics_ingest import _ALLOWED

_REPO_ROOT = Path(__file__).resolve().parents[3]
_FRONTEND = _REPO_ROOT / "packages/bowhead_frontend"

# track("metric_name", { key: "literal", ... })
_TRACK_CALL = re.compile(r'track\(\s*"([a-z0-9_]+)"\s*,\s*\{(.*?)\}\s*\)', re.S)
_LITERAL_PAIR = re.compile(r'(\w+)\s*:\s*"([^"]*)"')

# Names in lib/metrics.ts's own MetricName union with no track() callers
# are outside this test's scope (a separate kind of dead-code issue).
_SKIP_DIRS = {"node_modules", ".next", "out", "test-results"}


def _frontend_sources() -> list[Path]:
    files: list[Path] = []
    for path in list(_FRONTEND.rglob("*.ts")) + list(_FRONTEND.rglob("*.tsx")):
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        if ".test." in path.name or path.name.endswith(".d.ts"):
            continue
        files.append(path)
    return files


def _collect_emits() -> list[tuple[str, str, str, Path]]:
    """→ [(metric, label_key, literal_value, source_path)]，含无 label 的 (metric, "", "", path)。"""
    out: list[tuple[str, str, str, Path]] = []
    for path in _frontend_sources():
        text = path.read_text(errors="ignore")
        if "track(" not in text:
            continue
        for metric, body in _TRACK_CALL.findall(text):
            if not metric.startswith("bowhead_") and metric != "frontend_source_lookup_empty":
                continue
            pairs = _LITERAL_PAIR.findall(body)
            if not pairs:
                out.append((metric, "", "", path))
            for key, value in pairs:
                out.append((metric, key, value, path))
    return out


@pytest.mark.unit
def test_frontend_emits_are_covered_by_gateway_allowlist() -> None:
    emits = _collect_emits()
    assert emits, "没扫到任何 track() 调用——正则或路径失效了，这条闸会假绿"

    problems: list[str] = []
    for metric, key, value, path in emits:
        rel = path.relative_to(_REPO_ROOT)
        spec = _ALLOWED.get(metric)
        if spec is None:
            problems.append(f"指标未登记: {metric} ({rel})")
            continue
        if not key:
            continue
        if key not in spec:
            problems.append(f"label 未登记（整个 label 会被忽略）: {metric}.{key}={value!r} ({rel})")
            continue
        allowed = spec[key]
        if allowed is not None and value not in allowed:
            problems.append(f"取值未登记（会被改写成 other）: {metric}.{key}={value!r} 不在 {sorted(allowed)} ({rel})")

    assert not problems, "前端发的指标/取值没在 gateway allowlist 里：\n" + "\n".join(f"  - {p}" for p in problems)


@pytest.mark.unit
def test_chart_sprint_metrics_are_registered() -> None:
    """chart-table-ux 三个新指标的取值集合逐条钉死（它们是批2 决策的数据源）。"""
    action = _ALLOWED["bowhead_chart_action_total"]["action"]
    assert action is not None
    assert {"kind_switch", "download_png", "download_csv", "open_data", "expand_table"} <= action

    assert "bowhead_chart_kind_switch_total" in _ALLOWED
    fallback = _ALLOWED["bowhead_export_legacy_fallback_total"]
    assert fallback["format"] == {"pdf", "pptx", "xlsx", "docx"}
    # R8: soft-failure fallback must leave evidence too. Production actually hit surface_projection_conflict,
    # previously absent from the table⇒"other" bucket⇒fleet completely blind to V2-wide fallback.
    assert {"v2_capability_unavailable", "forced"} <= fallback["reason"]
    assert {"surface_projection_conflict", "surface_render_incomplete"} <= fallback["reason"]


def test_export_fallback_reasons_cover_the_frontend_union() -> None:
    """取值集合必须覆盖 FE `PrepareExportFailureReason` 里所有**软**失败。

    硬拒（active_canvas_generation / rewrite_in_progress / revision_conflict）不降级、
    直接 toast error 并 return，不该出现在这个指标里。其余每一种都会走降级路，
    漏一个就是那个原因在车队里静默变成 "other"。
    """
    src = (Path(__file__).resolve().parents[2] / "bowhead_frontend/lib/canvas-export-prepare.ts").read_text(encoding="utf-8")
    union = src.split("export type PrepareExportFailureReason =", 1)[1].split(";", 1)[0]
    fe_reasons = set(re.findall(r'"([^"]+)"', union))
    assert fe_reasons, "FE PrepareExportFailureReason 解析为空——源码形态变了就更新解析器"
    hard_reject = {"active_canvas_generation", "rewrite_in_progress", "revision_conflict"}
    soft = fe_reasons - hard_reject
    allowed = _ALLOWED["bowhead_export_legacy_fallback_total"]["reason"]
    assert soft <= allowed, f"这些降级原因会落 other 桶: {sorted(soft - allowed)}"
