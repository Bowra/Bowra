"""skill_save_generated —— 对话式生成的 skill 直接落盘（Phase 3+ 工具）.

Lead 走 skill-creator SOP 产出 SKILL.md + scripts 后，直接调这个工具保存；
不需要 user 再去 UI 手动上传。实际逻辑跟 `POST /api/v1/skills/generate` 同一
validator + store 路径，只是通过 ToolRegistry 暴露给 LLM.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from bowhead_router.types import ToolSchema
from bowhead_skill.script_langs import entry_extensions

from bowhead_core.skill_import.hook import apply_import_inventory, import_review_summary, sources_from_ctx
from bowhead_core.skill_upload import SkillUploadError, peek_skill_name, validate_and_save_generated
from bowhead_core.tool_registry import ToolRegistry, ToolSpec

logger = logging.getLogger(__name__)


def register_skill_save_tool(tool_registry: ToolRegistry) -> None:
    """注册 skill_save_generated. handler 依赖 ctx.skill_instance_store + ctx.user_id.

    必须在 SkillInstanceStore + skill_catalog 都 wire 进 ctx 后调.
    """

    async def _handler(args: dict[str, Any], ctx: Any) -> Any:
        store = getattr(ctx, "skill_instance_store", None)
        if store is None:
            return {"error": "skill_instance_store not configured on context"}
        uid = getattr(ctx, "user_id", None) or ""
        if not uid:
            return {"error": "no user_id in context"}

        body_md = str(args.get("body_md") or "").strip()
        if not body_md:
            return {"error": "body_md is required (full SKILL.md text with --- frontmatter ---)"}

        # ★ Do not use `or []`: it collapses omitted scripts into an explicit empty list, whose semantics are opposite:
        # overwrite keeps old scripts for None but deletes all for [] (skill_upload distinguishes them).
        # Observed on 116 on 2026-09-02: an agent editing only body_md was collapsed to [], deleting every script,
        # and skill_read does not return script contents, so the agent could not supply them even if it wanted to.
        # Only list / dict count as supplied scripts. **Treat invalid values as omitted**, or
        # a mistyped parameter (`"scripts": "step_1.py"`) effectively commands deletion of every script,
        # an irreversible disk operation. Fail-safe direction: when uncertain, do not delete.
        scripts_arg = args.get("scripts")
        scripts: list[dict[str, str]] | None = None
        if isinstance(scripts_arg, list):
            scripts = []
            for s in scripts_arg:
                if not isinstance(s, dict) or not isinstance(s.get("name"), str) or not isinstance(s.get("content"), str):
                    return {"ok": False, "error": "invalid scripts: each item requires string name and content; existing files were not changed"}
                if isinstance(s, dict) and "name" in s and "content" in s:
                    scripts.append(
                        {
                            "name": str(s["name"]),
                            "content": str(s["content"]),
                        }
                    )
        elif isinstance(scripts_arg, dict):
            scripts = [{"name": str(n), "content": str(c)} for n, c in scripts_arg.items()]

        # Fetch current user usage (same as gateway route; name → size_bytes deducts the old version on overwrite).
        existing = await store.list_for_user(user_id=uid)
        user_skills = {i.name: (i.size_bytes or 0) for i in existing if i.source_type in ("user_upload", "user_generated")}

        name = peek_skill_name(body_md)
        team_store = getattr(ctx, "team_skill_store", None)
        team_id = getattr(ctx, "team_id", None)
        if args.get("overwrite") and name and name not in user_skills and team_store is not None and team_id:
            team_skill = await team_store.get_by_name(team_id, name, only_enabled=True)
            if team_skill is not None:
                return {
                    "ok": False,
                    "error": "team_skill_copy_required",
                    "hint": "当前只有团队版，请在设置 → Skills 中先「复制为我的」，保留完整脚本和参考资料后再修改个人版。",
                }

        turn_id = getattr(ctx, "turn_id", None)
        dest_root = (Path(".data") / "user_skills").resolve()
        catalog = getattr(ctx, "skill_catalog", None)
        reserved_skill_names: set[str] = set()
        if catalog is not None:
            try:
                if callable(getattr(catalog, "list_names", None)):
                    reserved_skill_names = set(catalog.list_names())
                else:
                    reserved_skill_names = {str(s.name) for s in catalog.list_specs()}
            except Exception as e:
                logger.warning("skill_catalog.list_reserved_failed err=%s", e)

        try:
            vs = validate_and_save_generated(
                body_md=body_md,
                scripts=scripts,
                files=args.get("files"),
                user_id=uid,
                dest_root=dest_root,
                existing_user_skills=user_skills,
                source_turn_id=turn_id,
                overwrite=bool(args.get("overwrite")),
                reserved_skill_names=reserved_skill_names,
            )
        except SkillUploadError as e:
            if e.code == "E_SKILL_NAME_EXISTS":
                # Keep actionable hints on the LLM channel (coded fallback lacks instructions, leaving the LLM stuck).
                return {"ok": False, "error": f"validation_failed: {e}", "hint": "同名 skill 已存在；确认要替换旧版就重调本工具并传 overwrite=true."}
            return {"ok": False, "error": f"validation_failed: {e}"}

        # skill-import batch 1: overwrite preserves enabled preference (tech-design r3 §3.4); first save defaults to enabled.
        _prior = next((i for i in existing if i.name == vs.name), None)
        saved_instance = await store.upsert(
            user_id=uid,
            name=vs.name,
            source_type="user_generated",
            source_path=str(vs.source_dir),
            version=vs.version,
            description=vs.description,
            enabled=bool(getattr(_prior, "enabled", True)) if _prior is not None else True,
            size_bytes=vs.size_bytes,
            expected_content_hash=vs.content_hash,
            source_turn_id=turn_id,
            risk_report_json=json.dumps(vs.risk_report, ensure_ascii=False) if vs.risk_report else None,
            reset_verdict=True,
        )

        # Keep global catalog fresh for builtin/system/project roots. Per-user
        # skills are resolved from SkillInstanceStore by skill_list/read/invoke.
        if catalog is not None:
            try:
                catalog.refresh()
            except Exception as e:
                logger.warning("skill_catalog.refresh_failed after save err=%s", e)

        # skill-read-cache-2026-08-28 R1: same-turn read after write. After overwrite, this turn's
        # skill_read cache (ξ B2, ctx._ξ_skill_cache) may retain the old body;
        # neither invalidation above/below (catalog.refresh / Lead prompt block reset) covers it, so without clearing,
        # read-old → overwrite → reread-to-verify returns stale content. Clear the whole dictionary (name-prefix invalidation
        # is not worthwhile: ref/args variant keys also need matching). Subagents use copy.copy(ctx), but the dictionary
        # is a construction-time field shared through aliases, so clear() also affects the parent ctx.
        # Increment __gen__ so reads in flight at clear time **skip cache writeback** (skill_bridge checks before writing);
        # otherwise pre-clear old content reenters the cache upon completion, bypassing clear (reproduced experimentally).
        _read_cache = getattr(ctx, "_ξ_skill_cache", None)
        if isinstance(_read_cache, dict):
            _gen = _read_cache.get("__gen__", 0)
            _read_cache.clear()
            _read_cache["__gen__"] = _gen + 1

        # Phase 1 P0-2 fix (Sprint ω 2026-05-12): invalidate Lead prompt block
        # cache. Without this the rendered skill catalog inside Lead's system
        # prompt stays stale until process restart and the freshly saved skill
        # is invisible to LLM dispatch.
        try:
            from bowhead_core.agents.prompts.lead import (
                reset_skill_catalog_block_cache,
            )

            reset_skill_catalog_block_cache()
        except Exception as e:
            logger.warning("lead_skill_block_cache.reset_failed err=%s", e)

        # skill-import batch 1: rerun inventory on every personal skill disk write (content-based, not caller flags).
        # Inventory failure only reduces reporting; it must never fail the save.
        import_status: str | None = None
        red_count = 0
        inventory_completed = False
        try:
            sources = await sources_from_ctx(ctx, tool_registry)
            inv = await apply_import_inventory(
                store, user_id=uid, name=vs.name, skill_dir=vs.source_dir, sources=sources, expected_content_hash=vs.content_hash
            )
            if inv is not None:
                inventory_completed = True
                red_count = inv.red_count
                refreshed = await store.get(user_id=uid, name=vs.name)
                import_status = getattr(refreshed, "import_status", None)
        except Exception as e:  # noqa: BLE001
            logger.error("skill_import.inventory_failed skill=%s err=%s", vs.name, type(e).__name__)
        from bowhead_core.skill_bundle_lock import skill_bundle_lock
        from bowhead_core.skill_upload import bundle_sha256

        matches_saved, enabled, review = False, None, None
        try:
            with skill_bundle_lock(vs.source_dir):
                refreshed = await store.get(user_id=uid, name=vs.name)
                matches_saved = (
                    refreshed is not None
                    and refreshed.id == saved_instance.id
                    and vs.source_dir.is_dir()
                    and "sha256:" + bundle_sha256(vs.source_dir) == vs.content_hash
                )
                import_status = refreshed.import_status if matches_saved else None
                enabled = refreshed.enabled if matches_saved else None
                review = import_review_summary(refreshed) if matches_saved and inventory_completed else None
        except Exception:
            logger.warning("skill_save.current_inspection_unavailable skill=%s", vs.name)
            import_status = None
        if not matches_saved:
            inventory_completed = False

        if not inventory_completed:
            hint = "The Skill was saved, but its import inspection did not complete. Inspect its current availability before using or sharing it."
        elif import_status == "pending":
            hint = (
                f"Skill `{vs.name}` 已保存，但清点出 {red_count} 条阻断项（引用了不存在的工具/文件等），"
                "状态为「待处理导入」：未生效、不会被列出或调用。请在 /skills/ 查看报告并修正后再保存，或由本人显式「仍然启用」。"
            )
        else:
            hint = f"Skill `{vs.name}` was saved in the conversation-generated group at /skills/. " + (
                "It is enabled; review inspection findings and try it before relying on automatic selection or business results."
                if enabled
                else "It remains disabled; enable it in Settings before use."
            )
        return {
            "ok": True,
            "inventory_status": "complete" if inventory_completed else "not_completed",
            "name": vs.name,
            "description": vs.description,
            "version": vs.version,
            "source_path": str(vs.source_dir),
            "size_bytes": vs.size_bytes,
            "script_count": vs.script_count,
            "warnings": vs.warnings,
            "import_status": import_status,
            "enabled": enabled,
            "import_review": review,
            "inspection_target_matches_saved": matches_saved,
            "trial": "not_run",
            "trigger_verification": "not_run",
            "hint": hint,
        }

    tool_registry.register(
        ToolSpec(
            schema=ToolSchema(
                name="skill_save_generated",
                description=(
                    "保存一份对话生成的 skill 到当前 user 的 user_skills 目录. "
                    "走 Bowhead 官方 validator（frontmatter schema / script AST / 配额）；"
                    "成功后当前 user 可通过 skill_read / skill_list 使用.\n"
                    "输入：`body_md` 是完整 SKILL.md 文本（含 --- frontmatter ---）；"
                    f"`scripts` 是 [{{name, content}}]；name 是 scripts/ 下的相对路径，支持 {', '.join(sorted(entry_extensions()))}.\n"
                    "返回 {ok, name, description, script_count, warnings, hint}. 失败 ok=false + error.\n"
                    '用法：必须先 `skill_read(name="skill-creator")` 读元 SOP，按 5 步完成草稿，'
                    "最后用本工具落盘. 不要每步都调；只在最终稿成形后调一次."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "body_md": {
                            "type": "string",
                            "description": "完整 SKILL.md 文本（含 YAML frontmatter）",
                        },
                        "scripts": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "name": {"type": "string", "description": f"scripts/ 下的相对路径；支持 {', '.join(sorted(entry_extensions()))}"},
                                    "content": {"type": "string", "description": "脚本源码"},
                                },
                                "required": ["name", "content"],
                            },
                            "description": "整目录替换：省略保留原脚本；[] 删除全部脚本；提供列表须包含所有需保留脚本。修改前先 skill_read(file=...) 读取源码。当前 schema 提供 files 时优先逐文件修改。",
                        },
                        "files": {
                            "type": "array",
                            "description": "逐文件补丁：path 为 references/、scripts/ 或 evals/ 下的完整相对路径；content 为新文本，null 明确删除。未列出的文件保留。不可同时用 scripts 和 files 修改脚本。先 skill_read(file=path) 读取原文件。",
                            "items": {
                                "type": "object",
                                "properties": {"path": {"type": "string"}, "content": {"type": ["string", "null"]}},
                                "required": ["path", "content"],
                            },
                        },
                        "overwrite": {
                            "type": "boolean",
                            "description": "同名 skill 已存在时是否覆盖，默认 false",
                        },
                    },
                    "required": ["body_md"],
                },
            ),
            handler=_handler,
            risk_level="medium",  # Disk writes + affects later prompts → not low
        )
    )
