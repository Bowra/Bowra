"""Effective skill view —— 「用户 U 此刻能用哪些 skill」的唯一读侧投影.

skill-audit 2026-07 统一视图收敛：`resolve_skill` 是单名解析的权威，本模块是它的
**列表版**，契约 = 列表里出现的名字就是执行时真能 resolve 的名字，再叠加 per-user
停用过滤。所有列表型消费面（skill_list 工具、Lead prompt 自建 skill 段、将来的
设置页 / subagent picker）都应消费本视图，不要再各拼一份 catalog/user/team 三段
合并——历史上 4 处各拼各的，已经漂移出「列得到调不动」（team 段不查停用）。

视图**每次现算**（读 store 现状），无物化、无缓存：删 / 改 / 停用 / 撤销分享自动
反映到下一次列举与下一个 turn 的 prompt。store 依旧 duck-typed 自 ctx（不 import
bowhead_core，保持 bowhead_skill 分层）：

    ctx.skill_instance_store.list_for_user(user_id=) / .get(user_id=, name=)
    ctx.team_skill_store.list(team_id, only_enabled=True)
    ctx.granted_team_skill_ids: frozenset[str] | None（turn 预取「分享给我」）
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .loader import SkillSpec, load_skill
from .skill_resolver import _visibility_ok

if TYPE_CHECKING:
    from .catalog import SkillCatalog

logger = logging.getLogger(__name__)

USER_SKILL_SOURCE_TYPES = {"user_upload", "user_generated"}

# Team skill descriptions are free text in the database (historical data may have bypassed the write gate), and view entries
# enter the per-turn prompt sanitization hot path + skill_list. Truncate at the source as a fallback (limit matches
# bowhead_core.skill_upload.MAX_DESC_LEN; no import due to layering). Security review P1.
_MAX_TEAM_DESC_CHARS = 2048


@dataclass(frozen=True)
class EffectiveSkill:
    """视图条目。origin: "catalog"（内置/信任根）| "user"（自建）| "team"（团队共享）."""

    name: str
    description: str
    version: str | None
    origin: str
    scripts: tuple[str, ...] = ()
    source_id: str | None = None
    source_path: Path | None = None


def path_under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def user_skill_dir(user_id: str) -> Path:
    return (Path(".data") / "user_skills" / user_id).resolve()


def is_user_skill_instance(inst: Any | None) -> bool:
    return inst is not None and getattr(inst, "source_type", None) in USER_SKILL_SOURCE_TYPES


def load_user_skill_spec(inst: Any, user_id: str) -> SkillSpec | None:
    if not is_user_skill_instance(inst):
        return None
    source_dir = Path(str(getattr(inst, "source_path", "")))
    if not path_under(source_dir, user_skill_dir(user_id)):
        return None
    try:
        spec = load_skill(source_dir.resolve())
    except Exception:
        return None
    if spec.name != getattr(inst, "name", None):
        return None
    return spec


def effective_description(inst: Any, spec: SkillSpec) -> str:
    return str(getattr(inst, "description", "") or "").strip() or spec.description


async def description_for(ctx: Any, spec: SkillSpec, *, is_catalog: bool = False, strict: bool = False) -> str:
    store, uid = getattr(ctx, "skill_instance_store", None), getattr(ctx, "user_id", None)
    try:
        inst = await store.get(user_id=uid, name=spec.name) if store is not None and uid else None
    except Exception:
        if strict:
            raise
        inst = None
    if inst is not None:
        source = getattr(inst, "source_path", None)
        if (is_catalog and not is_user_skill_instance(inst)) or (
            not is_catalog and is_user_skill_instance(inst) and source and Path(source).resolve() == spec.source_dir.resolve()
        ):
            return effective_description(inst, spec)
    team_store, team_id = getattr(ctx, "team_skill_store", None), getattr(ctx, "team_id", None)
    if not is_catalog and team_store is not None and team_id:
        rec = await team_store.get_by_name(team_id, spec.name, only_enabled=True)
        source = getattr(rec, "source_path", None)
        if source and Path(source).resolve() == spec.source_dir.resolve():
            return effective_description(rec, spec)
    return spec.description


async def user_enabled(ctx: Any, catalog: SkillCatalog, name: str, *, strict: bool = False) -> bool:
    """查 ctx.skill_instance_store，判断当前 user 是否启用此 skill."""
    store = getattr(ctx, "skill_instance_store", None)
    uid = getattr(ctx, "user_id", None)
    if store is None or not uid:
        return True  # No store configured → allow everything (compatible with unit tests / legacy paths).
    try:
        inst = await store.get(user_id=uid, name=name)
        if inst is None:
            if catalog.get(name) is not None:
                return True
            team_store = getattr(ctx, "team_skill_store", None)
            team_id = getattr(ctx, "team_id", None)
            if team_store is None or not team_id:
                return True
            rec = await team_store.get_by_name(team_id, name, only_enabled=False)
            return await team_skill_user_enabled(ctx, rec, strict=strict) if rec is not None else True
        # A stale per-user skill whose name now collides with a global
        # skill must not disable or shadow that global skill.
        if is_user_skill_instance(inst) and catalog.get(name) is not None:
            return True
        return bool(inst.enabled)
    except Exception:
        if strict:
            raise
        return True


def import_pending(inst: Any | None) -> bool:
    """skill-import 批1（tech-design r3 §2）：待处理导入。`effective = enabled ∧ ¬pending`."""
    return is_user_skill_instance(inst) and getattr(inst, "import_status", None) == "pending"


async def user_import_pending(ctx: Any, name: str) -> bool:
    """当前 user 的同名个人 skill 是否处于待处理导入。查不到 store/记录一律 False（不误伤）."""
    store = getattr(ctx, "skill_instance_store", None)
    uid = getattr(ctx, "user_id", None)
    if store is None or not uid:
        return False
    try:
        return import_pending(await store.get(user_id=uid, name=name))
    except Exception:
        return False


async def team_skill_user_enabled(ctx: Any, rec: Any, *, strict: bool = False) -> bool:
    """Read a Team Skill preference by stable ID, with legacy name-key fallback."""

    store = getattr(ctx, "skill_instance_store", None)
    uid = getattr(ctx, "user_id", None)
    if store is None or not uid:
        return True
    try:
        preference = await store.get(
            user_id=uid,
            name=f"team:{getattr(rec, 'id', '')}",
        )
        if preference is None:
            legacy = await store.get(user_id=uid, name=str(getattr(rec, "name", "")))
            preference = legacy if getattr(legacy, "source_type", None) == "team_shared" else None
        return preference is None or bool(preference.enabled)
    except Exception:
        if strict:
            raise
        return True


async def skill_visible(ctx: Any, catalog: SkillCatalog, name: str, *, strict: bool = False) -> bool:
    """main workspace-permission：user_enabled + grants 可见性过滤（catalog/user 条目用）."""
    if not await user_enabled(ctx, catalog, name, strict=strict):
        return False
    try:
        from bowhead_core.skill_access import skill_visible_by_grants  # type: ignore[import-not-found]

        return await skill_visible_by_grants(ctx, name)
    except Exception:
        if strict:
            raise
        return True


async def list_effective_skills(
    ctx: Any,
    catalog: SkillCatalog,
    *,
    include_catalog: bool = True,
    strict: bool = False,
) -> list[EffectiveSkill]:
    """返回当前 ctx（user + team）此刻可用的 skill 列表.

    与 resolve_skill 同优先级：catalog > user > team——重名时低优先级来源不列
    （列出来也 resolve 不到它，列了就是撒谎）。include_catalog=False 只返自建/
    团队条目（Lead prompt 动态段用；内置有独立的静态目录段），shadow 去重仍生效。

    grants 过滤沿 resolve/_list 现状：catalog/user 走 skill_visible（含通用
    grants，shadow 模式恒过），team 走 visibility + 预取分享 id（与 resolve_skill
    逐字一致；通用 grants 的 keying 统一是 enforce 前置项，见 audit #11）。
    """
    out: list[EffectiveSkill] = []
    global_names = set(catalog.list_names())
    if include_catalog:
        for s in catalog.list_specs():
            if not await skill_visible(ctx, catalog, s.name, strict=strict):
                continue
            out.append(
                EffectiveSkill(
                    name=s.name,
                    description=await description_for(ctx, s, is_catalog=True, strict=strict),
                    version=s.version,
                    origin="catalog",
                    source_id="builtin:" + s.name,
                    source_path=getattr(s, "source_dir", None),
                    scripts=tuple(p.relative_to(s.source_dir / "scripts").as_posix() for p in s.scripts),
                )
            )
    seen = set(global_names)
    uid = getattr(ctx, "user_id", None)
    store = getattr(ctx, "skill_instance_store", None)
    if store is not None and uid:
        try:
            instances = await store.list_for_user(user_id=uid)
        except Exception:
            if strict:
                raise
            instances = []
        for inst in instances:
            name = str(getattr(inst, "name", ""))
            if not is_user_skill_instance(inst) or name in seen:
                continue
            # Once resolve_skill finds a user record, it does not fall through to team (load failures/disabled records return None or
            # block resolution). A user record shadows the team name even if broken; otherwise a team skill
            # with that name would be listed but could not resolve: listed but not invocable (review contract edge case).
            seen.add(name)
            # skill-import batch 1: pending imports stay out of the catalog (the Lead block and peer skill_list share this source).
            if import_pending(inst):
                continue
            if not await skill_visible(ctx, catalog, name, strict=strict):
                continue
            spec = load_user_skill_spec(inst, str(uid))
            if spec is None:
                if strict:
                    raise ValueError("skill_listing_incomplete")
                continue
            out.append(
                EffectiveSkill(
                    name=spec.name,
                    description=effective_description(inst, spec),
                    version=spec.version,
                    origin="user",
                    source_id=getattr(inst, "id", None),
                    source_path=spec.source_dir,
                    scripts=tuple(spec.script_names),
                )
            )
    oid = getattr(ctx, "team_id", None)
    ostore = getattr(ctx, "team_skill_store", None)
    granted_skill_ids = getattr(ctx, "granted_team_skill_ids", None)
    if oid and ostore is not None:
        try:
            for rec in await ostore.list(oid, only_enabled=True):
                name = str(getattr(rec, "name", ""))
                visible = _visibility_ok(rec, uid) or bool(granted_skill_ids and getattr(rec, "id", None) in granted_skill_ids)
                if not name or name in seen or not visible:
                    continue
                # audit #17: list/invoke symmetry; per-user disabling filters team skills too.
                if not await team_skill_user_enabled(ctx, rec, strict=strict):
                    continue
                out.append(
                    EffectiveSkill(
                        name=name,
                        description=str(getattr(rec, "description", "") or "")[:_MAX_TEAM_DESC_CHARS],
                        version=None,
                        origin="team",
                        source_id=getattr(rec, "id", None),
                        source_path=Path(rec.source_path) if getattr(rec, "source_path", None) else None,
                    )
                )
                seen.add(name)
        except Exception:
            if strict:
                raise
            logger.warning("effective_skills.team_store_failed team=%s", oid)
    return out
