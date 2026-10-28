"""Skill tool handlers — **不依赖** bowhead_core / bowhead_router.

三个 handler 返回 (async callable, schema_dict)。由 bowhead_core 侧的
skill_bridge.register_skill_tools() 把它们包成 ToolSpec 注册到 ToolRegistry。

这样避免 skill→core→skill 循环依赖。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from bowhead_skill.bundle_files import BUNDLE_FILE_KINDS, BundleFileError, bundle_file_path, list_bundle_files, read_bundle_text
from bowhead_skill.effective import (
    description_for,
    list_effective_skills,
    user_import_pending,
)
from bowhead_skill.effective import (
    path_under as _path_under,
)
from bowhead_skill.effective import (
    user_enabled as _effective_user_enabled,
)
from bowhead_skill.execution_policy import sandbox_default_timeout_s, sandbox_max_timeout_s
from bowhead_skill.export_naming import export_filename, report_title_from_sections
from bowhead_skill.index import substitute_args
from bowhead_skill.loader import SkillSpec
from bowhead_skill.sandbox_call_shape import (
    FLAT_SANDBOX_TOOL,
    sandbox_call_skeleton,
)
from bowhead_skill.script_langs import (
    argv0_allowlist,
    entry_extensions,
    invoke_command_enabled,
    multiruntime_enabled,
    script_execution_mode,
)
from bowhead_skill.skill_resolver import resolve_skill

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from bowhead_skill.catalog import SkillCatalog
    from bowhead_skill.runtime import SkillRuntime

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────
# Sprint π §K (Layer 1 + Layer 2 metric drain)
# ─────────────────────────────────────────────────────────
def _k_flag_on() -> bool:
    # compute-agent-optimize batch 1 (2026-08-19): default ON. Previously defaulted to "0", and deployment never configured this key,
    # so production INPUTS never included columns/dtypes/sample; models recalled column names from memory (31 KeyErrors on 116 in 30 days =
    # the second-largest sandbox failure category). Previews are capped at 5 rows/20 columns/2KB with PII masking; BOWHEAD_PI_K_ENABLED=0 still disables them.
    return os.environ.get("BOWHEAD_PI_K_ENABLED", "1") == "1"


def _k_workspace_in_allowlist(workspace_id: str | None) -> bool:
    """Empty allowlist → all workspaces. Otherwise membership check."""
    raw = os.environ.get("BOWHEAD_PI_K_ALLOWLIST_WORKSPACES", "").strip()
    if not raw:
        return True
    members = {w.strip() for w in raw.split(",") if w.strip()}
    if not members:
        return True
    return (workspace_id or "") in members


def _k_should_enable(ctx: Any) -> bool:
    """π §K.0 entry gate. Default ON since compute-agent-optimize 批1 (was OFF for 0.11.0 ship)."""
    if not _k_flag_on():
        return False
    ws = getattr(ctx, "workspace_id", None)
    return _k_workspace_in_allowlist(ws)


def _list_references(spec: SkillSpec) -> list[str]:
    return [name for name, _size in list_bundle_files(spec.source_dir, "references")]


def _read_reference(spec: SkillSpec, ref: str) -> dict[str, Any]:
    try:
        path = bundle_file_path(spec.source_dir, "references", ref)
        if not path.is_file() and not Path(ref).suffix:
            ref += ".md"  # Support legacy calls omitting .md; read other extensions by their original names.
        body = read_bundle_text(spec.source_dir, "references", ref)
    except BundleFileError as exc:
        error = f"reference '{ref}' not found" if exc.code == "skill_file_not_found" else f"invalid ref: {ref!r} ({exc.code})"
        return {"error": error, "available": _list_references(spec)}
    return {"name": spec.name, "ref": ref, "body": body}


# follow-up② grounding (design D4): return the skill's invocation contract file to the model as **read-only documentation**
# so it constructs command correctly. ⚠ This is not parsed execution (≠ option a); the platform does not use it to invoke scripts, nor is it injection mitigation:
# content has the same trust level as the SKILL.md body (uploaded by a partially trusted author), with the sandbox as the blast radius. Its advantage over option (a)
# is avoiding the attack surface of platform-side template parsing and execution.
_MAX_HINT_BYTES = 16 * 1024
_INVOCATION_HINT_FILES = ("integration/bowhead-tool-contract.json", "agents/openai.yaml")


def _read_invocation_hints(spec: SkillSpec) -> dict[str, str]:
    """读 skill 根的调用契约文件（若存在），带 resolve-under-source_dir + 非符号链 + size cap 守卫."""
    hints: dict[str, str] = {}
    root = spec.source_dir
    for rel in _INVOCATION_HINT_FILES:
        p = root / rel
        try:
            if p.is_symlink() or not p.is_file() or not _path_under(p, root):
                continue
            if p.stat().st_size > _MAX_HINT_BYTES:
                hints[rel] = f"(省略：文件 > {_MAX_HINT_BYTES} 字节)"
                continue
            hints[rel] = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
    return hints


# _path_under/_user_skill_dir/_is_user_skill_instance/_load_user_skill_spec moved to effective.py with
# the unified view (skill-audit 2026-07); this module uses import aliases at the top.


@dataclass
class SkillToolDef:
    """Tool handler 包 —— bowhead_core 侧自行转 ToolSpec."""

    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[[dict[str, Any], Any], Awaitable[Any]]
    risk_level: str = "low"


async def _fill_input_filenames(input_data: dict[str, Any], ctx: Any) -> None:
    """inputs 里没写 filename 的，宿主侧按 artifact_id 补上真名——模型不用复述.

    2026-08-18 现场：我们的 tool description 明确教模型把
    `"filename": "2025年1-7月onc产品线销售数量中文省份_21b5fc4.csv"` 原样打进 JSON。
    实测这正是流式 tool-args 写崩的位置（`skill_invoke` 报 offset=857，附近是同一串
    文件名被重复吐了一遍还带转义；同一天 `subagent_return` 也有一次 char 2132 的
    JSON 解析失败）。后端自己就知道这个 artifact 叫什么，让模型逐字复述只是白白加长
    payload、多一个出错面。

    只在 filename 缺失时补，不覆盖调用方显式给的值（别人可能有意改名）。查不到就留空，
    由 runner 走原有降级路径——绝不因为补名失败而让整次调用挂掉。
    """
    raw_inputs = input_data.get("inputs")
    if not isinstance(raw_inputs, dict):
        return
    store = getattr(ctx, "artifact_store", None)
    if store is None:
        return
    for spec in raw_inputs.values():
        if not isinstance(spec, dict):
            continue
        aid = spec.get("artifact_id")
        if not aid or spec.get("filename"):
            continue
        try:
            art = await store.get(str(aid))
        except Exception:  # noqa: S112 — Failure to fill filenames must never fail the whole sandbox call.
            continue
        if art is not None and getattr(art, "filename", ""):
            spec["filename"] = art.filename


# followup-context-deck #5 `_resolve_input_artifact_refs` was removed on 2026-09-06
# (sandbox-input-contract L2). Its job, translating simple-int <available_refs> IDs in inputs.artifact_id
# to actual art_id values, now has **one implementation**:
# `bowhead_core.sandbox_inputs_normalize`, wrapped around both sandbox entries by `skill_bridge`
# at registration, before any logic in this file runs.
#
# Why remove it rather than keep it: two implementations on the same path inevitably drift, and the old one **lacked the `kind == "data"`
# guard**. It would resolve a chart simple ref (`"12"` → `sec_abc`) to a non-file ID,
# causing a 404 in the runner. The new implementation copies the `add_chart` guard.


_CANVAS_EXPORT_SKILLS: frozenset[str] = frozenset({"canvas-xlsx", "canvas-pptx", "canvas-pdf", "canvas-docx"})

# Section kinds persisted only during compose; their presence means this turn's report has taken shape.
#
# Why this criterion is needed (116 dogfood 1.0.74, t_f3441c8bb6af): the gate below originally checked only
# whether the canvas had content, but tool-stage chart / code_block sections are persisted **before** compose. Thus
# in a single-turn "create a report + export to Word" request, export ran **6 seconds before** compose (observed: docx
# at 13:45:14, h1/kpi_row/narrative at 13:45:20). Nonempty `rows` ⇒ allowed ⇒ the exported document was
# **only chart data tables, without titles/KPIs/narrative/recommendations**, while the answer still claimed "完整报告已导出".
# The previous version already blocked the empty-canvas case (see the incident below); this is the next form of the same premature export: **content exists,
# but it is not a report**.
_COMPOSED_REPORT_KINDS: frozenset[str] = frozenset({"h1", "narrative", "kpi_row", "section"})


async def _inject_current_canvas_sections(name: str, input_data: dict[str, Any], ctx: Any) -> str | None:
    """canvas-xlsx/pptx/pdf 的 render_from_canvas.py 吃整份 `sections`(ReportSection[])。
    旧契约要求 LLM 把 canvas 全量 JSON(常含 6-10+ chart/table) 逐字塞进 skill_invoke 的
    流式 tool-call 参数——真实故障(t_76b9db41ad57, 2026-07-19): 8-chart canvas 连续两次
    `streamed tool args JSON invalid/truncated`，LLM 放弃，从未产出 .xlsx，用户看到的
    "下载不下来"其实是文件从没生成。

    改为服务端权威灌入：当前 turn 的 canvas 已经通过 add_chart 等落库，直接从
    canvas_store 读 CurrentCanvasContentSet(`list_export_projection`) 覆盖
    input_data["sections"]——LLM 的 tool-call 参数不再需要携带大 payload，从根上消除截断
    风险，也保证导出内容与当前 canvas 一致(不会拿到 LLM 手抄的过期版本)。

    仅在能解出一个非空 canvas 时才覆盖；解不出(无 canvas_store/session，或 canvas 为空
    ——多为单测/eval 场景，未接真 ctx) 时 no-op，保留 LLM 显式传的 `sections`（向后兼容）。

    返回非 None = 必须整个 BLOCK 这次导出(调用方须直接回错，不能再 runtime.run)：
    i18n v1 R4 quarantine 校验与已上线的 job-based export pipeline
    (`canvas_export.py:470-484`) 同款——canvas 里任何 section 被标 quarantined(已知
    数据质量问题，如历史 off-by-N scale 错) 时，那条 export job 会整单拒绝而不是静默
    带着错数字出文件；这里读同一份 projection，必须复刻同一条闸，否则 review 抓到的
    真故障——已知坏数据会绕过闸悄悄烤进每一次未来的 xlsx/pptx/pdf。
    """
    if name not in _CANVAS_EXPORT_SKILLS:
        return None

    # ★★ Structural requirement: **every path that failed to populate sections must converge on the same decision**,
    # instead of independently escaping with `return None`.
    #
    # Incident (116 prod 1.0.74, t_f9c3fc0d5ab4): the previous version blocked only `not rows` (an empty canvas content set),
    # but live reproduction hit **another branch**: export ran before the canvas was created,
    # `get_canvas_by_session` returned None, and an earlier return bypassed the block, still producing
    # a 36776B empty `(empty canvas)` .docx registered as a deliverable,
    # while the model claimed "已导出完成". **Fixing one branch is insufficient; unify the exit.**
    #
    # Four early-return paths (missing canvas_store / missing session / nonexistent canvas / empty content set / data-fetch exception)
    # mean the same thing to users: **this export has no content to render**. Continuing inevitably produces an empty file
    # (canvas-pdf/pptx/xlsx/docx main() all fall back to an `(empty canvas)` placeholder).
    injected = False
    canvas_store = getattr(ctx, "canvas_store", None)
    session_id = getattr(ctx, "session_id", "") or ""
    if canvas_store is not None and session_id:
        try:
            canvas = await canvas_store.get_canvas_by_session(session_id)
            if canvas is not None:
                rows = await canvas_store.list_export_projection(canvas.id)
                quarantined = [r for r in rows if getattr(r, "quarantined", False)]
                if quarantined:
                    reasons = [f"  - section {r.id} ({r.kind}): {r.quarantine_reason or 'flagged'}" for r in quarantined[:5]]
                    return (
                        f"export blocked: {len(quarantined)} canvas section(s) are quarantined due to "
                        "known data quality issues (e.g. off-by-N scale errors in earlier output). "
                        "Re-run the analysis to regenerate those sections, or ask an admin to clear "
                        "the quarantine, before exporting.\n" + "\n".join(reasons)
                    )
                if rows and not any(getattr(r, "kind", "") in _COMPOSED_REPORT_KINDS for r in rows):
                    # Content exists, but consists only of tool-stage output (chart / code_block / …) ⇒ compose has not finished.
                    # Export now produces an unfinished document with data tables only, worse than an empty file because it looks finished,
                    # so the model claims "完整报告已导出", and users discover the missing body only after downloading (P1, see above).
                    # Handle like an empty canvas: produce no file, and make the model wait for the report to take shape before exporting.
                    logger.warning(
                        "canvas_export_skill.blocked_precompose name=%s session=%s kinds=%s",
                        name,
                        session_id or "-",
                        sorted({str(getattr(r, "kind", "")) for r in rows})[:6],
                    )
                    return (
                        "export blocked: the report has not been composed yet — the canvas currently "
                        "holds only intermediate artifacts (charts/code), with no title, KPIs or "
                        "narrative. Exporting now would produce a file of bare data tables that looks "
                        "finished but is not. Finish the analysis so the report lands on the canvas, "
                        "then export. Do NOT tell the user the file was generated."
                    )
                if rows:
                    input_data["sections"] = [r.to_report_section() for r in rows]
                    if canvas.title and not input_data.get("title"):
                        input_data["title"] = canvas.title
                    injected = True
        except Exception:
            logger.warning("canvas_export_skill.auto_sections_failed name=%s", name, exc_info=True)

    # Single exit: neither injected nor supplied by the caller → fail loudly rather than produce an empty file.
    # ⚠ Allow explicitly supplied sections (preserve the existing unit-test / eval-harness contract).
    if not injected and not (input_data.get("sections") or []):
        logger.warning("canvas_export_skill.blocked_empty name=%s session=%s", name, session_id or "-")
        return (
            "export blocked: there is no canvas content to export right now (the report may "
            "still be generating, or this session has no canvas yet). Exporting now would "
            "produce an empty file. Wait until the report is on the canvas, then export again. "
            "Do NOT tell the user the file was generated."
        )
    return None


def _rel_to_workdir(p: Any, workdir: Any) -> str:
    """feedback-0713 #4b: 宿主输出路径 → workdir 相对名（防泄漏一次性绝对路径）。"""
    try:
        if workdir:
            return str(Path(str(p)).relative_to(Path(str(workdir))))
    except ValueError:
        pass
    return Path(str(p)).name


def _output_artifacts_from_result_json(result_json: Any) -> list[dict[str, Any]]:
    """D5: project the uploaded artifacts (real art_ ids) out of result_json['outputs'].

    The inner runner uploads each output file and records its artifact_id; the tool result
    otherwise only re-exposed bare file paths, so the peer improvised a path as
    data_artifact_id (→ invalid_artifact_id_format) and re-queried bi_query to get a real id.
    """
    rj = result_json if isinstance(result_json, dict) else {}
    outs = rj.get("outputs") if isinstance(rj.get("outputs"), list) else []
    projected: list[dict[str, Any]] = []
    for output in outs:
        if not isinstance(output, dict) or not output.get("artifact_id"):
            continue
        item = {
            "artifact_id": output.get("artifact_id"),
            "name": output.get("name"),
            "mime": output.get("mime"),
            "size": output.get("size"),
        }
        if output.get("data_role") in {"stdout", "structured"}:
            item["data_role"] = output["data_role"]
        projected.append(item)
    return projected


def _spec_origin(catalog: SkillCatalog, name: str, spec: SkillSpec) -> str:
    """audit 行的来源归类：catalog | user | team（skill-team-governance，PRD 验收 9）.

    source_dir 是 resolve 的权威落点（user 腿只落 .data/user_skills/、team 腿只落
    .data/team_skills/），路径即来源——审批 dogfood 按 origin=team 统计调用数/失败率，
    旧 'user_or_team' 合桶不可统计。"""
    if catalog.get(name) is not None:
        return "catalog"
    src = Path(str(getattr(spec, "source_dir", "")))
    if _path_under(src, Path(".data") / "user_skills"):
        return "user"
    if _path_under(src, Path(".data") / "team_skills"):
        return "team"
    return "user_or_team"  # Unusual roots (test stubs, etc.): conservatively group them without mislabeling.


def build_skill_tools(
    catalog: SkillCatalog,
    runtime: SkillRuntime,
    *,
    authorize_invoke: Callable[[str, Any], Awaitable[dict[str, Any] | None]] | None = None,
    material_observer: Any | None = None,
) -> list[SkillToolDef]:
    """Build handlers with an optional host-owned final authorization check.

    The registry supplies current resource authorization without a Skill-to-core
    dependency. Standalone callers retain their existing local runtime contract.
    """

    from bowhead_skill.runtime_scope import (
        assigned_skill_active,
        assigned_skill_operation_denial,
        filter_listed_skills,
        read_reference_skill,
        reference_skill_active,
        reference_skill_operation_denial,
        skill_operation_denial,
        skill_script_runtime_denial,
    )

    def _is_catalog(name: str, ctx: Any) -> bool:
        if assigned_skill_active(ctx) and getattr(ctx, "assigned_skill_material", None) is not None:
            return ctx.assigned_skill_material.source_kind == "builtin"
        return catalog.get(name) is not None

    async def _import_pending(ctx: Any, spec_name: str) -> bool:
        if assigned_skill_active(ctx) and getattr(ctx, "assigned_skill_material", None) is not None:
            return False  # The exact source gate checked import state.
        return await user_import_pending(ctx, spec_name)

    def _import_pending_error(spec_name: str) -> dict[str, Any]:
        # skill-import batch 1 (tech-design r3 §3.4): reject pending imports in every ordinary turn; personal skills always resolve for
        # their owner, so "owner can read" is an ineffective gate. error_kind provides a stable machine-readable category distinct from disabled;
        # show an ordinary step failure and explicitly say "do not retry" in the message.
        return {
            "error_kind": "skill_import_pending",
            "error": (
                f"skill '{spec_name}' 是待处理的导入：清点发现它引用了这里不存在的工具/文件，尚未修正，"
                "本轮不可读取或执行。请用户到 设置 → Skills 查看导入报告并修正后再用；不要重试。"
            ),
        }

    async def _user_enabled(ctx: Any, spec_name: str) -> bool:
        """查 ctx.skill_instance_store，判断当前 user 是否启用此 skill（权威在 effective.py）."""
        if assigned_skill_active(ctx) and getattr(ctx, "assigned_skill_material", None) is not None:
            return True  # Do not substitute a same-name source preference.
        return await _effective_user_enabled(ctx, catalog, spec_name)

    async def _resolve(name: str, ctx: Any) -> SkillSpec | None:
        """Per-request 归属解析（workflow-solidify §1）：信任根 → ctx-scoped user/team skill.

        store 从 ctx duck-type（skill_instance_store 已在 ctx；team_skill_store S4 接）—
        无需 build_skill_tools 扩签名。结构性多租户隔离，shadow/碰撞不可能。team-skill 是本
        路径独有（routes/skills.py 只管 personal）；与 main 的 grants 模型并存：resolve 负责
        "按 owner 找到 spec"，grants/停用可见性由 effective.skill_visible 负责（_list 经视图）.
        """
        if assigned_skill_active(ctx) and getattr(ctx, "assigned_skill_material", None) is not None:
            return ctx.assigned_skill_material.spec
        return await resolve_skill(
            name,
            catalog=catalog,
            user_id=getattr(ctx, "user_id", None),
            team_id=getattr(ctx, "team_id", None),
            user_skill_store=getattr(ctx, "skill_instance_store", None),
            team_skill_store=getattr(ctx, "team_skill_store", None),
            # content-permissions S4: team_skill IDs shared with the user, prefetched for the turn
            # (turn_runner injects them into ctx); default None → visibility alone (fail-safe).
            granted_team_skill_ids=getattr(ctx, "granted_team_skill_ids", None),
        )

    async def _list(_args: dict[str, Any], ctx: Any) -> Any:
        if denial := skill_operation_denial(ctx, "skill_list", _args):
            return denial
        if denial := await reference_skill_operation_denial(ctx, "skill_list", _args):
            return denial
        if denial := await assigned_skill_operation_denial(ctx, "skill_list", _args):
            return denial
        # skill-audit 2026-07 unified view: the single source of truth for merging catalog/user/team is
        # effective.list_effective_skills (= the list form of resolve_skill + disabled filtering).
        # Consumers must not merge all three independently; this previously skipped disabled filtering for team skills (listed but not invocable).
        capture = material_observer is not None and material_observer.active(ctx)
        try:
            entries = await list_effective_skills(ctx, catalog, strict=capture)
        except Exception:
            if not capture:
                raise
            entries = await list_effective_skills(ctx, catalog)
            capture = False
        entries = filter_listed_skills(ctx, entries)
        if capture:
            await material_observer.listed(entries, _args, ctx)
        return [
            {
                "name": e.name,
                "description": e.description,
                "version": e.version,
                "scripts": list(e.scripts),
            }
            for e in entries
        ]

    async def _read(args: dict[str, Any], ctx: Any) -> Any:
        if denial := skill_operation_denial(ctx, "skill_read", args):
            return denial
        if denial := await reference_skill_operation_denial(ctx, "skill_read", args):
            return denial
        if denial := await assigned_skill_operation_denial(ctx, "skill_read", args):
            return denial
        if reference_skill_active(ctx):
            return await read_reference_skill(ctx, args)
        name = str(args.get("name") or "").strip()
        spec = await _resolve(name, ctx)  # Per-request ownership resolution (catalog→user→team, §1.2).
        if spec is None:
            return {"error": f"skill '{name}' not found"}
        if await _import_pending(ctx, name):
            return _import_pending_error(name)
        if not await _user_enabled(ctx, name):
            return {"error": f"skill '{name}' disabled by user"}

        if material_observer is not None:
            await material_observer.reading(spec, args, ctx)

        # M137 follow-up: `ref` lets the LLM fetch .md files from the references/ subdirectory
        # (keep them outside the SKILL.md body to keep it concise; read subdocuments on demand).
        file = str(args.get("file") or "").strip()
        if file:
            kind, _, filename = file.partition("/")
            try:
                body = read_bundle_text(spec.source_dir, kind, filename)
            except BundleFileError as exc:
                result = {"error": str(exc), "error_kind": "invalid_args"}
                if material_observer is not None:
                    await material_observer.read_rejected(args, result, ctx)
                return result
            return {"name": spec.name, "file": file, "body": body}
        ref = args.get("ref")
        if ref is not None and str(ref).strip():
            return _read_reference(spec, str(ref).strip())

        body = spec.body_md
        raw_args = args.get("args") or ""
        body = substitute_args(body, raw_args) if isinstance(raw_args, list) else substitute_args(body, str(raw_args))
        # List references/ files so the LLM knows what it can read in depth instead of inventing ref names.
        references_available = _list_references(spec)
        # M137 follow-up: frontmatter `pipes_output_to: <other-skill-name>` means
        # read that SOP after running this one to complete the pipeline. Expose it to the LLM for chained reading.
        pipes_to = spec.frontmatter.get("pipes_output_to")
        result: dict[str, Any] = {
            "name": spec.name,
            "description": await description_for(ctx, spec, is_catalog=_is_catalog(name, ctx)),
            "version": spec.version,
            "allowed_tools": spec.allowed_tools,
            "scripts": spec.script_names,
            "script_execution": script_execution_mode(
                has_scripts=bool(spec.scripts), builtin=_is_catalog(name, ctx), knowledge=getattr(spec, "kind", "tool") == "knowledge"
            ),
            "references": references_available,
            "body": body,
        }
        # Editing a skill through conversation needs **the full original SKILL.md on disk**: `body` is a display form with frontmatter removed
        # and substitute_args applied. Saving it back loses every frontmatter field except
        # name/description/version/allowed_tools (including title/category/trigger_examples/
        # kind/sandbox_requirements…), because the model can reconstruct frontmatter only from those visible fields.
        # Observed on 116 on 2026-09-02: the agent did construct frontmatter itself and submit it back.
        # Provide this only for **editable user skills** (built-ins cannot be edited; an extra full copy only wastes tokens).
        if not _is_catalog(name, ctx):
            manifest = spec.source_dir / "SKILL.md"
            try:
                if manifest.is_file():
                    result["body_raw"] = manifest.read_text(encoding="utf-8")
                    store = getattr(ctx, "skill_instance_store", None)
                    uid = getattr(ctx, "user_id", None)
                    personal = await store.get(user_id=uid, name=name) if store is not None and uid else None
                    is_personal = getattr(personal, "source_type", None) in {"user_upload", "user_generated"}
                    result["origin"] = "user" if is_personal else "team"
                    result["files"] = {kind: [n for n, _size in list_bundle_files(spec.source_dir, kind)] for kind in BUNDLE_FILE_KINDS}
                    result["edit_hint"] = (
                        "在 body_raw（完整 SKILL.md）上修改，再用 skill_save_generated(body_md=<修改后的原文>, overwrite=true) 保存个人版；只改正文时省略 scripts。"
                        if is_personal
                        else "这是团队版。修改前必须在设置 → Skills 中使用「复制为我的」复制完整 bundle，再修改个人版并提交团队审批；不要直接调用 skill_save_generated 冒充修改团队版。"
                    )
            except OSError:
                pass
        if pipes_to:
            result["pipes_output_to"] = str(pipes_to)
            result["next_step_hint"] = f'完成本 SOP 的分析后，下一步**必须**调 `skill_read(name="{pipes_to}")` 看输出契约，再调对应工具收尾.'
        # follow-up② adoption/grounding (design D4, LLM-behavior P1): executable user/team skill
        # + flag ON → give the model a command scaffold to fill, a token vocabulary, and the skill's invocation contract (if present),
        # replacing inference about a new parameter with filling the blanks the tool just supplied.
        if (
            spec.scripts
            and invoke_command_enabled()
            and multiruntime_enabled()
            and not _is_catalog(name, ctx)
            and getattr(spec, "kind", "tool") != "knowledge"
        ):
            _scripts = spec.script_names
            _example_entry = next(
                (s for s in _scripts if s.endswith((".py", ".js", ".mjs", ".sh"))),
                "<脚本>",
            )
            result["command_usage"] = (
                "这是可执行的上传 skill。跑它的脚本：按上方 body(SKILL.md) 里**文档化的命令**调 "
                f'`skill_invoke(name="{spec.name}", command=[...argv...], input={{"inputs":{{...}}}})`。'
                "command 是 argv 数组（不是 shell 串），工作目录 = skill 根、脚本路径写相对（如 "
                f'"scripts/{_example_entry}"）。用 {{inputs.<名>}} 引用你在 input.inputs 里声明的工件、'
                "{output_dir} 作输出目录（产物必须写到 {output_dir} 才会被捕获；**不要**用 SKILL.md 里"
                "指向 examples/ 的示例路径或 output/ 相对目录）。"
            )
            _hints = _read_invocation_hints(spec)
            if _hints:
                result["invocation_hints"] = _hints
        return result

    async def _invoke(args: dict[str, Any], ctx: Any) -> Any:
        if denial := skill_operation_denial(ctx, "skill_invoke", args):
            return denial
        if denial := await reference_skill_operation_denial(ctx, "skill_invoke", args):
            return denial
        if denial := await assigned_skill_operation_denial(ctx, "skill_invoke", args):
            return denial
        # tool_registry inserts _args_parse_error when raw_arguments parsing fails.
        # Report it explicitly so the LLM knows the previous args were invalid and to resend standard JSON, instead of misdiagnosing
        # "skill '' not found".
        if args.get("_args_parse_error"):
            return {
                "error": (
                    f"tool args parse failed: {args['_args_parse_error']}. "
                    "请检查：不要在 JSON 里混 XML tag（如 '<property name>'）"
                    "或 'input' 写成 JSON 字符串. 重发标准 JSON，例如："
                    '{"name":"python_sandbox","script":"runner.py","input":{"code":"...","inputs":{...}}}'
                )
            }
        name = str(args.get("name") or "").strip()
        requested_name = name
        _direct_python_sandbox = name == "python_sandbox" and _is_catalog(name, ctx)
        script = str(args.get("script") or "").strip()
        input_data = args.get("input") or {}
        if not isinstance(input_data, dict):
            return {
                "error": (
                    f'input must be an object like {{"code": "<python>", "inputs": {{...}}, "timeout_sec": 30}}. got: {type(input_data).__name__}'
                )
            }
        # skill-multiruntime: _skill_dir/_skill_entry are server-injected control keys; strip all LLM-supplied
        # values (matching defensive stripping of _pi_k_/_sandbox_executor_*).
        input_data.pop("_skill_dir", None)
        input_data.pop("_skill_entry", None)
        # follow-up②: command-mode control keys are also server-only; unconditionally strip values supplied directly by the LLM.
        # In particular, _skill_command_enabled is a rollout marker; without stripping, the LLM could put it in input when the flag is OFF
        # to bypass rollout gating (Security P1-2). The sanctioned entry is the top-level `command` parameter below.
        input_data.pop("_skill_command", None)
        input_data.pop("_skill_command_enabled", None)
        input_data.pop("_skill_argv0_allowlist", None)
        # Results Workspace RW-ARTIFACT-001: producers must state delivery intent explicitly;
        # the runner must not infer it from extensions such as MP4/PNG. The LLM may use only the public purpose field;
        # strip all server control keys first, then reconstruct them from validated values.
        input_data.pop("_output_purpose", None)
        input_data.pop("_deliverable_outputs", None)
        _output_purpose = "analysis"
        _deliverable_outputs: list[str] = []
        if _direct_python_sandbox:
            _raw_output_purpose = str(input_data.pop("purpose", "analysis") or "analysis").strip()
            if _raw_output_purpose not in {"analysis", "deliverable"}:
                return {
                    "error_kind": "invalid_args",
                    "error": (
                        "python_sandbox input.purpose must be 'analysis' or "
                        "'deliverable'. Use deliverable only when the user asked "
                        "for the generated file as a final output."
                    ),
                }
            _output_purpose = _raw_output_purpose
            _raw_deliverable_outputs = input_data.pop("deliverable_outputs", [])
            if _output_purpose == "deliverable":
                if not isinstance(_raw_deliverable_outputs, list):
                    return {
                        "error_kind": "invalid_args",
                        "error": "python_sandbox input.deliverable_outputs must be a filename list.",
                    }
                for _candidate in _raw_deliverable_outputs:
                    if not isinstance(_candidate, str):
                        return {
                            "error_kind": "invalid_args",
                            "error": ("python_sandbox input.deliverable_outputs entries must be top-level output filenames without paths."),
                        }
                    _filename = _candidate.strip()
                    if not _filename or len(_filename) > 255 or "/" in _filename or "\\" in _filename or Path(_filename).name != _filename:
                        return {
                            "error_kind": "invalid_args",
                            "error": ("python_sandbox input.deliverable_outputs entries must be top-level output filenames without paths."),
                        }
                    if _filename not in _deliverable_outputs:
                        _deliverable_outputs.append(_filename)
                if not _deliverable_outputs:
                    return {
                        "error_kind": "invalid_args",
                        "error": ("python_sandbox purpose='deliverable' requires at least one exact filename in input.deliverable_outputs."),
                    }
            elif _raw_deliverable_outputs:
                return {
                    "error_kind": "invalid_args",
                    "error": ("python_sandbox input.deliverable_outputs requires purpose='deliverable'."),
                }
        # follow-up② model-driven command (argv array assembled by the model from commands documented in the skill's SKILL.md).
        _command = args.get("command")
        _orig_skill_name: str | None = None  # D7 attribution: rewrite changes name→python_sandbox.

        # Heuristic: Lead sometimes puts Python code in `script` (schema confusion;
        # script should be only a **filename**, such as 'runner.py'). If multiline code or an import
        # prefix is detected, explicitly report the correct shape to the LLM rather than silently looking up `import pandas...`
        # as a script filename.
        if script and ("\n" in script or script.startswith(("import ", "from ", "def ", "class ", "print("))):
            return {
                "error": (
                    f"script must be a SCRIPT FILE NAME (e.g. 'runner.py'), not python code. "
                    f"Got multi-line code starting with {script[:60]!r}. "
                    f"Correct shape: "
                    '{"name":"python_sandbox","script":"runner.py",'
                    '"input":{"code":"<python>","inputs":{"rows":{"artifact_id":"..."}}}}'
                )
            }

        # chat-polish-r2 Phase 4 (2026-05-26): give a separate, clear error for empty `name`.
        # LLMs occasionally omit name; "skill '' not found" is too cryptic, wasting 1-2
        # tool turns on trial-and-error recovery. Explicit skill_list guidance speeds self-recovery.
        # (2026-07-13 corrected a nonexistent name: the previous guidance named `skill_list_for_role`, which does not exist
        # (the actual name is skill_list); following it hit "not registered" and wasted another turn.)
        if not name:
            return {
                # invalid_args: LLM arg-shape slip — the agent retries autonomously,
                # so the step classifier must NOT surface a temporary-failure / retry-or-skip card
                # for it (prod t_2e569479 self-recovered yet still showed the card).
                "error_kind": "invalid_args",
                "error": ("skill_invoke 需要 'name' 参数. 先用 `skill_list` 看可用 skills, 再传 name='<skill>' + script='<file>' + input={...}."),
            }
        spec = await _resolve(name, ctx)  # Per-request ownership resolution (catalog→user→team, §1.2).
        if spec is None:
            return {"error": f"skill '{name}' not found"}
        _origin = _spec_origin(catalog, name, spec)
        if assigned_skill_active(ctx):
            _kind = ctx.assigned_skill_material.source_kind
            _origin = "catalog" if _kind == "builtin" else _kind
        if await _import_pending(ctx, name):
            return _import_pending_error(name)
        if not await _user_enabled(ctx, name):
            return {"error": f"skill '{name}' disabled by user"}
        # excel RC-1 fix: knowledge-kind skills (e.g. xlsx) are reference-only, not executable. Previously
        # their office bundle scripts were included in `available` by rglob; the LLM chose __init__.py to execute
        # → entry_script not found (cascading incomplete output in production). Redirect explicitly: read via skill_read;
        # run code / aggregate / clean tables via python_sandbox. error_kind=invalid_args prevents the step
        # classifier from showing a temporary-failure / retry card (the LLM recovers autonomously).
        if getattr(spec, "kind", "tool") == "knowledge":
            return {
                "error_kind": "invalid_args",
                "error": (
                    f"skill '{name}' 是知识参考 skill（不可执行）。读它用 "
                    f"skill_read(name='{name}')；要跑代码 / 聚合 / 筛选 / 清洗表格，走 "
                    'python_sandbox：skill_invoke name="python_sandbox" script="runner.py" '
                    'input={"code":"<python>","inputs":{"rows":{"artifact_id":"..."}}}。'
                ),
            }
        if _direct_python_sandbox:
            # python_sandbox has helper modules beside runner.py. It is a single
            # authoring capability. Omission defaults safely, but an explicit wrong
            # entrypoint is rejected so trace arguments stay equal to execution.
            if script and script != "runner.py":
                return {
                    "error_kind": "invalid_args",
                    "error": (f"python_sandbox only accepts script='runner.py'; got {script!r}. Put Python source in input.code."),
                }
            script = "runner.py"
        # follow-up②: determine command mode and report sub-gate errors loudly (design D2/D6). Command mode is
        # a **strict sub-gate** of BOWHEAD_SKILL_MULTIRUNTIME, reachable only for uploaded user/team skills with both flags ON.
        # Invalid combinations fail loudly rather than being silently discarded (CTO P2-1 / Backend P2).
        if _command is not None and not isinstance(_command, list):
            return {
                "error_kind": "invalid_args",
                "error": (
                    "command 必须是 argv 数组（string[]），例："
                    '["python3","scripts/run_pipeline.py","--spec","{inputs.spec}","--output","{output_dir}/deck.pptx"]'
                ),
            }
        _cmd_provided = isinstance(_command, list) and len(_command) > 0
        _cmd_mode = False
        if _cmd_provided and invoke_command_enabled():
            if not multiruntime_enabled():
                return skill_script_runtime_denial(name, uploaded=True, command=True)
            if _is_catalog(name, ctx):
                return {
                    "error_kind": "invalid_args",
                    "error": (f"command 模式只用于上传的 user/team skill；'{name}' 是内置 skill（内置脚本走各自路径，不经 command）"),
                }
            _cmd_mode = True

        if _direct_python_sandbox:
            code = input_data.get("code")
            if not isinstance(code, str) or not code.strip():
                return {
                    "error_kind": "invalid_args",
                    "error": (
                        "python_sandbox requires non-empty input.code. "
                        "Use exactly: "
                        '{"name":"python_sandbox","script":"runner.py",'
                        '"input":{"code":"<python>","inputs":{},"timeout_sec":30}}'
                    ),
                }

        if not script and not _cmd_mode:
            if len(spec.scripts) == 1:
                script = spec.script_names[0]
            else:
                return {
                    "error": "script required",
                    "available": spec.script_names,
                }

        extra_args = args.get("extra_args") or []
        if not isinstance(extra_args, list):
            extra_args = [str(extra_args)]

        # skill-audit batch C (security review P0-conditional): when the multiruntime flag is OFF, user/team
        # skill execution fails closed. Previously it fell back to unisolated execution on the gateway host (.py directly via
        # create_subprocess_exec, with PYTHONPATH able to import bowhead_core and access DB/secrets);
        # isolation must be guaranteed structurally rather than depend on flag settings. Trusted-root (catalog) skills are unaffected.
        if not multiruntime_enabled() and not _is_catalog(name, ctx):
            # review P3-1: denied execution attempts are exactly what auditing needs to record (the audit line follows runtime.run,
            # so early returns bypass it).
            logger.info(
                "skill_invoke.audit skill=%s origin=%s user=%s workspace=%s ok=denied reason=multiruntime_off",
                name,
                _origin,
                getattr(ctx, "user_id", None),
                getattr(ctx, "workspace_id", None),
            )
            # Uploaded scripts only run in the sandbox: fail closed, never on the gateway host.
            return skill_script_runtime_denial(name, uploaded=True, command=False)

        # skill-multiruntime S1 (D4): rewrite multilingual user/team skill scripts as python_sandbox
        # invocations to run in strongly isolated containers instead of unisolated on the host. Change name+spec before all name=="python_sandbox"
        # injections below and runtime.run's _build_env(spec.name); otherwise sandbox orchestration
        # configuration is missed entirely → gVisor silently falls back to runc (dogfood-0708 F6). Trusted-root skills (catalog matches)
        # are not rewritten and still use the host path.
        _user_reqs: list[str] | None = None
        if multiruntime_enabled() and not _is_catalog(name, ctx):
            _entry_names = set(spec.script_names)
            # Command mode does not validate a single entry (argv contains the script path; the runner host side guards paths);
            # legacy/shim mode still requires entry ∈ the allowlist and a supported extension.
            if not _cmd_mode:
                if script not in _entry_names:
                    return {
                        "error_kind": "invalid_args",
                        "error": f"script '{script}' 不在 skill '{name}' 的可执行脚本里",
                        "available": sorted(_entry_names),
                    }
                if Path(script).suffix not in entry_extensions():
                    return {
                        "error_kind": "invalid_args",
                        "error": f"script '{script}' 扩展名 {Path(script).suffix} 不被支持",
                    }
            _sandbox_spec = catalog.get("python_sandbox")
            if _sandbox_spec is None:
                return {"error": "python_sandbox skill unavailable (multiruntime)"}
            _orig_skill_name = name  # D7 attribution logging (rewrite changes name→python_sandbox).
            _user_skill_dir = str(spec.source_dir)
            _user_reqs = list(getattr(spec, "sandbox_requirements", []) or [])
            _skill_entry_name = script
            # Switch identity: everything afterward uses python_sandbox (all injections + _build_env take effect).
            name = "python_sandbox"
            spec = _sandbox_spec
            script = "runner.py"
            input_data = {
                **input_data,
                "_skill_dir": _user_skill_dir,
                "_skill_entry": _skill_entry_name,
                "timeout_sec": input_data.get("timeout_sec") or sandbox_default_timeout_s(),
            }
            if _cmd_mode:
                # Server-set marker (rollout authority) + original argv (including {inputs.X}/{output_dir} tokens)
                # + argv[0] allowlist (single source of truth). Token substitution + guards run on the runner host side (resolved paths
                # exist there); pass only the original argv here, without substitution.
                input_data["_skill_command"] = list(_command or [])
                input_data["_skill_command_enabled"] = True
                input_data["_skill_argv0_allowlist"] = sorted(argv0_allowlist())

        # followup-context-deck #5: translate simple-int <available_refs> ids in
        # python_sandbox inputs.artifact_id → real art_ids via the turn's ref
        # allowlist, BEFORE any downstream (preview / fetch / drain). See helper.
        if name == "python_sandbox":
            if _direct_python_sandbox:
                input_data["_output_purpose"] = _output_purpose
                input_data["_deliverable_outputs"] = _deliverable_outputs
            # Fill filenames **after** resolving refs to actual IDs (simple-int IDs cannot be looked up).
            await _fill_input_filenames(input_data, ctx)

        # π §K Layer 1 — flag-gated INPUTS schema injection
        k_enabled_for_call = name == "python_sandbox" and _k_should_enable(ctx)
        if k_enabled_for_call:
            # Defense-in-depth (r1 §K.5 open question #4): strip any
            # `_preview_hint` the LLM might have smuggled into args.input.inputs.
            # K Layer 1 doesn't use _preview_hint at all (cache stays in
            # gateway per CTO/Eng P0-3 r2 §K.1) so any incoming value is
            # untrusted.
            raw_inputs = input_data.get("inputs") or {}
            if isinstance(raw_inputs, dict):
                for _nm, _spec in raw_inputs.items():
                    if isinstance(_spec, dict) and "_preview_hint" in _spec:
                        _spec.pop("_preview_hint", None)
            # Pass flag through input.json (env vars with BOWHEAD_ prefix are
            # stripped by SkillRuntime._build_env). Runner main() reads this
            # field and passes to _materialize_inputs.
            input_data["_pi_k_enabled"] = True
            input_data["_pi_k_redact_pii"] = os.environ.get("BOWHEAD_PI_K_PII_REDACT", "1") == "1"
        elif name == "python_sandbox":
            # review batch 1-R1: when the gateway says OFF, pass False **explicitly**. Previously the key was popped, so None made the runner fall back
            # to the environment; SkillRuntime._build_env had already stripped BOWHEAD_*, and the runner's default was already "1",
            # rendering BOWHEAD_PI_K_ENABLED=0 / the workspace allowlist ineffective in production.
            # Explicit False also overrides any LLM-supplied _pi_k_enabled=True (preserving the original defensive intent of pop).
            input_data["_pi_k_enabled"] = False
            input_data.pop("_pi_k_redact_pii", None)
        else:
            # Non-sandbox skills: these two keys have no meaning and must not be supplied by the LLM.
            input_data.pop("_pi_k_enabled", None)
            input_data.pop("_pi_k_redact_pii", None)

        # pptx-from-template M3w.2 (RD-2): thread session_id + turn_id into the
        # runner (via input.json control keys, like _pi_k_* above) so sandbox
        # outputs can be session-pinned + typed at upload — a generated deck
        # becomes kind=deliverable with created_by_turn = the owning turn, which
        # the is_report_worthy keep-signal (turn_has_deliverable) then sees. Set
        # from ctx (NOT trusting any LLM-emitted value). Runner main() reads them.
        # Scoped to python_sandbox only (these keys are meaningless to other
        # skills and would otherwise leak into their echoed input).
        if name == "python_sandbox":
            input_data["_session_id"] = getattr(ctx, "session_id", "") or ""
            input_data["_turn_id"] = getattr(ctx, "turn_id", "") or ""
            # cca P3 F-B: mint a FRESH artifact-fetch-scoped token (via the gateway-set ctx
            # closure — the signing secret never reaches here) so the runner's host-side
            # artifact GET carries the caller's identity. ctx-sourced, NEVER an LLM value.
            # The runner reads this host-side and strips it before the --network=none mount.
            _mint = getattr(ctx, "_mint_artifact_token", None)
            input_data["_artifact_fetch_token"] = _mint() if callable(_mint) else ""
            # pptx-from-template M2 (wheelhouse): thread the skill's declared
            # long-tail deps into the runner via input.json (like _pi_k_* above).
            # Set from the RESOLVED SkillSpec (trusted gateway FS), NOT any
            # LLM-emitted value. Runner main() reads this, regex+lockset-validates
            # each entry, and only then mounts the RO wheelhouse + runs the
            # offline (--no-index --require-hashes) bootstrap. --network=none is
            # never relaxed. python_sandbox only (meaningless to other skills).
            input_data["_sandbox_requirements"] = _user_reqs if _user_reqs is not None else list(spec.sandbox_requirements)
            # Thread the mandatory sandbox-executor URL + shared-secret token to the runner via
            # input.json. MUST go through input.json (not env): the runner
            # subprocess never sees BOWHEAD_* (SkillRuntime._build_env strips that
            # prefix). Read here from the gateway's full env. The runner POSTs the
            # run to the executor, which is the sole Docker owner. Like _artifact_fetch_token, the runner
            # reads these host-side and never copies them into the container-visible
            # input.json → the token never enters the sandbox. python_sandbox only.
            requested_timeout = input_data.get("timeout_sec", sandbox_default_timeout_s())
            if isinstance(requested_timeout, bool) or not isinstance(requested_timeout, int):
                return {"error": "timeout_sec must be an integer number of seconds", "code": "sandbox_timeout_invalid"}
            input_data["timeout_sec"] = max(5, min(requested_timeout, sandbox_max_timeout_s()))
            input_data["_sandbox_executor_url"] = os.environ.get(
                "BOWHEAD_SANDBOX_EXECUTOR_URL",
                "",
            )
            input_data["_sandbox_executor_token"] = os.environ.get(
                "BOWHEAD_SANDBOX_EXECUTOR_TOKEN",
                "",
            )
        else:
            # Defensive: never let an LLM smuggle a deps list into a non-sandbox
            # skill's echoed input (mirrors the _pi_k_enabled strip above).
            input_data.pop("_sandbox_requirements", None)
            # Same defensive strip for the executor control keys — only
            # the python_sandbox runner reads them; never echo them (esp. the
            # shared-secret token) into another (possibly user-uploaded) skill's input.
            input_data.pop("_sandbox_executor_url", None)
            input_data.pop("_sandbox_executor_token", None)

        _canvas_export_block_reason = await _inject_current_canvas_sections(name, input_data, ctx)
        if _canvas_export_block_reason is not None:
            return {"error": _canvas_export_block_reason}

        if material_observer is not None:
            await material_observer.invoking(spec, script, input_data, ctx)

        # Input hydration and canvas preparation can await. Recheck the original
        # receiver/source after observation, immediately before dispatch.
        if denial := await assigned_skill_operation_denial(ctx, "skill_invoke", args):
            return denial

        if authorize_invoke is not None:
            if denial := await authorize_invoke(requested_name, ctx):
                return denial

        if _orig_skill_name is not None and (denial := skill_script_runtime_denial(_orig_skill_name, uploaded=True, command=_cmd_mode)):
            return denial

        result = await runtime.run(
            spec,
            entry_script=script,
            input_data=input_data,
            extra_args=[str(x) for x in extra_args],
        )
        if material_observer is not None:
            await material_observer.executed(result, ctx)

        # follow-up② D7 attribution (SRE P2): rewrite changes name to python_sandbox, so `runtime.run`
        # logs lose the uploaded skill identity / argv. In command mode, log the original skill name + argv here
        # so command failures can be attributed (otherwise operations sees only skill=python_sandbox).
        if _cmd_mode and _orig_skill_name is not None:
            logger.info(
                "skill_invoke.command skill=%s ok=%s exit=%s argv=%r",
                _orig_skill_name,
                getattr(result, "ok", None),
                getattr(result, "exit_code", None),
                (list(_command)[:12] if isinstance(_command, list) else _command),
            )
        # skill-audit batch C (#12): unified execution audit entry with who/which space/which skill/origin/outcome.
        # Previously only CRUD was audited; execution had no records for abuse detection or later investigation. origin is the pre-rewrite identity.
        logger.info(
            "skill_invoke.audit skill=%s origin=%s user=%s workspace=%s ok=%s exit=%s",
            _orig_skill_name or name,
            _origin,  # catalog|user|team (P1b: origin=team is measurable, PRD acceptance 9).
            getattr(ctx, "user_id", None),
            getattr(ctx, "workspace_id", None),
            getattr(result, "ok", None),
            getattr(result, "exit_code", None),
        )

        # Sprint chat-stream-fix C-1 (2026-05-16): inner script can write
        # `{"ok": false, "exit_code": 1, "error": "..."}` to stdout while the
        # subprocess itself exits 0 (skill wrapper ran fine). Prod trace
        # t_1a9f063efbdc seq=25/30: skill_invoke returned outer ok=True but
        # script's own logic failed → the user kept seeing "report 生成中", and the peer
        # ignored the failure. Now propagate inner ok→outer when result_json
        # is structured.
        # skill-multiruntime: the runner nests user skills' structured return values in envelope.skill_result;
        # expose the child script's own return value as external result_json (the envelope only supplies isolation). On non-skill paths, _rj =
        # envelope (no skill_result key), preserving behavior.
        # ⚠ The envelope's ok is the **authoritative** success/failure signal: _mark_input_errors sets envelope.ok=False when input artifact fetching
        # fails (the runner still exits 0 → result.ok stays True). After unpacking skill_result,
        # **check envelope.ok first**, or the child script's exit 0 hides missing inputs and falsely reports success (s_8a923ed27a cases).
        _env = result.result_json
        _rj = _env
        if isinstance(_env, dict) and isinstance(_env.get("skill_result"), dict):
            _rj = _env["skill_result"]
        _inner_ok = True
        _inner_err: str | None = None
        if isinstance(_env, dict) and _env.get("ok") is False:
            _inner_ok = False
            _inner_err = str(_env.get("error") or "") or "sandbox run failed"
        elif isinstance(_rj, dict) and _rj.get("ok") is False:
            _inner_ok = False
            _inner_err = str(_rj.get("error") or "") or f"script exit_code={_rj.get('exit_code', 1)}"
        outer_ok = bool(result.ok) and _inner_ok
        outer_err = result.error or _inner_err
        # D5: surface the uploaded artifacts (real art_ ids) so the peer can chart sandbox
        # output directly via add_chart(data_artifact_id=...) instead of improvising
        # a file path → invalid_artifact_id_format → bi_query re-fetch.
        # feedback-0713 #4b: python_sandbox outputs/workdir no longer expose ephemeral absolute host
        # paths. Every call creates a new mkdtemp directory; old paths become invalid, and reusing them across calls causes
        # FileNotFoundError (prod t_296fb09131ad seq5). The python_sandbox runner
        # already uploads every output as an artifact (output_artifacts references are the correct representation);
        # outputs returns only relative filenames. Other skills (consultant-deck, etc.) still have Lead-side host-path
        # consumers, so preserve their absolute paths.
        _is_sandbox = name == "python_sandbox"
        # P2-a (dev 1.0.77 dogfood): the model sees only sandbox `output.docx`, so its answer says
        # "ready for download as output.docx", while the backend registers it using the report title. Users cannot find the file
        # in the download directory by the answer's name. Return the **actual download name** to the model here.
        # Use the same source and function as registration (`export_naming`) to prevent drift.
        _download_name: str | None = None
        if outer_ok and name in _CANVAS_EXPORT_SKILLS:
            _title = report_title_from_sections(input_data.get("sections") or [], input_data.get("title"))
            _exts = {Path(str(p)).suffix.lower() for p in result.outputs}
            _exts = {e for e in _exts if e}
            if len(_exts) == 1:
                _download_name = export_filename(_title, _exts.pop())
        return {
            "ok": outer_ok,
            # Backend-resolved identity, separate from model arguments and script JSON.
            # Exact released resources retain their identity; no aliases or rebinding.
            **(
                {"executed_document_skill": {"name": spec.name, "origin": _origin, "script": script}}
                if spec.name in {"bowra-docx", "bowra-pdf", "bowra-pptx", "bowra-xlsx"}
                else {}
            ),
            "exit_code": result.exit_code,
            **(
                {
                    "download_filename": _download_name,
                    "_note": (
                        f"The user will see this file as {_download_name!r}. Refer to it by that name — never by the sandbox path (output.docx)."
                    ),
                }
                if _download_name
                else {}
            ),
            "stdout": result.stdout[-2000:],
            "stderr": result.stderr[-2000:],
            "result_json": _rj,
            # review nit: relative to workdir, not basename; preserve subdirectory information so nested files with the same name
            # do not collapse. Fall back to basename when workdir is absent.
            "outputs": ([_rel_to_workdir(p, result.workdir) for p in result.outputs] if _is_sandbox else [str(p) for p in result.outputs]),
            "output_artifacts": _output_artifacts_from_result_json(result.result_json),
            "workdir": None if _is_sandbox else (str(result.workdir) if result.workdir else None),
            "duration_ms": result.duration_ms,
            **(
                {"execution_policy": {"timeout_sec": input_data["timeout_sec"], "maximum_timeout_sec": sandbox_max_timeout_s()}}
                if _is_sandbox
                else {}
            ),
            "error": outer_err,
        }

    async def _invoke_python_sandbox_flat(args: dict[str, Any], ctx: Any) -> Any:
        """批 5ⓑ：扁平入口 —— 把顶层 code/inputs/timeout_sec 拼回老形状交给同一个 `_invoke`。

        **不新建执行路径**：resolve / 用户启用判定 / 审计日志 / runner / executor / 限额 /
        π-K 预览全部沿用 `_invoke` 那一条，这里只做形状转换。
        存在的理由是**形状本身**：老写法把整段 Python 塞进 `input` 这个子对象里的一个长字符串，
        流式 tool-args 一旦截断或转义出错，整次调用作废（116 30 天 72 次；`_fill_input_filenames`
        的注释里有 offset=857 那次的现场）。顶层字符串少一层嵌套、少一层转义。
        """
        if denial := await reference_skill_operation_denial(ctx, "python_exec", args):
            return denial
        if denial := await assigned_skill_operation_denial(ctx, "python_exec", args):
            return denial
        if args.get("_args_parse_error"):
            return {
                "error_kind": "invalid_args",
                "error": (
                    f"tool args parse failed: {args['_args_parse_error']}. "
                    '重发标准 JSON，例如：{"code":"<python>","inputs":{"rows":{"artifact_id":"art_..."}}}'
                ),
            }
        code = args.get("code")
        if not isinstance(code, str) or not code.strip():
            return {
                "error_kind": "invalid_args",
                "error": 'python_sandbox 需要顶层 "code" 参数（Python 源码字符串）。',
            }
        inner: dict[str, Any] = {"code": code}
        raw_inputs = args.get("inputs")
        if isinstance(raw_inputs, dict):
            inner["inputs"] = raw_inputs
        elif raw_inputs is not None:
            return {
                "error_kind": "invalid_args",
                "error": f'inputs 必须是对象，形如 {{"rows": {{"artifact_id": "art_..."}}}}。got: {type(raw_inputs).__name__}',
            }
        if "purpose" in args:
            inner["purpose"] = args["purpose"]
        if "deliverable_outputs" in args:
            inner["deliverable_outputs"] = args["deliverable_outputs"]
        # `intent` (user-facing source wording) is **deliberately not copied into `inner`**: source labels read the recorded
        # `ToolInvocation.arguments`, which for the flat entry is the top-level `args` here; `intent` is already present.
        # `inner` is a new dict used only by the `_invoke` execution path; copying intent adds only an unread key to the runner payload
        # (the container cannot access it either: python_sandbox container input includes only `inputs`).
        # Accept numeric strings / floats (models often write "60"); reject invalid values **explicitly**, rather than silently falling back to the 30s
        # default and giving users an unexplained timeout (invalid `inputs` types fail loudly; keep this symmetric).
        timeout = args.get("timeout_sec")
        if isinstance(timeout, bool):
            return {"error_kind": "invalid_args", "error": "timeout_sec 必须是数字（秒），不是布尔值。"}
        if timeout is not None:
            try:
                inner["timeout_sec"] = int(float(timeout))
            except (TypeError, ValueError, OverflowError):  # inf/1e400 raises OverflowError.
                return {
                    "error_kind": "invalid_args",
                    "error": f"timeout_sec 必须是数字（秒）。got: {timeout!r}",
                }
        # L1: render and pass down **this entry's** call skeleton for sandbox `INPUTS` KeyError messages.
        # The entry is known here (reaching this handler means the flat entry); no inference needed.
        inner["_call_shape"] = sandbox_call_skeleton(FLAT_SANDBOX_TOOL)
        # Other places in this file also use this literal directly (`name == "python_sandbox"` appears 5 times);
        # stay consistent instead of importing runtime for one constant (which would turn a TYPE_CHECKING-only dependency into a runtime dependency).
        return await _invoke({"name": "python_sandbox", "script": "runner.py", "input": inner}, ctx)

    # follow-up② skill_invoke schema: advertise `command` **only when the flag allows it** (hidden when the flag is OFF,
    # preventing empty model submissions from wasting tokens; SRE P2).
    _invoke_props: dict[str, Any] = {
        "name": {"type": "string", "description": "必填. 要运行的 skill 名, e.g. 'python_sandbox'. 不确定先调 skill_list."},
        "script": {
            "type": "string",
            "description": "scripts/ 下的完整相对路径（保留子目录），从 skill_read 返回的 scripts 中选择。若只有一个脚本可省略",
        },
        "input": {
            "type": "object",
            "description": (
                "python_sandbox 必须传非空 code。标准形态："
                '{"name":"python_sandbox","script":"runner.py",'
                '"input":{"code":"<python>","inputs":{},"timeout_sec":30,'
                '"purpose":"analysis|deliverable","deliverable_outputs":["final.mp4"]}}。'
                '用户明确要求把生成文件交付/下载时传 purpose="deliverable"，并在 '
                "deliverable_outputs 列出代码实际写出的顶层文件名；计算中间文件保持默认 analysis"
            ),
        },
        "extra_args": {"type": "array", "items": {"type": "string"}},
    }
    _invoke_desc = (
        "在沙箱里跑 skill 的一个 script。**必传 name**（skill 名）+ script（脚本文件名）。"
        "CPU/内存/墙钟 limits，env 屏蔽敏感变量。"
        "成功时 output_artifacts 是产出文件的 artifact 引用（跨调用复用数据用它的 "
        "artifact_id 重新声明 inputs）。⚠ 沙箱每次调用都是全新工作目录：上次调用"
        "结果里的任何文件路径在下次调用中必然失效，不要复用路径。"
        # ⚠ This description is **shared by all agents** (financial-/marketing-/business-/compliance-analyst,
        # consultant, kb-analyst…), but **only compute has** the flat `python_exec`. Therefore the legacy shape
        # specification must always remain; replacing it with "use python_exec" directs the other 6 peers to a tool they do not have.
        # Express the preference only as a **conditional statement**, letting the model inspect its own available tools.
        # (This commit already encountered the same pitfall with bi_concept_resolve: descriptions shared across peers must not contain
        #  statements true only for one profile.)
        'python_sandbox 固定用 script="runner.py" + input={"code":"<python>"}；不要省略 input.code。'
        "**若你的工具面里有 `python_exec`（`code` 是顶层参数），跑 Python 优先用它**"
        "——那条路少一层嵌套，长代码不易在流式传输时被截断。"
        "用户明确要求生成可下载文件时，python_sandbox 的 input 传 "
        'purpose="deliverable" + deliverable_outputs=["准确文件名"]；'
        "普通计算/中间数据保持默认 analysis。"
        "不要把 compute.py/dirty_table.py/period_growth.py 传给 script。"
        "代码仍可按 SKILL.md 指引 import dirty_table / period_growth helper。"
    )
    if invoke_command_enabled() and multiruntime_enabled():
        _invoke_props["command"] = {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "（可选，跑上传/组织 skill 的脚本时用）按 `skill_read` 的 SKILL.md 文档化命令传 "
                "argv 数组（**不是 shell 字符串**）。工作目录 = skill 根，脚本路径写相对（如 "
                "'scripts/run_pipeline.py'）。**两条必须**，否则产物拿不到或内容是示例数据："
                "① 用 {inputs.<名>} 引用你在 input.inputs 里声明的工件（会替换成沙箱真实路径）——"
                "**不要照抄 SKILL.md 里指向 examples/ 的示例路径**（会生成示例 deck 不是用户内容）；"
                "② 输出**必须**写到 {output_dir}（→ 工作目录顶层）——**不要**用 SKILL.md 里的 output/ "
                "相对目录（写进 skill 子目录不会被捕获，静默丢失）。"
                '例：["python3","scripts/run_pipeline.py","--spec","{inputs.spec}","--output","{output_dir}/deck.pptx"]。'
                "不传 command → 走向后兼容默认（要求脚本读 --input/--workdir 写 result.json）。"
            ),
        }

    defs = [
        SkillToolDef(
            name="skill_list",
            description=(
                "列出当前用户可用的全部 skill：内置 + 用户/团队自建（仅 name + 简介 + 脚本文件名）。无需参数。system prompt 索引未列出的自建 skill 也在这里返回。"
            ),
            input_schema={"type": "object", "properties": {}},
            handler=_list,
        ),
        SkillToolDef(
            name="skill_read",
            description=(
                "加载指定 skill 的完整 SKILL.md 指引（含 `$ARGUMENTS / $0` 替换）。"
                "当 system prompt 的 skill 索引显示某 skill 可能可用时，调这个拿详细指令再决定是否 invoke。"
                "返回值含 `references` 字段列出可深读的子文档（如 'payload-schema.md'）；"
                "想读子文档传 `ref=<filename>` 再调一次 skill_read."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "args": {"description": "（可选）$ARGUMENTS / $N 替换实参，string 或 array"},
                    "file": {
                        "type": "string",
                        "description": "读取 bundle 内的文本文件，传完整相对路径，如 scripts/nested/run.py、references/rules.json、evals/evals.json；修改脚本前先读取源码。",
                    },
                    "ref": {
                        "type": "string",
                        "description": (
                            "（可选）references/ 下的相对文件路径，支持嵌套与 Markdown/JSON/YAML/TXT 文本；Markdown 可省 .md 后缀。"
                            "传了就只返这个 ref 文件内容，不返主体. "
                            "先不传调一次拿到 references 列表，再决定要读哪个 ref."
                        ),
                    },
                },
                "required": ["name"],
            },
            handler=_read,
        ),
        SkillToolDef(
            name="skill_invoke",
            description=_invoke_desc,
            input_schema={
                "type": "object",
                "properties": _invoke_props,
                "required": ["name"],
            },
            handler=_invoke,
            risk_level="medium",
        ),
    ]
    defs.append(
        SkillToolDef(
            name="python_exec",
            # Only compute has this tool (compute.yaml tools.allow), and compute's permanent prompt ④
            # already covers INPUTS access / producing data instead of charts / statelessness across calls. Do not repeat it here; state only
            # **what is unique to this tool**: its shape (top-level code) and why it exists.
            description=(
                "在隔离沙箱里跑一段 Python（pandas / numpy / scipy / sklearn / statsmodels / matplotlib 已装，无网络）。"
                "**`code` 直接放顶层**，不要再包一层 `input={...}`——长代码塞进嵌套对象里的字符串"
                "容易在流式传输时被截断写坏，这正是本工具存在的理由。"
                "数据走 `inputs`（只写 artifact_id），代码里用 `INPUTS['<key>']['path']` 读。"
                "用户明确要求生成可下载文件时，传 `purpose=deliverable` 并在 "
                "`deliverable_outputs` 精确列出顶层文件名；普通计算保持 analysis。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": "要执行的 Python 源码（顶层字符串，不要再包一层对象）。",
                    },
                    "intent": {
                        "type": "string",
                        "description": (
                            "一句话说明**这次**在算什么，会作为「数据来源」里这条引用的文案直接给用户看，"
                            "所以写给人看、别写代码。例：「各省营业额排名与 z 值」「品类环比变化额/变化率」。"
                            "同一轮里跑多次时**分别写各自算的那件事**，别都写「读取数据」——"
                            "文案相同的两条引用在报告里读起来就是渲重了。"
                        ),
                    },
                    "inputs": {
                        "type": "object",
                        "description": (
                            '要载入的数据，形如 {"rows": {"artifact_id": "art_..."}}。'
                            "artifact_id 来自 `bi_query` 的 `_ref_artifact_id` 或上次沙箱的 "
                            "`output_artifacts[].artifact_id`；文件名宿主侧自动补，不用写。"
                        ),
                    },
                    "timeout_sec": {
                        "type": "integer",
                        "minimum": 5,
                        "maximum": sandbox_max_timeout_s(),
                        "description": "Optional execution timeout in seconds. Omit to use the Team default. Explicit values are clamped to the Team maximum and deployment ceiling. A slow execution and an unavailable executor have distinct failure causes.",
                    },
                    "purpose": {
                        "type": "string",
                        "enum": ["analysis", "deliverable"],
                        "description": "默认 analysis；仅用户明确要求最终可下载文件时用 deliverable。",
                    },
                    "deliverable_outputs": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "purpose=deliverable 时必填：代码实际写出的顶层文件名。",
                    },
                },
                # Declare `intent` required so the model is **prompted every time**; runtime
                # fails open (omission falls back to inference from comments/output files/code lines; see
                # `sources_resolver._sandbox_stated_intent`). Hard rejection would turn occasional omissions
                # into a wasted round trip, unjustified for one label; monitor omission rates through metrics.
                "required": ["code", "intent"],
            },
            handler=_invoke_python_sandbox_flat,
            risk_level="medium",
        )
    )
    return defs
