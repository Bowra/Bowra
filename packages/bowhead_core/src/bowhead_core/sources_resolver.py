"""Phase F — sources[] activation + [N] numeric citation resolver.

Sprint agent-redesign-1 (2026-05-15).

Activates the dormant `AnalysisPayload.sources[]` field as the canonical citation
primitive, and rewrites legacy ν1 inline markers to numbered `[N]` form in
narrative text:

    [num:V:U:src:CALL_ID]   → [N]     where N indexes sources[N-1]
    [ref:CHUNK_ID]           → [N]     same convention

Old-format markers without resolvable source are preserved as-is (frontend
citation-plugin still renders them via legacy regex; back-compat).

Module is intentionally separate from `canvas_composer.py` to keep composer hot
path slim and make Phase F testable in isolation.

Wire site: `canvas_composer.compose_canvas_from_payload` post-binding pass.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from collections import defaultdict
from typing import Any

logger = logging.getLogger(__name__)

_FALSY = {"0", "false", "no", "off"}


def is_protected_sources_section(kind: Any) -> bool:
    """来源段能不能删——**单一判据**，删除有两套实现（store 与 agent 原子操作）。

    画布作用域下来源段是整份文档共用的一件：删掉它，**所有轮次**的 `[N]` 一起哑，
    而且下一轮回灌找不到段会从 `[1]` 重建 ⇒ 历史 `[N]` 从「哑掉」变成「指错」。
    turn 作用域（flag 关）下它只属于一轮，照旧可删。

    判据与 tech-design-document-minimalism §0 一致：来源块是「可追溯」的载体，
    与身份（标题）同级，不属于「其余归作者」。
    """
    return str(kind) == "sources"


def _section_entries(section: Any) -> Any:
    return (getattr(section, "payload", None) or {}).get("entries")


def qualifying_sources_sections(sections: Any) -> list[Any]:
    """满足 INV-3 的 sources 段（按传入序 = seq 序）。

    三个条件与前端 `canvas-context.tsx` 的 `numberedSources` 循环**逐条对齐**：
    `kind=='sources'` + 未 superseded + `entries` 是数组。少一条就会出现
    「后端往一个前端根本不读的段里写」——INV-3 要防的正是这类事故。
    """
    return [
        s for s in sections if getattr(s, "kind", None) == "sources" and not getattr(s, "superseded", False) and isinstance(_section_entries(s), list)
    ]


def find_canvas_sources_section(sections: Any, *, turn_id: str | None) -> tuple[Any, bool]:
    """本次 upsert / 回灌要落到哪一段，以及**用的是哪种作用域**。

    返回 `(section | None, canvas_scope)`：

    - flag 开 **且该画布还没分叉**（合格段 ≤1）→ 画布作用域：第一个合格段（与读取侧同判据）。
    - 否则 → turn 作用域，**与今天逐字节相同**（注意这条不要求 entries 是数组）。

    「已分叉就不合并」是存量闸（tech-design §2.5）：合并会让 `numberedLen` 变长，
    历史上因 `N > numberedLen` 而静默哑掉的 chip 会复活成**会点、指错**的 chip
    （116 实测 25 个，分布在 5 个画布）——沉默的错变成自信的错，比现状更糟。

    作用域是**一处判定、两处消费**（upsert 与回灌），别在调用方各写一遍：
    读写选不到同一段正是 INV-3 要防的事故。
    """
    qualifying = qualifying_sources_sections(sections)
    if len(qualifying) <= 1:
        return (qualifying[0] if qualifying else None), True
    for s in sections:
        if getattr(s, "kind", None) == "sources" and getattr(s, "turn_id", None) == turn_id and not getattr(s, "superseded", False):
            return s, False
    return None, False


# Batch 5ⓑ: flat sandbox tool names. A separate constant makes remaining sandbox name checks
# searchable by grep (same convention as subagent.py / resource_access.py / answer_decomposer.py).
_FLAT_SANDBOX_TOOL = "python_exec"


# Mirror ν1 marker regexes (must match `_ref_validation._NUM_RE` + `_REF_RE`).
#
# Real prod data (t_136ece61, 2026-05-15) showed LLM emits *multiple* shapes:
#   1. Standard ν1:        [num:93.35:%:src:tl_79cf9d25c029]
#   2. Value+unit smushed: [num:136.8%:src:tl_79cf9d25c029]      ← prod regression
#   3. With sign smushed:  [num:+60.2%:src:tl_eca592082f2b]      ← prod regression
#   4. Integer smushed:    [num:85%:src:tl_79cf9d25c029]          ← prod regression
#
# Phase F resolver matches all four shapes:
_NUM_STANDARD_RE = re.compile(r"\[num:(-?\d+(?:\.\d+)?(?:e-?\d+)?):([^:\]#]*):src:([^\]#]+)(?:#highlight=([^\]]+))?\]")
# Smushed: value-and-unit are one token (no `:` between them). Be permissive:
# accept any non-`:` chars up to `:src:`. Strip leading sign+digit+. into value.
_NUM_SMUSHED_RE = re.compile(r"\[num:([^:\]#]+):src:([^\]#]+)(?:#highlight=([^\]]+))?\]")
_REF_RE = re.compile(r"\[ref:([^\]]+)\]")

# Map ASCII enum units emitted by convert (citation-audit impl-review C-P1-4:
# "4.18 percent [1]" never matches FE CITED_NUM_RE → verified-edit-guard fails).
# Semantically equivalent subset of citation_marks._ASCII_UNIT_MAP; M3 consolidates into one source (debt 5).
_CONVERT_UNIT_MAP = {
    "percent": "%",
    "ratio": "倍",
    "rate": "倍",
    "CNY": "元",
    "USD": "美元",
    "EUR": "欧元",
    "JPY": "日元",
    "count": "",
    "duration_days": "天",
    "duration_hours": "小时",
}


def _split_smushed_val_unit(val_unit: str) -> tuple[str, str]:
    """Split '136.8%' → ('136.8', '%'). Handles +/- prefix + decimals.

    Examples:
      '136.8%'  → ('136.8', '%')
      '+60.2%'  → ('+60.2', '%')
      '85%'     → ('85', '%')
      '12.3万元' → ('12.3', '万元')
      '500'      → ('500', '')
    """
    m = re.match(r"^([+-]?\d+(?:\.\d+)?(?:e-?\d+)?)(.*)$", val_unit.strip())
    if not m:
        return val_unit.strip(), ""
    return m.group(1), m.group(2).strip()


def build_sources_list(
    payload_sources: list[dict[str, Any]] | None,
    tool_invocations: list[Any] | None,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Build canonical sources list + call_id/chunk_id → index map.

    Priority:
      1. Start with `payload.sources[]` if peer emitted it (Phase F prompt directive).
      2. Augment from tool_invocations for any call_id that appears in inline
         markers but is missing from peer-emitted sources (back-compat for peers
         that emit `[num:V:U:src:S]` but don't populate `sources[]`).

    Returns:
      (sources_list, id_to_index): sources_list is the ordered final list
      (peer-emitted first, augmented at end). id_to_index maps both call_id
      and chunk_id → 1-based index (the N value used in `[N]` markers).
    """
    sources: list[dict[str, Any]] = []
    id_to_index: dict[str, int] = {}

    # Pass 1: peer-emitted sources
    # Sprint chat-stream-fix D-1 (2026-05-16): skip entries with neither label
    # NOR artifact_id — these otherwise become empty `{}` after
    # canvas_composer's empty-string filter (L917-920) and produce broken
    # numbered popovers (sources[0]/[1] empty in prod trace
    # sec_9daba9ea72b5). Synthesise label from tool+query when label empty
    # but artifact present, so `[N]` always resolves to something.
    for s in payload_sources or []:
        if not isinstance(s, dict):
            continue
        artifact_id = s.get("artifact_id") or None
        # Review R2-4: normalize peer-reported entries too (third site). `_add_entry` uses normalized
        # `(tool, query)` to bridge self-reported entries without aid to real entries. If self-reporting uses
        # `python_exec` but the system uses `python_sandbox`, the bridge fails, producing two adjacent source rows,
        # one rendered unavailable: exactly what this bridge must prevent.
        tool = surface_sandbox_tool_name(str(s.get("tool") or ""), None)
        query = s.get("query") or None
        label = str(s.get("label") or "").strip()
        if not label and not artifact_id:
            # entirely empty placeholder — peer sent `{}` or `{label: ""}` —
            # drop so it doesn't pollute numbered index.
            continue
        if not label:
            # synthesize from tool+query so canvas_composer's empty-filter
            # keeps the field. Format mirrors Pass 2 below for consistency.
            _tl = _source_tool_label(tool)
            label = _clip_label(f"{_tl}: {query}", 120) if (tool and query) else (f"{_tl}: {artifact_id}" if tool else f"artifact: {artifact_id}")
        entry = {
            "label": label,
            "tool": tool,
            "artifact_id": artifact_id,
            "query": query,
        }
        # citation-hover original text: preserve a peer- or registry-supplied inline-data
        # preview (bi_query sample_rows) so it survives the Phase-F rebuild into
        # the popover, instead of being flattened to the 4 string fields above.
        _pv_cols = s.get("preview_columns")
        _pv_rows = s.get("preview_rows")
        if isinstance(_pv_cols, list) and isinstance(_pv_rows, list) and _pv_rows:
            entry["preview_columns"] = _pv_cols
            entry["preview_rows"] = _pv_rows
            _tr = s.get("total_rows")
            if isinstance(_tr, int) and not isinstance(_tr, bool):
                entry["total_rows"] = _tr
        sources.append(entry)
        # Map known IDs (artifact_id is most common, but peer might tag others)
        n = len(sources)  # 1-based
        if entry["artifact_id"]:
            id_to_index.setdefault(str(entry["artifact_id"]), n)

    # Pass 2: augment from tool_invocations — add any artifact_id / call_id /
    # chunk_id referenced in inline markers but not yet in id_to_index.
    if tool_invocations:
        for inv in tool_invocations:
            # ToolInvocation may be dataclass or dict; tolerate both.
            call_id = _get_attr(inv, "call_id") or _get_attr(inv, "id")
            tool_name = _get_attr(inv, "tool_name") or _get_attr(inv, "tool") or ""
            result = _get_attr(inv, "result") or {}
            args = _get_attr(inv, "arguments") or {}
            query_text = _extract_query_text(args, tool_name)
            # bi_query / sandbox: artifact_id is the typical source key
            artifact_id = _extract_artifact_id(result)
            # kb_search: chunks list
            chunks = _extract_chunks(result)

            if call_id and str(call_id) not in id_to_index:
                # Reserve a slot for call_id-based refs even if not used by peer
                # — we add lazily on first reference (see resolve_in_text).
                pass  # will add lazily

            # Pre-add KB chunks if they're referenced via [ref:chunk_xxx]
            for ch_id in chunks:
                if str(ch_id) not in id_to_index:
                    entry = {
                        "label": f"KB 文档片段 {ch_id[:12]}",
                        "tool": "kb_search",
                        "artifact_id": str(ch_id),
                        "query": query_text or None,
                    }
                    sources.append(entry)
                    id_to_index[str(ch_id)] = len(sources)

    return sources, id_to_index


def _get_attr(obj: Any, name: str) -> Any:
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _is_sandbox_tool(tool_name: str, args: Any) -> bool:
    """批 5ⓑ 三入口：老伪名 / `skill_invoke(name=python_sandbox)` / 扁平 `python_exec`。"""
    if tool_name in ("python_sandbox", _FLAT_SANDBOX_TOOL):
        return True
    return tool_name == "skill_invoke" and isinstance(args, dict) and args.get("name") == "python_sandbox"


def surface_sandbox_tool_name(tool_name: str, args: Any) -> str:
    """展示面工具名——沙箱三入口一律收敛回 `python_sandbox`（批 5ⓑ既定处置，
    同 `subagent._surface_inner_skill_name`）。

    真实理由（2026-08-29 真机 `t_1831b22ed3c1`：Sources 第 1 条显示 `python_exec`，
    e2-r2 让懒注册条目首次真产出后暴露）：`label` 直接渲给用户，内部入口名对读者
    没有意义；`tool` 收敛则让同一次沙箱调用在两条登记路（peer 自报 / 系统懒注册）
    上取值一致。
    ⚠ review F5 订正：**不要**把理由写成「FE 按 `python_sandbox` 匹配来源徽章」——
    `data-tab-files.tsx:109/904` 匹配的是 **artifact 自身的 `source` 字段**（runner
    对三个入口一律上传成 `python_sandbox`），不是 `sources[].tool`；本仓没有按
    `sources[].tool` 出徽章的消费方（`sections/sources.tsx` 只渲序号）。
    """
    return "python_sandbox" if _is_sandbox_tool(tool_name, args) else tool_name


# Decorative comment strings (banner `===` / `---` / `###` / `***`); strip them to reveal intent.
# ⚠ Review R3-2: do not use prefix matching to classify these as noise; it would discard `# === 计算营业额 ===` entirely,
# though banners commonly declare agent intent. Strip decorations first, then check whether content remains.
_COMMENT_DECOR_RE = re.compile(r"^[=\-#*~_\s]+|[=\-#*~_\s]+$")
# Still uninformative after stripping: encoding declarations, shebang residue
_NOISE_COMMENT_RE = re.compile(r"coding[:=]|^!")


def _comment_intent_text(stripped_line: str) -> str:
    """把一行注释还原成「意图文本」，无意图返回空串。"""
    text = _COMMENT_DECOR_RE.sub("", stripped_line.lstrip("#").strip())
    return "" if not text or _NOISE_COMMENT_RE.search(text) else text


def _sandbox_code(args: Any) -> str | None:
    """沙箱调用里的 code（扁平入口在顶层、老入口在 `input` 子对象）。"""
    if not isinstance(args, dict):
        return None
    inner = args.get("input") if isinstance(args.get("input"), dict) else args
    code = inner.get("code") if isinstance(inner, dict) else None
    return code if isinstance(code, str) and code.strip() else None


SANDBOX_INTENT_FIELD = "intent"


def _sandbox_stated_intent(args: Any) -> str | None:
    """agent 在 `intent` 里**明说**的「这次在算什么」——沙箱标签的第一优先。

    为什么要有这个字段（而不是继续刮注释）：注释是给读代码的人用的，我们把它当界面
    文案，于是它有一半时间不存在——116 十四天语料里 agent 自发写意图注释的覆盖率只有
    **45–49%**，剩下那一半只能拿代码行凑，而首行代码几乎恒为 `df = pd.read_csv(...)`。
    刮注释降级成兜底档（下面 `_sandbox_intent_comment`）。

    **fail-open**：字段缺失 / 非字符串 / 空白一律当没填，继续走下面的档位，不报错。
    schema 里声明为 required 是为了让模型每次都被问到；真硬拒会把「模型偶尔忘填」
    变成「烧掉一整个 round trip」，对一个标签不值得。缺填率靠 metric 观测，
    要不要升级成硬拒等有数再说。
    """
    if not isinstance(args, dict):
        return None
    for holder in (args, args.get("input")):
        if not isinstance(holder, dict):
            continue
        stated = holder.get(SANDBOX_INTENT_FIELD)
        if isinstance(stated, str) and stated.strip():
            return stated.strip()[:60]
    return None


def _sandbox_intent_comment(args: Any) -> str | None:
    """**序言区**（空行 / import / 注释）里 agent 写的第一条实义注释——遇到第一条
    可执行语句就停。

    review R3-1：扫满前 N 行会捡到 agent **注释掉的调试代码**
    （`# df = df.head(100)  # 调试`）或多行字符串里的 markdown 标题
    （`tpl = \"\"\"\\n# 报告标题\\n\"\"\"`）——把一行从没运行过的代码当成数字的出处展示，
    是错误归属，比标签难看严重得多。
    """
    code = _sandbox_code(args)
    if code is None:
        return None
    for line in code.splitlines()[:12]:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            text = _comment_intent_text(stripped)
            if text:
                return text[:60]
            continue
        if stripped.startswith(("import ", "from ")):
            continue
        break  # First executable statement → end of preamble
    return None


def _sandbox_output_filename(inv: Any, artifact_id: Any) -> str | None:
    """这一条来源指向的沙箱产物**自己的文件名**，stdout 返 None。

    判据是 runner 写的 `data_role`、**不是文件名**（同 `skill_bridge` 那条判据，
    别改成按名字判：用户自己也能在 workdir 写一个叫 `sandbox-stdout.txt` 的文件）。
    116 实测真值域只有两个：`'stdout'` 与 `None`（文件产物不带 role）。

    为什么这里不需要 `enrich_source_labels` 那样的 owner 校验：那个函数拿 label 里的
    **任意 artifact id** 去 store 查文件名，id 可能指向别人的 artifact；这里的文件名
    只来自**本轮这次调用自己的 result**，是本轮沙箱刚写出来的东西，不存在跨用户面。
    """
    result = _get_attr(inv, "result")
    if not isinstance(result, dict):
        return None
    items = result.get("output_artifacts")
    if not isinstance(items, list):
        return None
    want = str(artifact_id or "")
    for item in items:
        if not isinstance(item, dict):
            continue
        if want and str(item.get("artifact_id") or "") != want:
            continue
        if str(item.get("data_role") or "") == "stdout":
            return None
        filename = str(item.get("filename") or "").strip()
        if filename:
            return filename
    return None


def _sandbox_code_summary(args: Any, produced_file: str | None = None) -> str | None:
    """沙箱调用的可读摘要（扁平入口 code 在顶层、老入口在 `input` 子对象——判据
    与 `pycell` 同源）。

    **档位（改一档必须同步改本注释）**：⓿ `intent` 字段（agent 明说）→ ① 代码开头的
    意图注释（刮出来的，`intent` 落地前的老行为）→ ② 本次运行产出的文件 → ③ 首行代码。

    **优先开头的意图注释**：agent 写的 code 几乎都是 `import …` 起手、紧接着一行
    `# 这段在算什么`。2026-09-02 真机 dogfood 逮到初版「取首个非注释语句」正好把
    有信息量的那半跳过了，Sources 里赫然写着 `python_sandbox: import pandas as pd`
    ——比修之前的裸 `python_exec` 好不到哪去。没有可用注释才退首个非 import 语句。

    **注释之后、代码之前，插一档「这次产出的文件」**（2026-09-10，116 十四天语料）：
    没写意图注释时旧行为落到首行代码，而首行代码几乎恒为 `df = pd.read_csv(INPUTS…)`
    ——一轮里跑几次沙箱就有几条一模一样的来源，实测 **有 ≥2 条沙箱来源的报告里
    64% 出现字面重复**，且这一档自身的撞车率 94%。产出文件是**这一次运行独有**的
    （`out_prov.csv` / `refund_top15.csv`），既能区分又比代码行像话。
    量过的三条替代方案都不行，别再走回头路：`INPUTS` 键名（同一轮反复读同一份输入，
    64%→64%）、跳过读输入取「真正在算的那行」（能分开但渲成
    `print(df.columns.tolist())`）、产出列名（库里没存列元数据）。
    """
    # ⓿ Intent explicitly stated in the agent's `intent` field: first-class input, ahead of all inference.
    stated = _sandbox_stated_intent(args)
    if stated:
        return stated
    # ① Fall back to the leading intent comment (legacy behavior before `intent`, now a fallback).
    intent = _sandbox_intent_comment(args)
    if intent:
        return intent
    # ② File produced by this run
    if produced_file:
        return produced_file[:60]
    code = _sandbox_code(args)
    if code is None:
        return None
    lines = code.splitlines()
    # ③ Fall back to the first substantive non-import statement
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "import ", "from ")):
            continue
        return stripped[:60]
    # ④ Fallback: code containing only imports (+ no intent comment) still needs literal text, or
    # `_extract_query_text` falls back to bare "python_sandbox", producing the label
    # `python_sandbox: python_sandbox`, worse than before (review R3-3).
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return stripped[:60]
    return None


def _extract_query_text(args: Any, tool_name: str, produced_file: str | None = None) -> str | None:
    if not isinstance(args, dict):
        return None
    if tool_name in ("bi_query", "bi_fetch_rows"):
        q = args.get("text") or args.get("query")
        if isinstance(q, str):
            return q[:200]
        qs = args.get("queries")
        if isinstance(qs, list) and qs:
            return str(qs[0])[:200]
    if _is_sandbox_tool(tool_name, args):
        # Legacy behavior returned bare "python_sandbox" (label became "python_sandbox: python_sandbox").
        # Use the first code statement instead: still no invented copy, but users opening the source can recognize the computation.
        return _sandbox_code_summary(args, produced_file) or "python_sandbox"
    if tool_name == "kb_search":
        return args.get("query") or args.get("text")
    return None


def _extract_artifact_id(result: Any) -> str | None:
    if not isinstance(result, dict):
        return None
    aid = result.get("artifact_id") or result.get("_ref_artifact_id")
    if aid:
        return str(aid)
    # batch shape {results: [...]}
    res_list = result.get("results")
    if isinstance(res_list, list) and res_list:
        first = res_list[0]
        if isinstance(first, dict):
            return first.get("artifact_id") or first.get("_ref_artifact_id")
    return None


def _extract_chunks(result: Any) -> list[str]:
    """kb_search result chunks."""
    if not isinstance(result, dict):
        return []
    out: list[str] = []
    # kb_search shape per ν1: {chunks: [{chunk_id, ...}]}
    chunks = result.get("chunks") or result.get("hits") or []
    if isinstance(chunks, list):
        for ch in chunks:
            if isinstance(ch, dict):
                cid = ch.get("chunk_id") or ch.get("id")
                if cid:
                    out.append(str(cid))
    return out


def resolve_in_text(
    text: str,
    *,
    sources: list[dict[str, Any]],
    id_to_index: dict[str, int],
    tool_invocations: list[Any] | None = None,
) -> tuple[str, list[dict[str, Any]], dict[str, int]]:
    """Rewrite [num:V:U:src:S] and [ref:S] markers in text → [N].

    May extend `sources` + `id_to_index` lazily if a referenced ID is found in
    tool_invocations but not yet indexed.

    Returns (new_text, updated_sources, updated_id_to_index). Caller should reuse
    the updated lists/maps for subsequent text fields on the same payload to
    keep N values consistent across the whole report.
    """
    if not isinstance(text, str) or not text:
        return text, sources, id_to_index

    inv_lookup = _build_inv_lookup(tool_invocations or [])

    def _alloc_index(src_id: str) -> int:
        return _alloc_source_index(
            src_id,
            sources=sources,
            id_to_index=id_to_index,
            inv_lookup=inv_lookup,
        )

    def _replace_num_standard(m: re.Match[str]) -> str:
        value, unit, src_id, _highlight = m.group(1), m.group(2), m.group(3), m.group(4)
        n = _alloc_index(src_id)
        unit = _CONVERT_UNIT_MAP.get(unit, unit)
        unit_str = f" {unit}" if unit else ""
        return f"{value}{unit_str} [{n}]"

    def _replace_num_smushed(m: re.Match[str]) -> str:
        val_unit, src_id, _highlight = m.group(1), m.group(2), m.group(3)
        value, unit = _split_smushed_val_unit(val_unit)
        n = _alloc_index(src_id)
        unit_str = f" {unit}" if unit else ""
        return f"{value}{unit_str} [{n}]"

    def _replace_ref(m: re.Match[str]) -> str:
        ref_id = m.group(1)
        n = _alloc_index(ref_id)
        return f"[{n}]"

    # Order matters: standard regex first (`val:unit:src:S`), then smushed
    # (`valunit:src:S`), then plain `[ref:S]`. The smushed pattern is a strict
    # subset shape so it would also match standard — we run standard first
    # then smushed only matches what's left.
    new_text = _NUM_STANDARD_RE.sub(_replace_num_standard, text)
    new_text = _NUM_SMUSHED_RE.sub(_replace_num_smushed, new_text)
    new_text = _REF_RE.sub(_replace_ref, new_text)
    return new_text, sources, id_to_index


def _alloc_source_index(
    src_id: str,
    *,
    sources: list[dict[str, Any]],
    id_to_index: dict[str, int],
    inv_lookup: dict[str, Any],
) -> int:
    """Return existing index for src_id, or lazily add a new source entry.

    共享给 resolve_in_text (marker 改写) 与 resolve_bare_artifact_ids
    (裸 id 改写) — 同一个 turn 内两条路必须分配一致的 [N]。"""
    n = id_to_index.get(src_id)
    if n is not None:
        return n
    # Lazy add: look up in tool_invocations
    inv = inv_lookup.get(src_id)
    if inv is not None:
        # Normalize presentation (batch 5ⓑ): all three sandbox entrances use `python_sandbox`; otherwise the flat entrance's
        # source has no FE badge and its label is an internal tool name.
        tool = surface_sandbox_tool_name(
            str(_get_attr(inv, "tool_name") or ""),
            _get_attr(inv, "arguments"),
        )
        # `src_id` is this run's own output; its filename is sandbox-label tier ②.
        produced_file = _sandbox_output_filename(inv, src_id)
        # Feishu 07.06 #11: src_id is a batch subtable artifact, so label/query/preview come from
        # that subtable (query_text/sample_rows), not the batch's top-level query.
        sub = _find_sub_result(inv, src_id)
        if sub is not None:
            q = str(sub.get("query_text") or "").strip() or _extract_query_text(
                _get_attr(inv, "arguments"),
                tool,
                produced_file,
            )
            entry = {
                "label": (_clip_label(f"{_source_tool_label(tool)}: {q}", 120) if (tool and q) else _infer_label_from_inv(inv)),
                "tool": tool,
                "artifact_id": str(src_id),
                "query": q or None,
            }
            preview = _preview_from_sample_rows(
                sub.get("sample_rows"),
                sub.get("total_rows"),
            )
            if preview:
                entry.update(preview)
        else:
            entry = {
                "label": _infer_label_from_inv(inv, produced_file),
                "tool": tool,
                "artifact_id": str(src_id),
                "query": _extract_query_text(_get_attr(inv, "arguments"), tool, produced_file),
            }
    else:
        # Unknown id — synthesize minimal entry so [N] resolves to something
        entry = {
            "label": f"数据源 {src_id[:12]}",
            "tool": "",
            "artifact_id": str(src_id),
            "query": None,
        }
    sources.append(entry)
    n = len(sources)
    id_to_index[src_id] = n
    return n


# ── Follow-up #12: deterministically rewrite bare body artifact IDs → [N] ──
# Writer (LLM) context necessarily includes artifact IDs for data references and can put internal IDs directly in
# prose ("数据汇总来自artifact art_7205168aa21c"; observed in production s_5499c1b85eaa).
# User-facing prose must not contain internal IDs. Deterministically rewrite resolvable IDs to numbered [N] references at completion;
# leave unresolvable IDs unchanged instead of deleting tokens and damaging sentences.
_BARE_ARTIFACT_ID_RE = re.compile(
    r"(?:artifacts?[:：]?\s*)?"  # Consume a preceding "artifact:" prefix
    # Do not use \b: CJK belongs to \w, so "来自art_x" has no \b between 自 and a and would be missed.
    # Lookbehind excludes '/': IDs in URL paths (/api/v1/files/art_x) are links, not references;
    # rewriting would break download links (review F2).
    r"(?<![A-Za-z0-9_/])(art_[0-9a-f]{8,16})(?![0-9A-Za-z_])",
)
# Skip existing markers (including IDs inside [num:…:src:art_x]) and code fences.
_SKIP_SEGMENT_RE = re.compile(
    r"```.*?```"  # fenced code block
    r"|`[^`\n]*`"  # inline code
    r"|\]\([^)\n]*\)"  # markdown link/image target (review F2)
    r"|\[(?:num|ref|chart|figure|cite|data)[^\]]*\]",  # existing markers
    re.S,
)


def resolve_bare_artifact_ids(
    text: str,
    *,
    sources: list[dict[str, Any]],
    id_to_index: dict[str, int],
    tool_invocations: list[Any] | None = None,
) -> tuple[str, list[dict[str, Any]], dict[str, int]]:
    """Rewrite bare `art_xxx` tokens in prose → `[N]` numbered citations.

    与 resolve_in_text 共用 sources/id_to_index (同一编号空间)。规则:
    - 裸 id 紧跟既有 `[N]`(如 "art_x[1]") → 只删 id token(信 writer 的括号);
    - id 可溯源(已在 sources 或 tool_invocations 可 lazy-add) → 换写 `[N]`;
    - 换不到 → 原样保留(不裸删, 免得把句子删残)。
    marker 内部/代码围栏内的 id 不动。
    """
    if not isinstance(text, str) or not text or "art_" not in text:
        return text, sources, id_to_index

    inv_lookup = _build_inv_lookup(tool_invocations or [])

    def _sub_one(m: re.Match[str], tail: str) -> str:
        src_id = m.group(1)
        # Immediately followed by [N]: writer already numbered it; delete only the token
        if re.match(r"\s*\[\d{1,3}\]", tail[:8]):
            return ""
        if src_id in id_to_index or src_id in inv_lookup:
            n = _alloc_source_index(
                src_id,
                sources=sources,
                id_to_index=id_to_index,
                inv_lookup=inv_lookup,
            )
            return f"[{n}]"
        return m.group(0)  # Cannot resolve: preserve unchanged

    out: list[str] = []
    last = 0
    for skip in _SKIP_SEGMENT_RE.finditer(text):
        seg = text[last : skip.start()]
        out.append(_sub_segment(seg, text, last, _sub_one))
        out.append(skip.group(0))
        last = skip.end()
    out.append(_sub_segment(text[last:], text, last, _sub_one))
    return "".join(out), sources, id_to_index


def _sub_segment(
    seg: str,
    full_text: str,
    seg_offset: int,
    sub_one: Any,
) -> str:
    """对一个非跳过区段做裸 id 替换; tail 取全文视角(跨段看紧跟的 [N])。"""
    if "art_" not in seg:
        return seg
    parts: list[str] = []
    last = 0
    for m in _BARE_ARTIFACT_ID_RE.finditer(seg):
        parts.append(seg[last : m.start()])
        tail = full_text[seg_offset + m.end() : seg_offset + m.end() + 8]
        parts.append(sub_one(m, tail))
        last = m.end()
    parts.append(seg[last:])
    return "".join(parts)


# ── Follow-up #14: fill unreadable source-entry labels with artifact filenames ──
async def enrich_source_labels(
    entries: list[dict[str, Any]],
    artifact_store: Any,
    *,
    user_id: str | None = None,
) -> list[dict[str, Any]]:
    """兜底标签(裸 artifact id 形)的来源条目, 用 artifact 的 filename 补成
    人话 (prod s_5499c1b85eaa: sources 段 13/13 条全是裸 id 墙)。

    - "需要补"的判定 = label 为空 **或 label 里出现该 artifact 的裸 id**
      (覆盖 `artifact: art_x` / `数据源 art_…`[截断] / `bi_query: art_x`
      全部兜底形状 — review F4; 真人话 query 不含 hex id, 不误伤)。
    - **owner 校验 (review F1, #54 事故契约)**: LLM 可能把别 user/session 的
      artifact id 混进 sources — 取 filename 必带 check_created_by, 拿不到
      (跨租户/不存在) 就保持原样, 绝不泄露他人文件名。
    best-effort: store 取不到就保持原样, 绝不因 enrich 失败丢条目。"""
    if artifact_store is None or not entries:
        return entries
    for e in entries:
        if not isinstance(e, dict):
            continue
        aid = e.get("artifact_id")
        if not (isinstance(aid, str) and aid.startswith("art_")):
            continue
        label = str(e.get("label") or "").strip()
        if label and aid not in label and aid[:12] not in label:
            continue
        try:
            if user_id is not None:
                art = await artifact_store.get(aid, check_created_by=user_id)
            else:
                art = await artifact_store.get(aid)
        except Exception:  # noqa: S112
            continue
        fn = str(getattr(art, "filename", "") or "") if art else ""
        if fn:
            e["label"] = fn
    return entries


def _build_inv_lookup(invocations: list[Any]) -> dict[str, Any]:
    """Map call_id + artifact_id + chunk_id → invocation.

    飞书07.06 #11: 批量结果 `{results:[{artifact_id,…},…]}` 的**每张**子表
    artifact 都要能反查到 invocation — value map 根修后 `:src:` 是子表
    artifact id, 只映射 results[0] 会让 lazy-add 查不到 → 合成兜底条目。
    """
    out: dict[str, Any] = {}
    for inv in invocations:
        call_id = _get_attr(inv, "call_id") or _get_attr(inv, "id")
        if call_id:
            out.setdefault(str(call_id), inv)
        result = _get_attr(inv, "result") or {}
        aid = _extract_artifact_id(result)
        if aid:
            out.setdefault(str(aid), inv)
        if isinstance(result, dict):
            res_list = result.get("results")
            if isinstance(res_list, list):
                for r in res_list:
                    if isinstance(r, dict):
                        sub_aid = r.get("artifact_id") or r.get("_ref_artifact_id")
                        if sub_aid:
                            out.setdefault(str(sub_aid), inv)
            # Review F2: sandbox output IDs live in `result_json.outputs[]`, neither top-level
            # artifact_id nor results[]. Without indexing, stdout output used as value-map
            # `:src:` cannot resolve back to the invocation, yielding the Unknown id fallback and degrading the source to
            # a bare data-source art_xxx label + empty tool, defeating display-name normalization entirely.
            inner = result.get("result_json")
            outputs = inner.get("outputs") if isinstance(inner, dict) else None
            if isinstance(outputs, list):
                for o in outputs:
                    if isinstance(o, dict) and o.get("artifact_id"):
                        out.setdefault(str(o["artifact_id"]), inv)
        for cid in _extract_chunks(result):
            out.setdefault(str(cid), inv)
    return out


def _find_sub_result(inv: Any, src_id: str) -> dict[str, Any] | None:
    """定位批量结果里 artifact_id == src_id 的那个子结果 (results[] entry)。

    飞书07.06 #11: lazy-add 一个子表 artifact 时, label/query/preview 要来自
    **那张子表** (query_text/sample_rows), 而不是整批 invocation 的顶层 query —
    否则 [N] popover 的标签和预览是批次级的, 用户分不清是哪张表。
    """
    result = _get_attr(inv, "result") or {}
    if not isinstance(result, dict):
        return None
    res_list = result.get("results")
    if not isinstance(res_list, list):
        return None
    for r in res_list:
        if isinstance(r, dict) and (r.get("artifact_id") == src_id or r.get("_ref_artifact_id") == src_id):
            return r
    return None


def source_label_language() -> str:
    """Language for generated source labels: the turn's answer language, resolved exactly like the
    report chrome (`chrome_lang`), so the tool prefix and the run-ordinal suffix of one label always
    agree. Labels already persisted keep the language they were written in."""
    from types import SimpleNamespace

    from bowhead_core.agents._lead_context import get_current_output_language
    from bowhead_core.report_chrome_text import chrome_lang

    return chrome_lang(SimpleNamespace(output_language=get_current_output_language()))


_TOOL_DISPLAY_KEY_BY_LANG = {"zh-CN": "display_zh", "en-US": "display_en", "de-DE": "display_de", "fr-FR": "display_fr"}


def _source_tool_label(tool: Any, lang: str | None = None) -> str:
    """How a source citation names its tool: the **human name**, not the tool code.

    The sources section is the report's credibility surface; customers reading `python_sandbox` /
    `bi_query` there treat it as noise (116 dogfood 2026-09-09). Names come from the generated
    tool_aliases.yaml table shared with step cards, chips and the tool picker. The language is the
    answer language (`source_label_language`), the same as the run-ordinal suffix; it used to follow
    the interface language, which mixed "Run Code" with "（第 1 次运行）" in one label.

    Unknown tools (team MCP, retired names) are returned **unchanged** rather than pretending to know them.
    """
    name = str(tool or "").strip()
    if not name:
        return name
    try:
        from bowhead_core.agents._generated.tool_display import TOOL_DISPLAY

        entry = TOOL_DISPLAY.get(name)
        if entry is None:
            return name
        key = _TOOL_DISPLAY_KEY_BY_LANG.get(lang or source_label_language(), "display_en")
        return str(entry.get(key) or entry["display_en"])
    except Exception:
        return name


# Distinguishing suffixes generated by this function (one form per language); strip for idempotency and renumbering on reruns.
# Dangling opening brackets after truncation (including fullwidth/Chinese title brackets); see `_clip_label`.
_TRAILING_OPEN_BRACKET_RE = re.compile(r"[\s(（\[【{｛「『<《〈]+$")
_RUN_ORDINAL_SUFFIX_RE = re.compile(r"(?:（第 \d+ 次运行）| \((?:run|Durchlauf|exécution) \d+\))$")


def number_duplicate_source_labels(entries: Any, *, lang: str = "en-US") -> None:
    """同一份报告里字面相同的沙箱来源标签 → 逐条补「第 N 次运行」。**就地改 label。**

    为什么在标签已经尽力之后还需要这一层（116 十四天语料，172 份含 ≥2 条沙箱来源
    的报告）：标签三档取完仍有 **23%** 的报告存在字面重复——两次运行既没写意图注释、
    也没写出文件（只 print 到 stdout），或者两次都写成同一个 `out.csv`。这时**不存在**
    任何能区分它们的真材料，所以只标次序：读者至少知道这是两次独立运行，而不是
    渲重了。补完 0%。

    ⚠ 编号的前提是「这两条确实是两个来源」，不是「标签撞了」：
    - **沙箱**：一律编号。同一段脚本跑两次就是两次独立运行，本来就该分开。
    - **其余工具**（bi_query / kb_search…）：只在组内每条都有 artifact_id 且**互不相同**时编号。
      `_add_entry` 是按 **artifact_id** 去重的，(tool, query) 那条桥**只**用来把
      「自报的无 aid 条目」并进真条目（见 `_add_entry` 里 `ex.get("artifact_id") is None` 那支）；
      两个不同 artifact 的同 query 结果不会被折叠，于是留下两行字面一样的标签。
      ★ 本函数原先写着「bi_query 同名条目早已被 (tool, query) 折成一条」——**那句不成立**，
      116 语料实测（09-12 起 10 份报告）唯一残留的重名正是这个形态：同 tool 同 query、
      artifact 分别是 `art_68bb01347847` / `art_952147c2f4a0`。
      反过来，若组内 aid 有缺失或有重复，说明本该折叠而没折，**不编号**——编号会把
      「该合的没合」伪装成「两次不同的取数」，那比重名更难查。

    幂等 + 可重排：先剥掉本函数自己的后缀再重编。`upsert` 在一轮里会被反复调用
    （patch 语义、prior 条目 seed 回来），不剥就会叠成「（第 1 次运行）（第 1 次运行）」，
    而且后到的第三条会光着身子站在两条编过号的旁边。编号取列表位置序，靠 INV-2
    （已存在 entry 的位置永不变）保证同一条来源的号不会在轮内跳。
    """
    if not isinstance(entries, list):
        return
    from bowhead_core.report_chrome_text import chrome_text

    idx_by_base: dict[str, list[int]] = {}
    sandbox_at: dict[int, bool] = {}
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            continue
        base = _RUN_ORDINAL_SUFFIX_RE.sub("", str(entry.get("label") or ""))
        entry["label"] = base
        if base:
            idx_by_base.setdefault(base, []).append(i)
            sandbox_at[i] = _is_sandbox_tool(str(entry.get("tool") or ""), None)
    for base, idxs in idx_by_base.items():
        if len(idxs) < 2:
            continue
        if not all(sandbox_at.get(i) for i in idxs):
            # Non-sandbox groups: distinct sources require an aid on every entry and all aids distinct (see docstring).
            aids = [entries[i].get("artifact_id") for i in idxs]
            if any(a is None for a in aids) or len(set(map(str, aids))) != len(aids):
                continue
        for n, i in enumerate(idxs, 1):
            entries[i]["label"] = base + chrome_text("sources_run_ordinal", lang, n=n)


def _clip_label(text: Any, limit: int) -> str:
    """按长度截断标签文本，并剥掉截断后**悬在尾巴上的左括号**。

    116 2026-09-20 实拍：沙箱意图注释被 `[:40]` 正好切在一个全角 `（` 上，来源行渲成
    `运行代码: …增长/萎缩分类（`；而同名来源补序号时后缀本身又以 `（` 开头，两者并排成
    `（（第 1 次运行）`。半截括号单独出现就已经是坏的，补序号只是把它放大。

    ⚠ 只剥**左**括号和空白，不动逗号顿号之类——那些截断后虽不好看但不构成坏结构，
    扩大剥除范围只会增加误伤面（本仓对「用词形做判定」一贯保守）。
    全被剥空时退回未剥版本：宁可难看，不要把标签变成只剩工具名。
    """
    s = str(text or "")[:limit]
    trimmed = _TRAILING_OPEN_BRACKET_RE.sub("", s)
    return trimmed or s


def _infer_label_from_inv(inv: Any, produced_file: str | None = None) -> str:
    raw_tool = _get_attr(inv, "tool_name") or _get_attr(inv, "tool") or "数据"
    args = _get_attr(inv, "arguments") or {}
    q = _extract_query_text(args, raw_tool, produced_file)
    # First normalize the flat entrance to the pseudo tool name (python_exec → python_sandbox), then translate to a human-readable name.
    # ⚠ Previously only the first step ran, so users saw `python_sandbox: df = ...`: replacing one code name with another,
    # not a human-readable name (116 dogfood screenshot on 2026-09-09).
    tool_name = _source_tool_label(surface_sandbox_tool_name(str(raw_tool), args))
    if q:
        return f"{tool_name}: {_clip_label(q, 40)}"
    return str(tool_name)


def resolve_payload_inline_refs(
    payload: dict[str, Any],
    tool_invocations: list[Any] | None,
) -> dict[str, Any]:
    """Walk payload text fields, rewrite all inline markers to `[N]`, and
    populate `payload.sources[]` as the canonical index.

    Mutates payload in place; also returns it for chainability.

    Text fields walked:
      - exec_summary
      - insights[i].thesis / narrative
      - recommendations[i].action / impact / detail
      - caveats[i]
    """
    if not isinstance(payload, dict):
        return payload

    sources, id_to_index = build_sources_list(
        payload.get("sources"),
        tool_invocations,
    )

    def _walk_str(s: Any) -> Any:
        if not isinstance(s, str):
            return s
        new_text, _, _ = resolve_in_text(
            s,
            sources=sources,
            id_to_index=id_to_index,
            tool_invocations=tool_invocations,
        )
        return new_text

    # exec_summary
    if "exec_summary" in payload:
        payload["exec_summary"] = _walk_str(payload.get("exec_summary"))

    # insights
    for ins in payload.get("insights") or []:
        if not isinstance(ins, dict):
            continue
        if "thesis" in ins:
            ins["thesis"] = _walk_str(ins.get("thesis"))
        if "narrative" in ins:
            ins["narrative"] = _walk_str(ins.get("narrative"))

    # recommendations
    # Sprint chat-stream-fix D-1 (2026-05-16): walker was using legacy field
    # names `action/detail/impact.note` but canvas_composer.py:1356-1364 emits
    # recommendations with `action/rationale/expected_impact/owner/timeline`.
    # Result: rationale + expected_impact strings containing
    # `[num:V:U:src:tl_xxx]` markers were NEVER rewritten to `[N]`. Prod
    # evidence: sec_3f03a4cd86d3 still has raw `[num:7.98亿:src:tl_*]`.
    for rec in payload.get("recommendations") or []:
        if not isinstance(rec, dict):
            continue
        # New canonical fields (canvas_composer composer-pipeline)
        for k in (
            "action",
            "rationale",
            "expected_impact",
            # Legacy fields kept for back-compat with older payloads
            "detail",
            "owner",
            "timeline",
        ):
            if k in rec:
                rec[k] = _walk_str(rec.get(k))
        # Very-old `impact.note` nested form
        impact = rec.get("impact")
        if isinstance(impact, dict) and "note" in impact:
            impact["note"] = _walk_str(impact.get("note"))

    # caveats
    if isinstance(payload.get("caveats"), list):
        payload["caveats"] = [_walk_str(c) for c in payload["caveats"]]

    # Re-attach updated sources to payload
    payload["sources"] = sources
    return payload


# ─────────────────────────────────────────────────────────────
# Inline-data preview for citation popovers (citation-hover original text)
# ─────────────────────────────────────────────────────────────
# A bi_query source's data lives in the tool result's ``sample_rows`` (a small,
# already-capped ``list[dict]`` preview). Its ``artifact_id`` is a datarc
# query-ledger ref (``tl_…``) that is NOT a fetchable bowhead artifact — it 404s
# on ``/api/v1/files/{id}`` and ``…/preview``. So the ONLY way a citation hover
# popover can show the original rows *without* a dead "download" link is to carry
# this tiny preview into the Sources entry itself. Kept minimal (≤5 rows, ≤6
# cols, short cells) — the hover shows a glimpse; the full dataset stays in the
# workspace Data tab.
#
# NB — distinct from ``_build_value_to_source_map``'s ι-15 lesson (L141): that
# was about adding sample_rows to the number→source BINDING map, which blew up
# cross-inv value collisions → ambiguous-drop. This is per-entry DISPLAY data
# keyed by artifact_id, with no cross-inv value matching, so no such risk.
_PREVIEW_MAX_ROWS = 5
_PREVIEW_MAX_COLS = 6
_PREVIEW_CELL_CHARS = 80


def _cell_str(v: Any) -> str:
    """Stringify a preview cell, bounding length so one fat cell can't bloat the
    persisted Sources entry / the hover popover."""
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    s = str(v)
    return s if len(s) <= _PREVIEW_CELL_CHARS else s[:_PREVIEW_CELL_CHARS] + "…"


def _preview_from_sample_rows(
    sample_rows: Any,
    total_rows: Any = None,
) -> dict[str, Any] | None:
    """Build a compact ``{preview_columns, preview_rows, total_rows}`` block from
    a bi_query sub-result's ``sample_rows`` (``list[dict]``). Returns ``None``
    when there is nothing usable, so callers add no preview keys and the entry
    stays lean."""
    if not isinstance(sample_rows, list) or not sample_rows:
        return None
    rows_in = [r for r in sample_rows if isinstance(r, dict)][:_PREVIEW_MAX_ROWS]
    if not rows_in:
        return None
    # columns = first-row key order, union-extended by later rows, capped.
    cols: list[str] = []
    for r in rows_in:
        for k in r:
            ks = str(k)
            if ks not in cols:
                cols.append(ks)
        if len(cols) >= _PREVIEW_MAX_COLS:
            break
    cols = cols[:_PREVIEW_MAX_COLS]
    out_rows = [{c: _cell_str(r.get(c)) for c in cols} for r in rows_in]
    tr = total_rows if isinstance(total_rows, int) and not isinstance(total_rows, bool) else None
    if tr is None:
        tr = len(sample_rows)
    return {"preview_columns": cols, "preview_rows": out_rows, "total_rows": tr}


# ─────────────────────────────────────────────────────────────
# report-engine Phase-2 §13.1 — turn-global Sources registry (multi-writer P0①)
# ─────────────────────────────────────────────────────────────
class TurnSourcesRegistry:
    """Turn-global, content-addressed Sources accumulator shared by every writer/
    peer composing ONE report — the canvas-Sources analogue of the chat path's
    ``TurnCitationRegistry`` (citations.py).

    Why this exists (§13.1.2): the legacy path rebuilds ``build_sources_list``
    **per payload**, so with multiple writers each emitting ``source_refs`` the
    same source gets a different ``[N]`` in each section (or duplicate Sources
    rows). One shared accumulator gives a single numbering space: dedup by
    content id (artifact_id / chunk_id / call_id), render-time numbering = the
    order entries were first registered. Specialists therefore only ever emit
    content-addressed ``[ref:<id>]`` / ``[num:…:src:<id>]`` markers and NEVER a
    literal ``[N]`` (§13.1.1) — this registry owns the integers.

    Pure + in-memory; persistence/rendering is the caller's job. Mutation is sync
    (no await) so it is safe across sibling-peer coroutines in asyncio, exactly
    like ``TurnCitationRegistry``.
    """

    def __init__(self) -> None:
        # ordered Sources entries ({label, tool, artifact_id, query}); index+1 = N
        self._sources: list[dict[str, Any]] = []
        # content id → 1-based N (dedup key); mirrors build_sources_list.id_to_index
        self._id_to_index: dict[str, int] = {}

    @property
    def sources(self) -> list[dict[str, Any]]:
        """The accumulated Sources list (render-order). The Sources section entries."""
        return self._sources

    @property
    def id_to_index(self) -> dict[str, int]:
        return self._id_to_index

    def is_empty(self) -> bool:
        return not self._sources

    def size(self) -> int:
        return len(self._sources)

    def _add_entry(self, entry: dict[str, Any]) -> int:
        """Fold one Sources entry into the accumulator, deduping by artifact_id.
        Returns the 1-based N (existing if already present — first-write wins).

        On a dedup hit we still graft any inline-data preview the incoming entry
        carries onto the existing row: ``ingest_payload_sources`` may register a
        bi_query ``artifact_id`` first (no rows), then ``ingest_tool_invocations``
        sees the same id WITH ``sample_rows`` — the popover data must not be lost
        to first-write-wins."""
        aid = entry.get("artifact_id")

        def _tq(e: dict[str, Any]) -> tuple[str, str] | None:
            # Normalize (tool, query) keys; peer payloads and actual tool results may differ in whitespace
            t = str(e.get("tool") or "").strip()
            q = "".join(str(e.get("query") or "").split())
            return (t, q) if (t and q) else None

        if aid is not None:
            existing_n = self._id_to_index.get(str(aid))
            if existing_n is not None:
                existing = self._sources[existing_n - 1]
                for k in ("preview_columns", "preview_rows", "total_rows"):
                    if entry.get(k) is not None and existing.get(k) is None:
                        existing[k] = entry[k]
                return existing_n
            # ★116 dogfood 1.0.93 P3 (t_c8959fde27ca): peer-reported payload sources
            # (label present, artifact_id absent) and actual tool results for the same query are the same source,
            # but aid lookup misses aidless entries, producing duplicate pairs in Sources; the aidless
            # row renders "Source is currently unavailable". Bridge by (tool, query):
            # upgrade the aidless entry in place (unchanged number; [N] still resolves to the same row).
            k_in = _tq(entry)
            if k_in is not None:
                for i, ex in enumerate(self._sources):
                    if ex.get("artifact_id") is None and _tq(ex) == k_in:
                        ex["artifact_id"] = aid
                        for k in ("preview_columns", "preview_rows", "total_rows"):
                            if entry.get(k) is not None and ex.get(k) is None:
                                ex[k] = entry[k]
                        self._id_to_index[str(aid)] = i + 1
                        return i + 1
        else:
            # review[2]-fix: deduplicate label-only entries (no artifact_id, supported by build_sources_list
            # Pass-1) by label; otherwise multiple writers sharing a registry repeat the same label-only source
            # in Sources once per ingest.
            _lbl = str(entry.get("label") or "").strip()
            if _lbl:
                for i, ex in enumerate(self._sources):
                    if ex.get("artifact_id") is None and str(ex.get("label") or "").strip() == _lbl:
                        return i + 1
            # Existing entry with the same (tool, query), with or without aid: reuse its number, do not append
            k_in = _tq(entry)
            if k_in is not None:
                for i, ex in enumerate(self._sources):
                    if _tq(ex) == k_in:
                        return i + 1
        self._sources.append(entry)
        n = len(self._sources)
        if aid is not None:
            self._id_to_index[str(aid)] = n
        return n

    def seed(self, entries: Any) -> None:
        """按既有顺序**原样**装入已落库的 entries（不去重）。

        为什么不复用 `_add_entry`：已落库的编号是历史文本里的**死数字**
        （`resolve_in_text` 把标记铸成整数写进 markdown），装入时哪怕折掉一行，
        它后面所有条目的 `[N]` 就全体错位。去重只对**新来的** entry 生效——
        这条让 INV-2（已存在 entry 的位置与编号永不变）由**构造**保证，
        而不是靠事后断言。只在空注册表上调用。
        """
        for entry in entries or []:
            if not isinstance(entry, dict):
                continue
            self._sources.append(dict(entry))
            aid = entry.get("artifact_id")
            if aid is not None:
                self._id_to_index.setdefault(str(aid), len(self._sources))

    def register_source(
        self,
        content_id: str,
        *,
        label: str | None = None,
        tool: str = "",
        query: str | None = None,
        sample_rows: Any = None,
        total_rows: Any = None,
    ) -> int:
        """Register one content-addressed source (e.g. a section's ``source_ref``);
        returns its 1-based N. Deduped — the field the WP-B handler stores is no
        longer inert, it flows here. Label synthesised when absent so ``[N]``
        always resolves to a non-empty Sources row (mirrors build_sources_list).

        ``sample_rows``/``total_rows`` (from a bi_query sub-result) are folded
        into a compact ``preview_*`` block so the citation hover popover can show
        the 原文 rows inline instead of a dead ``tl_`` download link."""
        if not label:
            _tl = _source_tool_label(tool)
            label = _clip_label(f"{_tl}: {query}", 120) if (tool and query) else (f"{_tl}: {content_id}" if tool else f"artifact: {content_id}")
        entry: dict[str, Any] = {
            "label": label,
            "tool": tool,
            "artifact_id": content_id,
            "query": query,
        }
        preview = _preview_from_sample_rows(sample_rows, total_rows)
        if preview:
            entry.update(preview)
        return self._add_entry(entry)

    def ingest_tool_invocations(self, tool_invocations: list[Any] | None) -> None:
        """Eagerly register each data artifact produced this turn (bi_query /
        bi_fetch_rows / kb_search results carry an ``artifact_id``) as a Sources
        entry. ``build_sources_list`` only materialises these *lazily* on inline
        ``[num:…:src:]`` marker reference — but the writer's narrative cites no
        such markers, so without this the '数据来源' section comes out empty even
        though the report was built on real BI queries.

        Batch-aware: a bi_query/bi_fetch_rows result is often a batch
        ``{results: [{artifact_id, query_text, …}]}`` (the shape the kpi builder
        reads). Each sub-result is its OWN data source with its own human query
        text — so register them per-sub-result, giving readable Sources labels
        (the query) instead of a bare ``artifact: <hex>`` fallback."""
        for inv in tool_invocations or []:
            # Review F4: normalize display names on both registration paths: lazy (`_alloc_source_index`)
            # and eager here. Fixing only one repeats this repository's one-concept/many-implementations problem:
            # sandbox calls on the eager path still write `python_exec` to `sources[].tool`.
            _raw_tool = str(_get_attr(inv, "tool_name") or _get_attr(inv, "tool") or "")
            _inv_args = _get_attr(inv, "arguments")
            tool = surface_sandbox_tool_name(_raw_tool, _inv_args)
            result = _get_attr(inv, "result") or {}
            top_query = _extract_query_text(_inv_args, _raw_tool)
            registered = False
            res_list = result.get("results") if isinstance(result, dict) else None
            if isinstance(res_list, list):
                for r in res_list[:10]:  # cap noisy result lists
                    if not isinstance(r, dict):
                        continue
                    aid = r.get("artifact_id") or r.get("_ref_artifact_id")
                    if aid:
                        q = str(r.get("query_text") or "").strip() or top_query
                        self.register_source(
                            str(aid),
                            tool=tool or "bi",
                            query=q,
                            sample_rows=r.get("sample_rows"),
                            total_rows=r.get("total_rows"),
                        )
                        registered = True
                    elif r.get("url"):
                        # web_search result {title, url, snippet} — register the
                        # page as a source (content id = url, label = title) so a
                        # market/web report cites its real sources too.
                        self.register_source(
                            str(r["url"]),
                            label=str(r.get("title") or r["url"]),
                            tool=tool or "web_search",
                            query=str(r.get("title") or "")[:120] or None,
                        )
                        registered = True
            if not registered and _raw_tool.startswith("web_") and isinstance(result, dict):
                # web_extract / web_crawl return pages[], web_deep_research returns sources[]; each entry is one webpage source,
                # registered by url like web_search results[] (only web_* tools; other tools' sources keys have different semantics).
                for key in ("pages", "sources"):
                    items = result.get(key)
                    if not isinstance(items, list):
                        continue
                    for item in items[:10]:
                        if isinstance(item, dict) and item.get("url"):
                            self.register_source(
                                str(item["url"]),
                                label=str(item.get("title") or item["url"]),
                                tool=tool or _raw_tool,
                                query=str(item.get("title") or "")[:120] or None,
                            )
                            registered = True
            if not registered:
                aid = _extract_artifact_id(result)
                if aid:
                    res_dict = result if isinstance(result, dict) else {}
                    # Sandbox: aid is this run's own output; use its filename instead of the first code line
                    # (`top_query` was computed before aid was available, so it lacks this tier and must be recomputed).
                    _produced = _sandbox_output_filename(inv, aid)
                    _query = _extract_query_text(_inv_args, _raw_tool, _produced) if _produced else top_query
                    self.register_source(
                        str(aid),
                        tool=tool,
                        query=_query,
                        sample_rows=res_dict.get("sample_rows"),
                        total_rows=res_dict.get("total_rows"),
                    )

    def ingest_payload_sources(
        self,
        payload_sources: list[dict[str, Any]] | None,
        tool_invocations: list[Any] | None = None,
    ) -> None:
        """Merge a payload's ``sources[]`` (+ tool_invocations) into the shared
        accumulator. Reuses ``build_sources_list`` (same Pass-1/Pass-2 logic),
        then folds each entry in with cross-payload dedup — the fix for
        ``lead_orchestrator.py:3705`` building per-peer with no cross-peer dedup."""
        merged, _ = build_sources_list(payload_sources, tool_invocations)
        for entry in merged:
            self._add_entry(entry)

    def resolve_text(
        self,
        text: str,
        tool_invocations: list[Any] | None = None,
    ) -> str:
        """Rewrite ``[num:…:src:S]`` / ``[ref:S]`` markers in ``text`` → ``[N]``
        against the SHARED maps (so the same source is the same N in every
        writer's section). ``resolve_in_text`` mutate-extends the maps in place
        for ids first seen here (lazy add from tool_invocations)."""
        new_text, self._sources, self._id_to_index = resolve_in_text(
            text,
            sources=self._sources,
            id_to_index=self._id_to_index,
            tool_invocations=tool_invocations,
        )
        return new_text

    def resolve_bare_ids(
        self,
        text: str,
        tool_invocations: list[Any] | None = None,
    ) -> str:
        """追#12: 正文裸 `art_xxx` → `[N]`(共享编号空间, 同 resolve_text)。"""
        new_text, self._sources, self._id_to_index = resolve_bare_artifact_ids(
            text,
            sources=self._sources,
            id_to_index=self._id_to_index,
            tool_invocations=tool_invocations,
        )
        return new_text


# Per-canvas serial lock: upsert's list→append spans awaits; two peers fanned out by Lead can both
# miss on same-turn canvas_add_insight and append twice (impl-review A-P1-3/B-P1-1 reproduced two sections
# with the real store). asyncio.Lock suffices for the in-process single-writer topology.
_UPSERT_LOCKS: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)


async def upsert_turn_sources_section(
    *,
    canvas_store: Any,
    bus: Any,
    canvas_id: str,
    turn_id: str | None,
    entries: list[dict[str, Any]],
    lang: str = "en-US",
) -> str | None:
    """citation-audit M1 总则2：**一份 turn 一份 sources 段**。

    本 turn 已有（未 superseded 的）kind=sources 段 → patch entries（emit
    `canvas.section_patched`，契约一 update 形状：patch=完整 section）；否则
    append（emit `canvas.section_added`）。thin-path（peer 阶段）与
    writer-loop / composer（compose 阶段）共用本原语——同 turn 双 sources 段
    = [N] 解析串号面（popover 取组内第一个）。

    并发安全：per-canvas asyncio 锁串行化 list→写 全程；CAS 冲突（并发 rev
    bump）重读重试一次（有界）。Best-effort：仍失败返回 None，绝不打断写报告。
    """
    if not entries:
        return None
    # Only here is the list complete (thin-path and composer both reach this primitive), making this the sole place
    # that can detect label collisions. Modify labels in place, without new fields: downstream
    # `_build_sections_from_payload` whitelists fields and would drop new ones.
    number_duplicate_source_labels(entries, lang=lang)
    async with _UPSERT_LOCKS[canvas_id]:
        return await _upsert_turn_sources_locked(
            canvas_store=canvas_store,
            bus=bus,
            canvas_id=canvas_id,
            turn_id=turn_id,
            entries=entries,
            lang=lang,
        )


def fold_source_entries(prior: Any, incoming: Any) -> list[dict[str, Any]]:
    """把 incoming 折进 prior，复用注册表**同一套**去重规则（不另写一份）。

    prior 逐条 `seed`（保位保号，INV-2 由构造保证），incoming 逐条走 `_add_entry`
    （命中去重就复用旧编号、并把 preview 嫁接到旧行；没命中才追加）。
    """
    reg = TurnSourcesRegistry()
    reg.seed(prior)
    for entry in incoming or []:
        if isinstance(entry, dict):
            reg._add_entry(dict(entry))
    return reg.sources


def _incoming_looks_seeded(prior: Any, incoming: Any) -> bool:
    """incoming 是不是「在 prior 之上继续编号」的（即调用方的注册表被回灌过）。

    折叠保住了**落库列表**的完整，但保不住**已经铸进正文的数字**：调用方若拿一张空表
    起铸，它的 `[1]` 指的是自己的第一条，折叠后那个位置却是历史的第一条 ⇒ 静默错指。
    这条只用来打日志——文本已经写完，救不回来；但它把「回灌失效」从不可见变成可查。
    """
    prior_list = [e for e in (prior or []) if isinstance(e, dict)]
    inc_list = [e for e in (incoming or []) if isinstance(e, dict)]
    if not prior_list or not inc_list:
        return True
    if len(inc_list) < len(prior_list):
        return False

    def _key(e: dict[str, Any]) -> str:
        return str(e.get("artifact_id") or e.get("label") or "")

    return [_key(e) for e in inc_list[: len(prior_list)]] == [_key(e) for e in prior_list]


async def _upsert_turn_sources_locked(
    *,
    canvas_store: Any,
    bus: Any,
    canvas_id: str,
    turn_id: str | None,
    entries: list[dict[str, Any]],
    lang: str = "en-US",
) -> str | None:
    from bowhead_core.canvas_store import CanvasRevConflictError

    try:
        sections = await canvas_store.list_sections(canvas_id)
        existing, canvas_scope = find_canvas_sources_section(sections, turn_id=turn_id)
        if existing is not None:
            # ★ Merge semantics belong at persistence, not at callers (tech-design §2.2):
            # `ensure_turn_sources_registry` has 6 call sites, only 3 rehydrate,
            # and the eager card path (`canvas_composer.py` Phase-F) is production's default.
            # Replacing entries wholesale with caller input would let that path replace canvas-wide Sources
            # with a current-turn-only list, misaligning all historical `[N]`. Folding lets any caller
            # safely pass subsets (also covering unrehydrated registries after resume changes generations).
            # Turn scope (flag off / existing sections already forked) retains full replacement: today's byte-for-byte behavior.
            entries_to_write = entries
            if canvas_scope:
                _prior = _section_entries(existing)
                if not _incoming_looks_seeded(_prior, entries):
                    # Caller registry was not rehydrated, so its `[N]` was numbered against its own empty table,
                    # pointing to historical entries after folding. Completed text cannot be repaired, but this must be observable
                    # (review #1: `get_turn_sources_registry` silently falls back to initialization only
                    # when canvas_store is unavailable).
                    logger.warning(
                        "sources_resolver.unseeded_registry canvas=%s turn=%s prior=%d incoming=%d",
                        canvas_id,
                        turn_id,
                        len([e for e in (_prior or []) if isinstance(e, dict)]),
                        len(entries),
                    )
                entries_to_write = fold_source_entries(_prior, entries)
            try:
                stored = await canvas_store.patch_section(
                    section_id=existing.id,
                    patch={"entries": entries_to_write},
                    expected_rev=getattr(existing, "rev", None),
                    by_turn_id=turn_id,
                )
            except CanvasRevConflictError:
                # One bounded retry: reread current rev and patch (impl-review B-P2-3:
                # the original swallowed conflicts and returned None, leaving thin-only turns without a next writer to recover).
                refreshed = next(
                    (s for s in await canvas_store.list_sections(canvas_id) if s.id == existing.id),
                    None,
                )
                if refreshed is None:
                    return None
                # The conflicting writer may have appended entries; refold using refreshed entries,
                # or writing the original entries loses that append (INV-2).
                retry_entries = fold_source_entries(_section_entries(refreshed), entries) if canvas_scope else entries
                stored = await canvas_store.patch_section(
                    section_id=existing.id,
                    patch={"entries": retry_entries},
                    expected_rev=getattr(refreshed, "rev", None),
                    by_turn_id=turn_id,
                )
            if stored is None:
                return None
            event_type = "canvas.section_patched"
            event_payload: dict[str, Any] = {
                "canvas_id": canvas_id,
                "section_id": stored.id,
                "kind": "sources",
                "patch": stored.to_report_section(),
                "rev": getattr(stored, "rev", None),
                "seq": getattr(stored, "seq", None),  # Batch B1-6: IM insertion position (FE ignores)
            }
        else:
            # chrome-i18n review P1-1: this is the repository's sole persisted Sources-section creation point.
            # The label localized upstream in _build_sections_from_payload never reaches this entries-only route;
            # localize the label at the gate or English reports always persist Chinese headers.
            from bowhead_core.report_chrome_text import chrome_text as _chrome_text

            stored = await canvas_store.append_section(
                canvas_id=canvas_id,
                kind="sources",
                payload={"label": _chrome_text("sources_label", lang), "entries": entries},
                by_turn_id=turn_id,
            )
            event_type = "canvas.section_added"
            event_payload = {
                "canvas_id": canvas_id,
                "section": stored.to_report_section(),
                "seq": getattr(stored, "seq", None),
            }
        if bus is not None:
            from bowhead_core.events import OrchestratorEvent

            await bus.emit(
                OrchestratorEvent(
                    event_type=event_type,  # type: ignore[arg-type]
                    turn_id=turn_id or "",
                    payload=event_payload,
                )
            )
        return str(stored.id)
    except Exception:
        logger.exception(
            "sources_resolver.upsert_turn_sources_failed canvas=%s turn=%s",
            canvas_id,
            turn_id,
        )
        return None


async def rehydrate_turn_registry(
    reg: TurnSourcesRegistry,
    *,
    canvas_store: Any,
    canvas_id: str,
    turn_id: str | None,
) -> bool:
    """clarification resume 代际修复（impl-review B-P2-7）：pause→resume 会换
    TurnEventBus 代际，挂在旧 bus 上的 registry 丢失、新 registry 从 [1] 重新
    编号，而 upsert 按 turn 匹配会 patch 掉 pause 前的 sources 段 → pause 前
    narrative 的 [N] 静默串号。修法：registry 为空且本 turn 已有 sources 段时，
    按原顺序回灌 entries——编号跨代际连续。Best-effort。"""
    if not reg.is_empty():
        return True
    try:
        existing, _canvas_scope = find_canvas_sources_section(
            await canvas_store.list_sections(canvas_id),
            turn_id=turn_id,
        )
        if existing is None:
            return True  # No Sources section on the canvas yet; nothing to rehydrate, not a failure
        entries = (existing.payload or {}).get("entries")
        if not isinstance(entries, list):
            return True
        # Use `seed`, not `_add_entry`: persisted numbers are fixed integers in historical text;
        # dropping one row during rehydration misaligns every subsequent `[N]` (see `TurnSourcesRegistry.seed`).
        reg.seed(entries)
        return True
    except Exception:
        logger.warning(
            "sources_resolver.rehydrate_failed canvas=%s turn=%s",
            canvas_id,
            turn_id,
            exc_info=True,
        )
        return False


def ensure_turn_sources_registry(parent_ctx: object) -> TurnSourcesRegistry:
    """Lazy-init the turn-global Sources registry on
    ``parent_ctx.turn_sources_registry`` (mirrors ``citations.ensure_registry``).
    Tolerant of frozen ctxes — best-effort setattr, else a transient registry
    that still works for the current caller (just not shared)."""
    existing = getattr(parent_ctx, "turn_sources_registry", None)
    if isinstance(existing, TurnSourcesRegistry):
        return existing
    registry = TurnSourcesRegistry()
    try:  # noqa: SIM105
        parent_ctx.turn_sources_registry = registry
    except (AttributeError, TypeError):
        pass  # frozen ctx — transient (no cross-peer sharing)
    return registry


async def get_turn_sources_registry(
    bus: Any,
    *,
    canvas_store: Any = None,
    canvas_id: str | None = None,
    session_id: str | None = None,
    turn_id: str | None = None,
) -> TurnSourcesRegistry:
    """**唯一**的注册表入口：建表 + 首次回灌一次（canvas-elements 来源合并批）。

    为什么要收口：`ensure_turn_sources_registry` 全仓有 6 个调用点，而回灌只挂了其中 3 个
    ——设计 v1 正是栽在这里（tech-design-cross-turn-sources §2.2）。回灌**不再是正确性前提**
    （合并语义已经落在唯一落库点 `_upsert_turn_sources_locked`），但漏回灌会让本轮的新来源
    从 `[1]` 起铸、与既有条目撞号。收成一个入口 + 一条源码扫描闸，
    让「加第七个调用点」不会再静默漏掉。

    `canvas_store` 缺省时退化成纯建表（等价于旧 `ensure_turn_sources_registry`）。
    """
    reg = ensure_turn_sources_registry(bus)
    if not reg.is_empty() or canvas_store is None or getattr(bus, "_reg_rehydrated", False):
        return reg
    try:
        cid = canvas_id
        if not cid and session_id:
            canvas = await canvas_store.get_canvas_by_session(session_id)
            cid = getattr(canvas, "id", None) if canvas is not None else None
        if not cid:
            return reg  # Canvas not created yet; leave flag unset and retry next time
        # ★ Set the duplicate-rehydration guard only after actual rehydration. The old implementation also set it on four silent skip paths
        # (unavailable canvas_store / canvas not found / exceptions), recording attempted rather than successful rehydration.
        # `rehydrate_turn_registry` swallows exceptions, so inspect its return value; absence of an exception does not prove success.
        if await rehydrate_turn_registry(reg, canvas_store=canvas_store, canvas_id=cid, turn_id=turn_id):
            with contextlib.suppress(Exception):
                bus._reg_rehydrated = True
    except Exception:
        logger.warning(
            "sources_resolver.registry_rehydrate_failed canvas=%s turn=%s",
            canvas_id or session_id,
            turn_id,
            exc_info=True,
        )
    return reg
