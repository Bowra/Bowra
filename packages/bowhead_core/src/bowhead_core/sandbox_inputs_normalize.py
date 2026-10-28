"""Normalize sandbox input references at the Core allowlist boundary.

The Skill layer cannot import Core. Accept unambiguous Artifact dictionaries,
bare Artifact IDs, integer aliases, and nested input wrappers here, resolving
through the existing allowlist resolver rather than introducing another ID
parser. Leave unresolved inputs to fail downstream; never guess an Artifact.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from bowhead_core.canvas_tools import resolve_simple_data_ref

logger = logging.getLogger(__name__)

#: Two sandbox entries: flat `inputs` is top-level; `skill_invoke` nests it inside `input`.
FLAT_SANDBOX_TOOL = "python_exec"
SKILL_INVOKE_TOOL = "skill_invoke"
SANDBOX_SKILL_NAME = "python_sandbox"


def _metric(shape: str) -> None:
    """每救一次记一笔 —— 有计数才判得了「该收紧还是该继续宽容」。"""
    try:
        from bowhead_core import metrics as _m

        _m.inc(
            "bowhead_sandbox_inputs_coerced_total",
            labels={"shape": shape},
            help="沙箱 inputs 被就地纠正的次数（按原始形状分）。",
        )
    except Exception:  # noqa: S110
        logger.debug("sandbox_inputs.metric_unavailable shape=%s", shape)


def _coerce_one(name: str, spec: Any, ctx: Any) -> tuple[Any, str | None]:
    """把单个 `inputs[name]` 归一。返 `(spec, 救援形状 | None)`。"""
    # ① Bare string/integer value → treat as a reference.
    if isinstance(spec, (str, int)) and not isinstance(spec, bool):
        raw = str(spec).strip()
        if not raw:
            return spec, None
        resolved, _kind = resolve_simple_data_ref(raw, ctx)
        return {"artifact_id": resolved}, ("bare_ref" if resolved == raw else "bare_simple_int")

    if not isinstance(spec, dict):
        return spec, None

    # ② Simple-integer artifact_id → resolve through allowlist.
    aid = spec.get("artifact_id")
    if isinstance(aid, (str, int)) and not isinstance(aid, bool):
        raw = str(aid).strip()
        resolved, _kind = resolve_simple_data_ref(raw, ctx)
        if resolved != raw:
            out = dict(spec)
            out["artifact_id"] = resolved
            return out, "simple_int_artifact_id"
        if raw != aid:  # Only leading/trailing whitespace or nonstring value
            out = dict(spec)
            out["artifact_id"] = raw
            return out, "artifact_id_normalized"
    return spec, None


def normalize_sandbox_inputs(inputs: Any, ctx: Any) -> tuple[Any, list[str]]:
    """归一整个 `inputs` 映射。返 `(inputs, 救援形状列表)`；非 dict 原样放行。"""
    if not isinstance(inputs, dict) or not inputs:
        return inputs, []
    out: dict[str, Any] = {}
    shapes: list[str] = []
    for name, spec in inputs.items():
        new_spec, shape = _coerce_one(str(name), spec, ctx)
        out[str(name)] = new_spec
        if shape:
            shapes.append(shape)
    return (out, shapes) if shapes else (inputs, [])


def salvage_misnested_sandbox_args(args: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    """Recover nested sandbox arguments only when flat code or inputs are missing.

    Preserve already-correct calls while supporting the nested Skill invocation shape.
    """
    inner = args.get("input")
    if not isinstance(inner, dict):
        return args, None
    if not any(k in inner for k in ("code", "inputs")):
        return args, None
    out = dict(args)
    rescued: list[str] = []
    # ⚠ This is a **field allowlist**: add every new public input field, or a model's misplaced nesting under `input`
    # silently discards that field from normalized args.
    # `intent` **does not currently take effect through this**: normalization is a handler wrapper running **after**
    # `ToolInvocation` recording and modifying a copy, so source labels read original (still misnested) args;
    # `sources_resolver._sandbox_stated_intent` supports both holders itself. Include here for SSOT
    # parity: future consumers reading normalized args' `intent` will not hit a missing-allowlist-field trap.
    for key in ("code", "inputs", "timeout_sec", "purpose", "deliverable_outputs", "intent"):
        if key in inner and key not in out:
            out[key] = inner[key]
            rescued.append(key)
    if not rescued:
        return args, None
    out.pop("input", None)
    return out, "nested_input_" + "+".join(sorted(rescued))


def _count_intent_coverage(tool_name: str, args: dict[str, Any]) -> None:
    """`intent` 填了没 —— 逐次调用记一笔。

    schema 里 `intent` 是 required 但运行时 fail-open（没填就退回从代码里猜），所以
    「模型到底填不填」这件事**只能靠计数看见**，看不见就等于没上线。缺填率高到不能接受
    再考虑升级成硬拒——那要拿这个数去决定，不是拍脑袋。
    """
    holder = args.get("input") if tool_name == SKILL_INVOKE_TOOL else args
    stated = holder.get("intent") if isinstance(holder, dict) else None
    filled = isinstance(stated, str) and bool(stated.strip())
    try:
        from bowhead_core import metrics as _m

        _m.inc(
            "bowhead_sandbox_intent_declared_total",
            labels={"tool": tool_name, "filled": "1" if filled else "0"},
            help="沙箱调用是否带了 `intent`（用户面来源文案的一等来源）。",
        )
    except Exception:  # noqa: S110
        pass
    if not filled:
        logger.info("sandbox_inputs.intent_missing tool=%s", tool_name)


def normalize_sandbox_tool_args(tool_name: str, args: Any, ctx: Any) -> Any:
    """两个沙箱入口的统一入口。非沙箱工具 / 形状不认识 → 原样返回。"""
    if not isinstance(args, dict):
        return args
    if tool_name == FLAT_SANDBOX_TOOL:
        args, shape = salvage_misnested_sandbox_args(args)
        if shape:
            _metric(shape)
            logger.info("sandbox_inputs.salvaged tool=%s shape=%s", tool_name, shape)
        new_inputs, shapes = normalize_sandbox_inputs(args.get("inputs"), ctx)
        if shapes:
            args = dict(args)
            args["inputs"] = new_inputs
            for s in shapes:
                _metric(s)
            logger.info("sandbox_inputs.coerced tool=%s shapes=%s", tool_name, shapes)
        # Record **after** salvage: misnested `intent` under `input` just moved to top-level;
        # recording first would miscount supplied-but-misnested as absent.
        _count_intent_coverage(tool_name, args)
        return args
    if tool_name == SKILL_INVOKE_TOOL and args.get("name") == SANDBOX_SKILL_NAME:
        inner = args.get("input")
        if not isinstance(inner, dict):
            return args
        _count_intent_coverage(tool_name, args)
        new_inputs, shapes = normalize_sandbox_inputs(inner.get("inputs"), ctx)
        if not shapes:
            return args
        args = dict(args)
        args["input"] = {**inner, "inputs": new_inputs}
        for s in shapes:
            _metric(s)
        logger.info("sandbox_inputs.coerced tool=%s shapes=%s", tool_name, shapes)
        return args
    return args


# ─────────────────────────────────────────────────────────
# Hardcoded file paths in code → bind as declared inputs
# ─────────────────────────────────────────────────────────
# Two 116 turns requesting the three fastest-growing categories on 2026-09-25 (`t_239e8b6cf3dc` / `t_6acd16abddee`) wasted
# 2–3 sandbox runs each, with only two unambiguous-intent shapes:
# ① Path directly names artifact ID: `pd.read_csv('inputs/art_xxx.csv')` / `'artifacts/art_xxx.csv'`;
# model clearly identifies the data but did not declare `inputs`, or declared it under a different key.
# ② Reading a file from the prior sandbox run: `pd.read_csv('category_metrics.csv')`; each run has a fresh
# working directory, but that file is persisted as a current-turn artifact and recoverable by name.
# Replace these literals in place with `INPUTS['<key>']['path']`, add declarations, and explain the change in results
# so the model knows what happened. **Always leave unresolved paths unchanged**; downstream still raises FileNotFoundError (with available inventory).
_ART_PATH_LITERAL_RE = re.compile(r"""(?P<q>['"])(?P<path>(?:[\w.\-]*/)*(?P<aid>art_[a-f0-9]{12})(?P<ext>\.[A-Za-z0-9]{1,8})?)(?P=q)""")
_FILE_LITERAL_RE = re.compile(
    r"""(?P<q>['"])(?P<path>(?:\./|/workdir/)?(?P<name>[^'"/\\\n]{1,120}\.(?:csv|tsv|xlsx|xls|json|jsonl|parquet|txt)))(?P=q)""",
    re.IGNORECASE,
)
_WRITE_CALL_RE = r"(?:to_csv|to_excel|to_parquet|to_json|savefig|write_text|write_bytes)\s*\(\s*['\"](?:\./|/workdir/)?{name}['\"]"
_PATH_WRITE_RE = r"Path\(\s*['\"](?:\./|/workdir/)?{name}['\"]\s*\)\s*\.(?:write_text|write_bytes|open)"
_OPEN_WRITE_RE = r"open\(\s*['\"](?:\./|/workdir/)?{name}['\"]\s*,\s*(?:mode\s*=\s*)?['\"][^'\"]*[wax]"
_MAX_FILE_LOOKUPS = 10


def _is_subscript(code: str, start: int) -> bool:
    """字面量紧跟在 `[` 后面 = 它是个下标（`INPUTS['art_x']`），不是路径，别碰。"""
    i = start - 1
    while i >= 0 and code[i] in " \t":
        i -= 1
    return i >= 0 and code[i] == "["


def _code_writes(code: str, name: str) -> bool:
    esc = re.escape(name)
    return any(re.search(pat.replace("{name}", esc), code) for pat in (_WRITE_CALL_RE, _PATH_WRITE_RE, _OPEN_WRITE_RE))


def _free_key(base: str, taken: set[str]) -> str:
    key = re.sub(r"[^0-9A-Za-z_]+", "_", base).strip("_") or "prior"
    if key[0].isdigit():
        key = "f_" + key
    candidate, n = key, 2
    while candidate in taken:
        candidate, n = f"{key}_{n}", n + 1
    return candidate


def _locate(args: dict[str, Any], tool_name: str) -> dict[str, Any] | None:
    if tool_name == FLAT_SANDBOX_TOOL:
        return args
    if tool_name == SKILL_INVOKE_TOOL and args.get("name") == SANDBOX_SKILL_NAME and isinstance(args.get("input"), dict):
        return args["input"]
    return None


def candidate_prior_filenames(tool_name: str, args: Any) -> list[str]:
    """代码里以字面量读取、且本次代码自己不写的文件名（最多 `_MAX_FILE_LOOKUPS` 个）。"""
    holder = _locate(args, tool_name) if isinstance(args, dict) else None
    code = holder.get("code") if holder else None
    if not isinstance(code, str) or not code:
        return []
    names: list[str] = []
    for m in _FILE_LITERAL_RE.finditer(code):
        name = m.group("name")
        if _is_subscript(code, m.start()) or _ART_PATH_LITERAL_RE.fullmatch(m.group(0)) or name in names:
            continue
        if _code_writes(code, name):
            continue
        names.append(name)
        if len(names) >= _MAX_FILE_LOOKUPS:
            break
    return names


def bind_literal_file_refs(tool_name: str, args: Any, prior_files: dict[str, str]) -> tuple[Any, list[dict[str, str]]]:
    """把代码里的 artifact 路径 / 本 turn 已落库的文件名换成 `INPUTS[key]['path']` 并补声明。

    返 `(args, 绑定记录)`；没有可绑的就原样返回 `(args, [])`，一个字节都不改。
    """
    if not isinstance(args, dict):
        return args, []
    holder = _locate(args, tool_name)
    if holder is None or not isinstance(holder.get("code"), str):
        return args, []
    code: str = holder["code"]
    raw_inputs = holder.get("inputs")
    if raw_inputs is not None and not isinstance(raw_inputs, dict):
        return args, []  # Unknown shape: unchanged; let downstream report an explicit error
    inputs: dict[str, Any] = dict(raw_inputs or {})
    key_by_aid = {str(spec.get("artifact_id")): str(k) for k, spec in inputs.items() if isinstance(spec, dict) and spec.get("artifact_id")}
    taken = {str(k) for k in inputs}
    bound: list[dict[str, str]] = []

    def _key_for(aid: str, base: str) -> str:
        key = key_by_aid.get(aid)
        if key is None:
            key = _free_key(base, taken)
            taken.add(key)
            key_by_aid[aid] = key
            inputs[key] = {"artifact_id": aid}
        return key

    def _sub_art(m: re.Match[str]) -> str:
        path, aid = m.group("path"), m.group("aid")
        if _is_subscript(code, m.start()) or (path == aid and not m.group("ext")):
            return m.group(0)  # Subscript, or bare ID string (possibly another use); not a path
        key = _key_for(aid, aid)
        bound.append({"literal": path, "artifact_id": aid, "key": key, "shape": "artifact_path"})
        return f"INPUTS[{key!r}]['path']"

    def _sub_file(m: re.Match[str]) -> str:
        name = m.group("name")
        aid = prior_files.get(name)
        if not aid or _is_subscript(code, m.start()) or _code_writes(code, name):
            return m.group(0)
        key = _key_for(aid, name.rsplit(".", 1)[0])
        bound.append({"literal": m.group("path"), "artifact_id": aid, "key": key, "shape": "prior_run_file"})
        return f"INPUTS[{key!r}]['path']"

    new_code = _ART_PATH_LITERAL_RE.sub(_sub_art, code)
    if prior_files:
        code = new_code
        new_code = _FILE_LITERAL_RE.sub(_sub_file, code)
    if not bound:
        return args, []
    new_holder = {**holder, "code": new_code, "inputs": inputs}
    out = new_holder if holder is args else {**args, "input": new_holder}
    for rec in bound:
        _metric("literal_" + rec["shape"])
    logger.info("sandbox_inputs.literal_bound tool=%s bound=%s", tool_name, [(b["shape"], b["artifact_id"]) for b in bound])
    return out, bound


def describe_bindings(bound: list[dict[str, str]]) -> str:
    """给模型看的一句说明：它写的路径被换成了什么，下次直接这么写。"""
    parts = [f"'{b['literal']}' → INPUTS[{b['key']!r}]['path']（{b['artifact_id']}）" for b in bound]
    return (
        "代码里直接写的文件路径已自动绑定为输入：" + "；".join(parts) + "。"
        "每次运行都是全新工作目录，之前的文件不会留在盘上；下次请直接在 inputs 里声明 artifact_id，"
        "用 INPUTS['<key>']['path'] 读。"
    )
