"""Per-request skill 解析 layer（workflow-solidify S0 / tech-design §1）.

替代裸 `catalog.get`：**信任根（catalog）优先 → ctx-scoped user/team 上传 skill**。
结构性多租户隔离——只 load 该 ctx (owner) 的 skill，跨租户 spec 根本不可达：
  - shadow 不可能：catalog.get 先命中 builtin，user/team skill 只在 miss 后按 owner 查；
  - 碰撞不可能：user/team skill 按 (owner, name) 查 store，不同 owner 各自独立；
  - else = fail-CLOSED（未知来源不返）。

`store` 参数 **duck-typed**（不 import bowhead_core，保持 bowhead_skill 分层）：
  user_skill_store.get(user_id=, name=)            -> rec(.source_type, .source_path) | None
  team_skill_store.get_by_name(team_id, name, only_enabled=True)
                                                   -> rec(.source_path, .visibility, .created_by) | None

调用方各自传 store（agent 读点从 ctx；HTTP 路由从 app.state）—— 见 tools.py / routes/skills.py。
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from .loader import SkillSpec, load_skill

if TYPE_CHECKING:
    from .catalog import SkillCatalog

# skill_instance_store also stores per-user disable overrides for built-ins (source_type='builtin');
# resolve considers only actual uploaded/generated user skills; the trusted-root catalog handles built-ins (including disable records).
_USER_SOURCE_TYPES = ("user_upload", "user_generated")


def _safe_load(source_path: str) -> SkillSpec | None:
    """坏 record（路径不存在/SKILL.md 损坏）不该让整个解析崩——降级 None=不可见."""
    try:
        return load_skill(Path(source_path))
    except Exception:
        return None


def _visibility_ok(rec: Any, user_id: str | None) -> bool:
    """team_skill 可见性：team_wide → 任意本 team 成员；creator_only → 仅 created_by."""
    vis = getattr(rec, "visibility", None) or "team_wide"
    if vis == "team_wide":
        return True
    return bool(user_id) and getattr(rec, "created_by", None) == user_id


async def resolve_skill(
    name: str,
    *,
    catalog: SkillCatalog,
    user_id: str | None = None,
    team_id: str | None = None,
    user_skill_store: Any = None,
    team_skill_store: Any = None,
    granted_team_skill_ids: frozenset[str] | None = None,
) -> SkillSpec | None:
    """按名解析一个 skill spec，做 per-request 多租户隔离。None = 该 ctx 不可见/不存在."""
    # 1) Trusted roots (builtin / ~/.bowhead/skills / ./.bowhead/skills): the catalog no longer includes the .data
    #    upload root (§5.1), only trusted roots. Built-ins match here first, making shadowing structurally impossible.
    spec = catalog.get(name)
    if spec is not None:
        return spec

    # 2) user_skill: only the current user's .data/user_skills/<uid>/<name>/ (owner-scoped lookup provides isolation).
    if user_id and user_skill_store is not None:
        rec = await user_skill_store.get(user_id=user_id, name=name)
        if rec is not None and getattr(rec, "source_type", None) in _USER_SOURCE_TYPES:
            return _safe_load(rec.source_path)

    # 3) team_skill: only the current team's .data/team_skills/<team_id>/<name>/ + visibility.
    #    content-permissions S4: shared creator_only skills also resolve (their oskill_* ID is in the prefetched set);
    #    no injected set → use visibility alone (fail-safe: sharing does not take effect, without exceeding permissions).
    if team_id and team_skill_store is not None:
        rec = await team_skill_store.get_by_name(team_id, name, only_enabled=True)
        if rec is not None and (_visibility_ok(rec, user_id) or (granted_team_skill_ids and getattr(rec, "id", None) in granted_team_skill_ids)):
            return _safe_load(rec.source_path)

    # 4) Otherwise fail CLOSED (do not return unknown sources).
    return None
