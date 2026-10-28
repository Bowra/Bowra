"""Phase 2 — 代码 data mappers.

把 ToolInvocation 的 result 映射成 data section payload。
数字由此锁定；LLM 在 phase 3 不得再编造数值。

每个 data kind 都有独立的 `_map_<kind>` 函数，失败返回 None（outline 会跳过该段）。

数据来源速查：
    - bi_ask_question.result.metrics[0]          → kpi_row / narrative 的指标 value
    - bi_ask_question.result.explanations_sql    → sql_block
    - bi_attribute.result.{rows, measure_*}      → attribution
    - bi_list_measures / list_dimensions         → meta（推断 filters/时间）
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from bowhead_core.types import IntentType, ToolInvocation

logger = logging.getLogger(__name__)


@dataclass
class MapperInput:
    intent: IntentType
    question: str
    tool_invocations: list[ToolInvocation]
    # Sprint ι r2 P1 #3: cross-turn safety origin (PRD §10.1). Default ""
    # preserves backward compat for existing unit tests that build MapperInput
    # without turn_id; popover treats empty as "unknown" → soft no-op.
    turn_id: str = ""


# ─── code_block (Sprint ι replaces sql_block) ───────────────
# 3 trigger paths per PRD §4.A.2:
#   1. python_sandbox / skill_invoke(name="python_sandbox") → lang="python"
#   2. bi_ask_question with explanations_sql → lang="sql"
#   3. bi_attribute synthetic-SQL fallback (no explanations_sql) → lang="sql"
def _artifact_ref_from_spec(spec: Any) -> dict[str, str] | None:
    if not isinstance(spec, dict):
        return None
    aid = str(spec.get("artifact_id") or "").strip()
    if not aid:
        return None
    ref = {"artifact_id": aid}
    filename = spec.get("filename") or spec.get("name")
    if filename:
        ref["filename"] = str(filename)
    mime = spec.get("mime") or spec.get("mime_type")
    if mime:
        ref["mime"] = str(mime)
    return ref


def _map_code_block(mi: MapperInput) -> list[dict[str, Any]]:
    """Sprint ι: emit code_block sections from python_sandbox / bi_ask_question /
    bi_attribute (synthetic-SQL fallback). Replaces _map_sql_block. Returns list
    (turn may have ≥1 sandbox call); composer caller flattens into sections.
    """
    from bowhead_core.scrubber import scrub_with_count

    out: list[dict[str, Any]] = []
    for inv in mi.tool_invocations:
        if inv.error or not inv.result:
            continue

        # ── trigger 1: python_sandbox ──
        # Batch 5ⓑ: `python_exec` is the flat entrance to the same sandbox skill; failing to recognize it leaves
        # the canvas Code tab empty once compute uses that entrance.
        if inv.tool_name in ("python_sandbox", "python_exec") or (
            inv.tool_name == "skill_invoke" and isinstance(inv.arguments, dict) and inv.arguments.get("name") == "python_sandbox"
        ):
            args = inv.arguments if isinstance(inv.arguments, dict) else {}
            inner: dict[str, Any] = {}
            if isinstance(args.get("input"), dict):
                inner = args["input"]
            elif isinstance(args.get("arguments"), dict):
                inner = args["arguments"]
            else:
                inner = args
            code = str(inner.get("code") or "").strip()
            if not code:
                continue
            inputs_spec = inner.get("inputs") or {}
            input_ids: list[str] = []
            input_artifacts: list[dict[str, str]] = []
            _binding_lines: list[str] = []
            if isinstance(inputs_spec, dict):
                # Feishu bug list follow-up #23: sandbox injects data through INPUTS aliases; code uses
                # INPUTS['monthly'] instead of filenames, leaving users unable to see which CSV is referenced. Before persistence,
                # prepend binding comments mapping aliases to filenames, making code self-explanatory.
                for alias, spec in inputs_spec.items():
                    ref = _artifact_ref_from_spec(spec)
                    if ref:
                        input_artifacts.append(ref)
                        input_ids.append(ref["artifact_id"])
                        _fn = ref.get("filename") or ref["artifact_id"]
                        _binding_lines.append(f"#   INPUTS[{alias!r}] = {_fn}")
            if _binding_lines and not code.lstrip().startswith("# 输入数据"):
                code = "# 输入数据 (由沙箱通过 INPUTS 注入):\n" + "\n".join(_binding_lines) + "\n\n" + code
            r = inv.result if isinstance(inv.result, dict) else {}
            sandbox_res = r.get("result_json") if isinstance(r.get("result_json"), dict) else r
            # D17: only persist a SUCCESSFUL sandbox run. A failed/scratch run returns a
            # clean {ok: false} (inv.error is None, so the top-of-loop guard at :305 misses
            # it) — drop it so broken code doesn't leak into the canvas Code tab. Absence of
            # ok/exit_code (synthetic fixtures) → keep (use `is False` / explicit !=0).
            _outer_ok = r.get("ok")
            _inner_ok = sandbox_res.get("ok") if isinstance(sandbox_res, dict) else None
            _exit = sandbox_res.get("exit_code") if isinstance(sandbox_res, dict) else None
            if _exit is None:
                _exit = r.get("exit_code")
            if _outer_ok is False or _inner_ok is False or (_exit is not None and _exit != 0):
                continue
            outputs = sandbox_res.get("outputs") or []
            output_ids: list[str] = []
            output_artifacts: list[dict[str, str]] = []
            if isinstance(outputs, list):
                for o in outputs:
                    ref = _artifact_ref_from_spec(o)
                    if ref:
                        output_artifacts.append(ref)
                        output_ids.append(ref["artifact_id"])
            stdout_raw = str(
                r.get("stdout") or (sandbox_res.get("stdout") if isinstance(sandbox_res, dict) else "") or "",
            )
            stdout_clean, hit_counts = scrub_with_count(stdout_raw)
            if hit_counts:
                logger.info(
                    "code_block.scrubbed call_id=%s counts=%s",
                    inv.call_id,
                    hit_counts,
                )
            stdout_preview = _truncate_stdout(stdout_clean, limit=1024)
            out.append(
                {
                    "call_id": inv.call_id,
                    "turn_id": mi.turn_id,
                    "lang": "python",
                    "code": code,
                    "dur_ms": int(inv.duration_ms or 0),
                    # When the run started (ISO 8601). Shown with the run result in the citation drawer.
                    "ran_at": str(getattr(inv, "started_at", "") or ""),
                    "stdout_preview": stdout_preview,
                    "input_artifact_ids": input_ids,
                    "output_artifact_ids": output_ids,
                    "input_artifacts": input_artifacts,
                    "output_artifacts": output_artifacts,
                    "summary_payload": _summarize_python(code, inv.duration_ms or 0),
                    # Legacy alias for sql-block.tsx compat (renders code via `sql` field).
                    "sql": code,
                    "meta": {"source": inv.tool_name},
                }
            )
            continue

        # ── trigger 2: bi_ask_question with explanations_sql ──
        if inv.tool_name == "bi_ask_question" and isinstance(inv.result, dict):
            sqls = inv.result.get("explanations_sql") or []
            for idx, sql in enumerate(sqls):
                s = (sql or "").strip()
                if not s:
                    continue
                args = inv.arguments if isinstance(inv.arguments, dict) else {}
                out.append(
                    {
                        "call_id": f"{inv.call_id}#sql{idx}" if idx else inv.call_id,
                        "turn_id": mi.turn_id,
                        "lang": "sql",
                        "code": s,
                        "dur_ms": int(inv.duration_ms or 0),
                        "stdout_preview": "",
                        "input_artifact_ids": [],
                        "output_artifact_ids": [],
                        "summary_payload": _summarize_sql(s, inv.duration_ms or 0),
                        "sql": s,
                        "meta": {
                            "source": inv.tool_name,
                            "query": str(args.get("text") or args.get("question") or ""),
                        },
                    }
                )
            continue

        # ── trigger 3: bi_attribute synthetic-SQL fallback ──
        if inv.tool_name == "bi_attribute" and isinstance(inv.result, dict):
            sqls = inv.result.get("explanations_sql") or []
            if sqls:
                for idx, sql in enumerate(sqls):
                    s = (sql or "").strip()
                    if s:
                        out.append(
                            {
                                "call_id": f"{inv.call_id}#sql{idx}" if idx else inv.call_id,
                                "turn_id": mi.turn_id,
                                "lang": "sql",
                                "code": s,
                                "dur_ms": int(inv.duration_ms or 0),
                                "stdout_preview": "",
                                "input_artifact_ids": [],
                                "output_artifact_ids": [],
                                "summary_payload": _summarize_sql(s, inv.duration_ms or 0),
                                "sql": s,
                                "meta": {"source": inv.tool_name},
                            }
                        )
            else:
                # synthetic-SQL fallback (mirror answer_decomposer.py:485-509).
                args = inv.arguments if isinstance(inv.arguments, dict) else {}
                question = str(args.get("question") or "").strip()
                comparison = str(args.get("comparison_type") or "ring")
                comp_label = {"ring": "环比", "yoy": "同比"}.get(comparison, comparison)
                rows = inv.result.get("rows") or []
                n_rows = len(rows) if isinstance(rows, list) else 0
                if question:
                    pseudo = (
                        f"-- bi_attribute ({comp_label} 归因分析, 无 explanations_sql)\n"
                        f"-- 问题: {question}\n"
                        f"-- comparison_type: {comparison}\n"
                        f"-- 返 {n_rows} 条 attribution rows"
                    )
                    out.append(
                        {
                            "call_id": inv.call_id,
                            "turn_id": mi.turn_id,
                            "lang": "sql",
                            "code": pseudo,
                            "dur_ms": int(inv.duration_ms or 0),
                            "stdout_preview": "",
                            "input_artifact_ids": [],
                            "output_artifact_ids": [],
                            "summary_payload": _summarize_sql(pseudo, inv.duration_ms or 0),
                            "sql": pseudo,
                            "meta": {"source": inv.tool_name, "kind": "pseudo_attribute"},
                        }
                    )
            continue

        # ── trigger 4 (φ Phase E): bi_fetch_rows synthetic-SQL ──
        # Datarc download_data API does not return explanations_sql; synthesize SQL-shaped comments from query text + meta
        # for the Code tab, addressing users' missing-SQL complaint.
        if inv.tool_name == "bi_fetch_rows" and isinstance(inv.result, dict) and not inv.error:
            args_fr = inv.arguments if isinstance(inv.arguments, dict) else {}
            question_fr = str(args_fr.get("text") or args_fr.get("question") or "").strip()
            limit_fr = args_fr.get("limit")
            keep_cols_fr = args_fr.get("keep_columns") or args_fr.get("columns") or []
            n_rows_total = inv.result.get("total_rows") or len(inv.result.get("rows") or [])
            csv_fn = inv.result.get("artifact_filename") or inv.result.get("_debug_csv_filename") or ""
            if question_fr:
                pseudo_fr = (
                    f"-- bi_fetch_rows (datarc download_data, 无 explanations_sql)\n"
                    f"-- 问题: {question_fr}\n"
                    + (f"-- limit: {limit_fr}\n" if limit_fr else "")
                    + (f"-- keep_columns: {keep_cols_fr}\n" if keep_cols_fr else "")
                    + (f"-- artifact: {csv_fn}\n" if csv_fn else "")
                    + f"-- 返 {n_rows_total} 行"
                )
                out.append(
                    {
                        "call_id": inv.call_id,
                        "turn_id": mi.turn_id,
                        "lang": "sql",
                        "code": pseudo_fr,
                        "dur_ms": int(inv.duration_ms or 0),
                        "stdout_preview": "",
                        "input_artifact_ids": [],
                        "output_artifact_ids": ([inv.result.get("artifact_id")] if inv.result.get("artifact_id") else []),
                        "summary_payload": _summarize_sql(pseudo_fr, inv.duration_ms or 0),
                        "sql": pseudo_fr,
                        "meta": {"source": inv.tool_name, "kind": "pseudo_fetch_rows"},
                    }
                )
            continue
    return out


def _truncate_stdout(s: str, *, limit: int = 1024) -> str:
    """Inline stdout preview cap; full text persists to artifact_stdout table."""
    if not s:
        return ""
    if len(s) <= limit:
        return s
    return s[:limit] + f"\n[... {len(s) - limit} chars truncated; see Data tab ...]"


def _summarize_python(code: str, dur_ms: int) -> dict[str, Any]:
    n_lines = max(1, code.count("\n") + 1)
    summary = "python script"
    if "groupby" in code:
        summary = "pandas groupby"
    elif "merge" in code or "concat" in code:
        summary = "pandas merge/concat"
    elif "matplotlib" in code or "plt." in code:
        summary = "matplotlib plot"
    elif ".plot" in code:
        summary = "pandas plot"
    elif "describe" in code or "agg" in code:
        summary = "pandas aggregate"
    return {"n_lines": n_lines, "statement_summary": summary, "dur_ms": int(dur_ms or 0)}


def _summarize_sql(sql: str, dur_ms: int) -> dict[str, Any]:
    n_lines = max(1, sql.count("\n") + 1)
    s = sql.upper()
    summary = "SQL query"
    if "GROUP BY" in s:
        summary = "SQL groupby"
    elif "JOIN" in s:
        summary = "SQL join"
    elif "UNION" in s:
        summary = "SQL union"
    elif s.strip().startswith("--"):
        summary = "synthetic SQL"  # bi_attribute fallback
    return {"n_lines": n_lines, "statement_summary": summary, "dur_ms": int(dur_ms or 0)}


# ─── meta ───────────────────────────────────────────────────
_TIME_HINT_RE = re.compile(r"(近\s*\d+\s*(天|日|周|月|年)|今年|去年|本月|上月|\d{4}\s*年(\s*\d{1,2}\s*月)?|Q[1-4]|YTD|MTD)")
