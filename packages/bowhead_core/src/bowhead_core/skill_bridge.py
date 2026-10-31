"""Skill → ToolRegistry 桥接.

bowhead_skill 产出 SkillToolDef (纯数据)；这里把它包装成 ToolSpec 并注册到
bowhead_core.tool_registry.ToolRegistry。

也负责：
    - 构造 SkillRuntime + 默认 SkillCatalog
    - 生成 system prompt 需要注入的 skill index 文本
"""

from __future__ import annotations

import copy
import json
import logging
import os
from functools import wraps
from typing import TYPE_CHECKING, Any

from bowhead_router.types import ToolSchema
from bowhead_skill.runtime_scope import assigned_skill_active, reference_skill_active, reference_skill_operation_denial
from bowhead_skill.tools import build_skill_tools

from bowhead_core.results_artifact_policy import SANDBOX_STDOUT_PRODUCER
from bowhead_core.terminal_effect_contract import TOOL_EFFECT_READ_ONLY
from bowhead_core.tool_registry import ToolRegistry, ToolSpec
from bowhead_skill import SkillCatalog, SkillRuntime, default_catalog

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


from bowhead_core.sandbox_inputs_normalize import (
    FLAT_SANDBOX_TOOL,
    SKILL_INVOKE_TOOL,
    bind_literal_file_refs,
    candidate_prior_filenames,
    describe_bindings,
    normalize_sandbox_tool_args,
)

logger = logging.getLogger(__name__)


# ξ B2: skill_read per-turn cache wrapper
# Production 10-turn audit: analyst-sop-output-contract read 5 times in one turn; LLM failed to converge.
# Cache lifetime = per turn (ctx attr `_ξ_skill_cache`). Hits return the same result with
# `_cached: True` so the LLM sees it was already read and reduces rereads.
# Flag-gated BOWHEAD_SKILL_READ_CACHE=1, default ON; opt-out bypasses wrapper.


def _wrap_skill_read_with_cache(
    handler: Callable[..., Awaitable[Any]],
) -> Callable[..., Awaitable[Any]]:
    async def _cached_handler(args: dict[str, Any], ctx: Any) -> Any:
        if denial := await reference_skill_operation_denial(ctx, "skill_read", args):
            return denial
        if reference_skill_active(ctx) or assigned_skill_active(ctx):
            # A legacy name-keyed body is not an exact reference snapshot.
            return await handler(args, ctx)
        from bowhead_core.ordinary_lead_material import collecting

        if collecting(ctx):
            # A name-only cache has no original source proof. Read through the
            # native handler while collecting a retained execution.
            return await handler(args, ctx)
        if os.environ.get("BOWHEAD_SKILL_READ_CACHE", "1") != "1":
            return await handler(args, ctx)
        skill_name = str(args.get("name") or "")
        ref = str(args.get("ref") or "")
        file = str(args.get("file") or "")
        if not skill_name:
            return await handler(args, ctx)
        # args replace $ARGUMENTS and must enter the key, or changing args within a turn returns old substitutions (audit #16).
        raw_args = args.get("args") or ""
        # ⚠ Key omits role: skill resolution currently depends only on user_id/team_id (stable within a turn). Future
        # role-based skill visibility requires adding role to the key (same constraint at the tool_registry field).
        cache_key = f"{skill_name}|{ref}|{file}|{raw_args!r}"
        # Cache is a **construction-time field** of ToolExecutionContext (default_factory=dict), so peer dispatch's
        # copy.copy(ctx) aliases the same dict, propagating copied-context writes to the parent. The lazy setattr
        # fallback below serves only non-dataclass contexts (test stubs / old paths), which
        # do not use copy.copy dispatch. Concurrent siblings missing the same skill both read/write; harmless, so no lock
        # (same known cost as BIOutageLatch: concurrent siblings in one batch can bypass it).
        cache = getattr(ctx, "_ξ_skill_cache", None)
        if cache is None:
            cache = {}
            try:
                ctx._ξ_skill_cache = cache
            except Exception:
                cache = None  # ctx does not support setattr (e.g., test stub)
        if cache is not None and cache_key in cache:
            try:
                from bowhead_core import metrics as _m

                _m.inc(
                    "bowhead_skill_read_cache_hit_total",
                    labels={"skill": skill_name},
                    help="ξ B2: skill_read per-turn cache hit.",
                )
            except Exception:  # noqa: S110
                pass
            cached_result = cache[cache_key]
            # Annotate so LLM sees it was cached + don't re-read.
            if isinstance(cached_result, dict):
                return {
                    **cached_result,
                    "_cached": True,
                    "_cache_hint": ("本 turn 已读过同 skill, 直接返 cached. 不要再调 skill_read 同 name+ref+args — 写答案."),
                }
            return cached_result
        # Generation snapshot (additional review, 2026-08-28): during a read, this turn may run
        # skill_save_generated overwrite; save clears cache and increments __gen__. If this read resolved old spec
        # **before** clear but completes **after** it, direct writeback restores
        # pre-clear content into invalidated cache (reproducible: resolve→suspend→save→complete).
        # Compare generations before writeback; discard changed-generation writes (return result normally, just do not cache).
        # __gen__ cannot collide with real keys: they are always f"{name}|{ref}|{args!r}", containing "|".
        gen_before = cache.get("__gen__", 0) if cache is not None else 0
        result = await handler(args, ctx)
        # Only cache successful reads (ok=True or schema-shape result).
        if cache is not None and isinstance(result, dict) and cache.get("__gen__", 0) == gen_before:
            # SkillRuntime.read returns {ok: True, content: ...} or similar
            if result.get("ok", True) and "error" not in result:
                cache[cache_key] = result
        try:
            from bowhead_core import metrics as _m

            _m.inc(
                "bowhead_skill_read_cache_miss_total",
                labels={"skill": skill_name},
                help="ξ B2: skill_read per-turn cache miss (real read).",
            )
        except Exception:  # noqa: S110
            pass
        return result

    return _cached_handler


def _wrap_sandbox_inputs_normalization(tool_name: str, handler: Any) -> Any:
    """把「意图无歧义、只是形状不合」的 `inputs` 就地纠正后再交给原 handler。

    纠正不了的一律原样放行 —— 让下游响亮报错，不瞎猜。猜错会让模型拿到别人的数据，
    比失败更糟。每次纠正都打 metric，事后才判得了该收紧还是该继续宽容。
    """

    async def _wrapped(args: dict[str, Any], ctx: Any) -> Any:
        try:
            args = normalize_sandbox_tool_args(tool_name, args, ctx)
        except Exception:  # noqa: BLE001 — normalization is best-effort; never break valid calls
            logger.warning("sandbox_inputs.normalize_failed tool=%s", tool_name, exc_info=True)
        bound: list[dict[str, str]] = []
        try:
            args, bound = await _bind_literal_file_refs(tool_name, args, ctx)
        except Exception:  # noqa: BLE001 — likewise: failed binding passes through unchanged; downstream reports errors normally
            logger.warning("sandbox_inputs.literal_bind_failed tool=%s", tool_name, exc_info=True)
        result = await handler(args, ctx)
        if bound and isinstance(result, dict):
            result = {**result, "input_autobind_note": describe_bindings(bound)}
        return result

    return _wrapped


async def _bind_literal_file_refs(tool_name: str, args: Any, ctx: Any) -> tuple[Any, list[dict[str, str]]]:
    """代码里写死的 `art_xxx` 路径 / 本 turn 上次沙箱运行写出的文件名 → 绑定成输入。

    文件名要查库（只查本 turn 的 artifact），所以放在 async wrapper 里，不进同步的归一。
    """
    prior: dict[str, str] = {}
    names = candidate_prior_filenames(tool_name, args)
    store = getattr(ctx, "artifact_store", None)
    finder = getattr(store, "find_turn_artifact_ids_by_filename", None)
    turn_id = str(getattr(ctx, "turn_id", "") or "")
    if names and finder is not None and turn_id:
        prior = await finder(turn_id, names)
    return bind_literal_file_refs(tool_name, args, prior)


def _wrap_skill_execution_gate(tool_name: str, handler: Any, catalog: SkillCatalog) -> Any:
    """Re-check mutable Skill state immediately before read/invoke.

    This wrapper must sit outside ``skill_read``'s cache so a mid-turn disable
    cannot return a previously cached body.  For sandbox entry points the
    normalization wrapper remains outermost; normalization is local and has no
    side effect, while this gate still runs before any Skill materialization or
    process spawn.
    """

    async def _wrapped(args: dict[str, Any], ctx: Any) -> Any:
        from bowhead_skill.runtime_scope import skill_operation_denial

        if assigned_skill_active(ctx) or reference_skill_active(ctx):
            from bowhead_core.chat_references import _bounded_json

            try:
                _bounded_json(args, max_depth=20, max_bytes=1024 * 1024)
                args = json.loads(json.dumps(args, ensure_ascii=False, allow_nan=False))
                ctx = copy.copy(ctx)
            except Exception:
                return {
                    "ok": False,
                    "error_kind": "invalid_args",
                    "error": "The Skill call arguments must be plain JSON of at most 1 MB and 20 levels. Resend smaller JSON arguments.",
                }
        if denial := skill_operation_denial(ctx, tool_name, args):
            return denial
        if denial := await reference_skill_operation_denial(ctx, tool_name, args):
            return denial
        if reference_skill_active(ctx):
            if tool_name in {"skill_invoke", "python_exec"}:
                execute = getattr(ctx.reference_skill_access, "execute_tool", None)
                if execute is None:
                    return {"ok": False, "error_kind": "reference_skill_unavailable", "error": "The exact Skill invocation context is unavailable."}
                return await execute(tool_name, handler, args, ctx)
            return await handler(args, ctx)
        if assigned_skill_active(ctx):
            from bowhead_core.assigned_skill_execution import execute_assigned_skill

            return await execute_assigned_skill(handler, args, ctx, catalog=catalog, tool_name=tool_name)
        if tool_name == FLAT_SANDBOX_TOOL:
            name = "python_sandbox"
        else:
            name = str(args.get("name") or "").strip()
        if name:
            from bowhead_core.resource_execution_gate import check_skill_execution

            permission = "read" if tool_name == "skill_read" else "invoke"
            denial = await check_skill_execution(
                ctx,
                catalog=catalog,
                skill_name=name,
                permission=permission,
            )
            if denial is not None:
                from bowhead_core.ordinary_lead_material import note_known_rejection

                note_known_rejection(ctx, tool_name, args, denial)
                # A pending import or revocation must also invalidate bodies from
                # before that state change. Keep the shared dict so sibling peers
                # see the invalidation, and fence reads already in flight.
                cache = getattr(ctx, "_ξ_skill_cache", None)
                if isinstance(cache, dict):
                    generation = cache.get("__gen__", 0)
                    cache.clear()
                    cache["__gen__"] = generation + 1
                return denial
        return await handler(args, ctx)

    return _wrapped


async def _mark_sandbox_stdout_internal(result: Any, ctx: Any) -> None:
    """把本次沙箱调用产出的 stdout artifact 标成「结果的内部依赖」。

    判据是 runner 写进 `result_json.outputs[]` 的 ``data_role``，**不是文件名**：
    用户自己也可以在 workdir 里写一个叫 `sandbox-stdout.txt` 的文件，真正的 stdout
    记录最后追加、会被 runner 改名成 `sandbox-stdout-data-N.txt`
    （与 `answer_decomposer._sandbox_output_artifact_id` 同一条判据，别改成按名字判）。
    """
    if not isinstance(result, dict):
        return
    items = result.get("output_artifacts")
    if not isinstance(items, list) or not items:
        return
    mark = getattr(getattr(ctx, "artifact_store", None), "mark_internal_producer", None)
    if mark is None:
        return
    for item in items:
        if not isinstance(item, dict) or str(item.get("data_role") or "") != "stdout":
            continue
        artifact_id = str(item.get("artifact_id") or "")
        if artifact_id:
            await mark(artifact_id, SANDBOX_STDOUT_PRODUCER)


def _wrap_sandbox_stdout_marking(handler: Any) -> Any:
    """Mark persisted sandbox stdout as an internal dependency, hiding it from deliverables.

    Keep the complete stdout artifact for provenance; inline stdout is only a bounded
    model preview. The server learns ``data_role`` from the runner result, so marking
    it here avoids changing the image-baked runner and bumping ``SANDBOX_VERSION``.
    Keep this wrapper in Core because bowhead_skill deliberately does not import Core.
    Marking failures must never fail an otherwise successful sandbox call.
    """

    async def _wrapped(args: dict[str, Any], ctx: Any) -> Any:
        result = await handler(args, ctx)
        try:
            await _mark_sandbox_stdout_internal(result, ctx)
        except Exception:  # noqa: BLE001 — marking is best-effort; never break successful calls
            logger.warning("sandbox_stdout.mark_internal_failed", exc_info=True)
        return result

    return _wrapped


def register_skill_tools(
    registry: ToolRegistry,
    catalog: SkillCatalog,
    runtime: SkillRuntime,
) -> list[str]:
    """Register Skill handlers and keep current authorization at dispatch."""

    async def authorize_invoke(skill_name: str, ctx: Any) -> dict[str, Any] | None:
        if assigned_skill_active(ctx) or reference_skill_active(ctx):
            # Exact sources use the existing call-scoped material authorization.
            return None
        from bowhead_core.resource_execution_gate import check_skill_execution

        return await check_skill_execution(ctx, catalog=catalog, skill_name=skill_name, permission="invoke")

    from bowhead_core.ordinary_lead_material import OrdinarySkillObserver

    observer = OrdinarySkillObserver(catalog)
    defs = build_skill_tools(catalog, runtime, authorize_invoke=authorize_invoke, material_observer=observer)

    def record_material(tool_name, handler):
        @wraps(handler)
        async def observed(args, ctx):
            result = await handler(args, ctx)
            await observer.finished(tool_name, args, result, ctx)
            return result

        return observed

    def record_configuration_read(handler: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        async def read(args: dict[str, Any], ctx: Any) -> Any:
            result = await handler(args, ctx)
            from bowhead_core.configuration_agent import note_configuration_skill_read

            note_configuration_skill_read(ctx, args, result)
            return result

        return read

    def record_presentation(tool_name, handler):
        async def invoke(args, ctx):
            from bowhead_core.artifact_presentation import normalize_producer_declaration

            result = await handler(args, ctx)
            try:
                return await normalize_producer_declaration(tool_name, args, result, ctx)
            except Exception:
                # Generation has finished. Preserve its files and outcome;
                # uncertainty in presentation must never invite regeneration.
                logger.warning("artifact_presentation.producer_verification_failed tool=%s", tool_name, exc_info=True)
                if isinstance(result, dict):
                    return {**result, "artifact_presentation_error": "artifact_presentation_verification_unavailable"}
                return result

        return invoke

    registered: list[str] = []
    for d in defs:
        handler = d.handler
        # ξ B2: wrap skill_read with per-turn cache (5 rereads/SOP in 10 production turns)
        if d.name == "skill_read":
            handler = _wrap_skill_read_with_cache(handler)
            handler = _wrap_skill_execution_gate(d.name, handler, catalog)
            handler = record_configuration_read(handler)
        # sandbox-input-contract L2: normalize `inputs` at sandbox entry first.
        # Wrap in core (resolution needs `ctx._ν15_allowlist`; bowhead_skill deliberately
        # does not import bowhead_core, and runner is a separate process without ctx).
        if d.name in (FLAT_SANDBOX_TOOL, SKILL_INVOKE_TOOL):
            handler = _wrap_skill_execution_gate(d.name, handler, catalog)
            # Mark sandbox stdout as an internal dependency (only if results actually have data_role=stdout,
            # so attaching to skill_invoke does not affect other skills).
            #
            # Normalize before authorization/materialization; mark stdout before
            # passive observation hashes the final result. The observer preserves
            # wrapper metadata and never changes arguments or returned values.
            handler = _wrap_sandbox_stdout_marking(handler)
            handler = _wrap_sandbox_inputs_normalization(d.name, handler)
            handler = record_presentation(d.name, handler)
        if d.name in {"skill_read", "skill_list", FLAT_SANDBOX_TOOL, SKILL_INVOKE_TOOL}:
            handler = record_material(d.name, handler)
        registry.register(
            ToolSpec(
                schema=ToolSchema(
                    name=d.name,
                    description=d.description,
                    input_schema=d.input_schema,
                ),
                handler=handler,
                risk_level=d.risk_level,
                effect_spec=(TOOL_EFFECT_READ_ONLY if d.name in {"skill_list", "skill_read"} else None),
            )
        )
        registered.append(d.name)
    logger.info("skill_bridge.registered tools=%s (skill_read cache wrapped)", registered)
    return registered


def build_default_skill_stack() -> tuple[SkillCatalog, SkillRuntime]:
    """供 gateway main.py 使用的缺省工厂：catalog 只扫 builtin + system-user + project 信任根.

    **多租户隔离（workflow-solidify §5.1 + main workspace-permission）**: user/team 上传或
    生成的 skill（`.data/user_skills/<uid>/`、`.data/team_skills/<team_id>/`）**不进全局
    catalog**——name-keyed 全局 dict 不隔离租户（跨 user/team 同名 last-wins + shadow
    builtin → 跨租户 list/read + RCE）. 二者都改走 per-request 解析：user-skill 经
    ``SkillInstanceStore``、team-skill 经 ``resolve_skill``（按 (owner, name) 查 store +
    load）. 见 skill_resolver.py / tools.py / routes/skills.py. 保存路径（upload/generate/
    skill_save_tool）经 store.upsert 落 DB，resolve 据此找回；catalog.refresh 对这些变防御性 no-op.
    """
    catalog = default_catalog()
    catalog.refresh()
    runtime = SkillRuntime()
    return catalog, runtime


__all__ = [
    "build_default_skill_stack",
    "register_skill_tools",
]
