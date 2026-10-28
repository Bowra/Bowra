"""PyCell 渲染 — 把 tool_call/tool_result 转成 Jupyter 风 In[N] 展示.

production 注意：
    - code 字段仅用于 UI 展示；不应被再次执行，也不应让用户以为是真 Python
    - 参数序列化用 repr() 以保证可读；大 object 截断
    - 输出同样截断（默认 1500 字符）防止 SSE 帧过大
"""

from __future__ import annotations

import json
from typing import Any

_MAX_ARG_REPR = 120
_MAX_OUTPUT = 1500


def render_code(tool_name: str, arguments: dict[str, Any]) -> str:
    """把 tool_name + args 渲染成伪 Python 代码字符串."""
    # Special-case `skill_invoke python_sandbox`: render user code verbatim as Python;
    # otherwise Python source becomes an unreadable JSON argument truncated to 120 characters.
    # Batch 5ⓑ: flat `python_exec` puts code at the **top level**; the old entry nests it under `input`.
    # Both need this special case; otherwise the flat entry falls into the generic branch below, truncated to 120 characters by `_compact_repr`,
    # exactly the shape this special case originally addressed.
    if (tool_name == "skill_invoke" and arguments.get("name") == "python_sandbox") or tool_name == "python_exec":
        inp = arguments.get("input") if isinstance(arguments.get("input"), dict) else arguments
        code = str(inp.get("code", "")).rstrip()
        inputs = inp.get("inputs") or {}
        timeout = inp.get("timeout_sec")
        header_parts: list[str] = ["# python_sandbox"]
        if inputs:
            keys = list(inputs.keys())[:5]
            header_parts.append(f"# inputs: {keys}")
        if timeout:
            header_parts.append(f"# timeout: {timeout}s")
        return "\n".join(header_parts) + "\n\n" + code

    parts: list[str] = []
    for k, v in arguments.items():
        parts.append(f"{k}={_compact_repr(v)}")
    args_str = ", ".join(parts)
    return f"{tool_name}({args_str})"


def render_output(result: Any, error: str | None) -> str:
    if error:
        return f"# ERROR: {error[:_MAX_OUTPUT]}"
    if result is None:
        return "None"
    # Special-case skill_invoke python_sandbox: display stdout / stderr / outputs flat,
    # without nesting another result_json wrapper.
    if isinstance(result, dict) and isinstance(result.get("result_json"), dict):
        sbx = result["result_json"]
        segs: list[str] = []
        stdout = str(sbx.get("stdout") or "").rstrip()
        if stdout:
            segs.append(stdout)
        stderr = str(sbx.get("stderr") or "").rstrip()
        if stderr:
            segs.append(f"# stderr:\n{stderr}")
        outputs = sbx.get("outputs") or []
        if outputs:
            names = ", ".join(f"{o.get('name', '?')} ({o.get('mime', '?')})" for o in outputs[:6])
            segs.append(f"# outputs → artifacts: {names}")
        dur = sbx.get("duration_ms")
        ok = sbx.get("ok")
        if dur is not None:
            segs.append(f"# {dur}ms · ok={ok}")
        if segs:
            s = "\n\n".join(segs)
            if len(s) > _MAX_OUTPUT:
                s = s[:_MAX_OUTPUT] + "\n# … truncated"
            return s

    # M layer (V2.8): detect tabular results (rows: list[dict]) → print shape + head(3)
    # to show users the data shape immediately, rather than a business summary / large JSON dump.
    table_view = _try_render_table(result)
    if table_view is not None:
        return table_view

    try:
        s = json.dumps(result, ensure_ascii=False, indent=2, default=str)
    except (TypeError, ValueError):
        s = str(result)
    if len(s) > _MAX_OUTPUT:
        s = s[:_MAX_OUTPUT] + "\n# … truncated"
    return s


def _try_render_table(result: Any) -> str | None:
    """识别 tabular 形态 → 输出 shape + head(3) (类 pandas df.head() 风).

    覆盖几种常见 BI tool result:
      - {"rows": [{...}, {...}]}            (bi_fetch_rows)
      - {"data": [{...}, {...}]}            (datarc 老 schema)
      - [{"col1": ..., "col2": ...}, ...]   (扁平列表)

    返 None 时调用方走 fallback JSON dump.
    """
    rows: list[Any] | None = None
    if isinstance(result, list) and result and all(isinstance(x, dict) for x in result):
        rows = result
    elif isinstance(result, dict):
        for key in ("rows", "data"):
            v = result.get(key)
            if isinstance(v, list) and v and all(isinstance(x, dict) for x in v):
                rows = v
                break
    if rows is None or len(rows) == 0:
        return None

    # Column names = union of the first 3 rows (supports sparse dicts)
    cols: list[str] = []
    seen: set[str] = set()
    for r in rows[:3]:
        for k in r:
            if k not in seen:
                seen.add(k)
                cols.append(str(k))
    n_rows = len(rows)
    n_cols = len(cols)

    # Render head(3) as a Markdown-like table (character-width alignment suffices)
    def _cell(v: Any) -> str:
        if v is None:
            return ""
        s = str(v)
        return s[:24] + "…" if len(s) > 24 else s

    head_rows = rows[:3]
    widths = [max(len(c), max((len(_cell(r.get(c))) for r in head_rows), default=0)) for c in cols]
    header = " | ".join(c.ljust(w) for c, w in zip(cols, widths, strict=False))
    sep = "-+-".join("-" * w for w in widths)
    body_lines = []
    for r in head_rows:
        body_lines.append(" | ".join(_cell(r.get(c)).ljust(w) for c, w in zip(cols, widths, strict=False)))

    suffix = f"\n# ... +{n_rows - 3} 行" if n_rows > 3 else ""
    out = f"# DataFrame shape=({n_rows}, {n_cols})\n{header}\n{sep}\n" + "\n".join(body_lines) + suffix
    if len(out) > _MAX_OUTPUT:
        out = out[:_MAX_OUTPUT] + "\n# … truncated"
    return out


def _compact_repr(v: Any) -> str:
    if isinstance(v, str):
        s = v.replace("\n", "\\n")
        if len(s) > _MAX_ARG_REPR:
            s = s[: _MAX_ARG_REPR - 1] + "…"
        return f'"{s}"'
    if isinstance(v, (int, float, bool)) or v is None:
        return repr(v)
    if isinstance(v, (list, dict)):
        try:
            s = json.dumps(v, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            s = str(v)
        if len(s) > _MAX_ARG_REPR:
            s = s[: _MAX_ARG_REPR - 1] + "…"
        return s
    return repr(v)[:_MAX_ARG_REPR]
