"""credential-readiness D5：能力联系人——「谁能开启这个能力」的最小查询面。

普通成员在遇到「团队还未配置模型」时没有可执行路径：成员页是 admin-gated，
他们不知道该找谁（PRD 6.0）。本端点按能力返回**有权配置者的 display_name
最小集合**——Disclosure 级 `authenticated`；只回显示名，不回邮箱/角色明细/
完整成员列表（PRD §11 Q7）。名单只供 UI 呈现，绝不注入 LLM prose（自审 F6）。

v1 只有 ``model_config`` 一个能力键（`owner`/`team_admin`，与后端
`_REQUIRE_TEAM_ADMIN` / 前端 `MODEL_ADMIN_ROLES` 同源）。公开入口等
owner-only 资源按需扩键，键控角色集合而非布尔（PRD 6.0.3 ③）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, HTTPException, status

from bowhead_gateway.routes.auth import _current_user, _store

if TYPE_CHECKING:
    from bowhead_core.user_store import User, UserStore

router = APIRouter()

# Capability → authorized team roles (resource-keyed, no global boolean — PRD 6.0.3 ③).
_CAPABILITY_ROLES: dict[str, frozenset[str]] = {
    "model_config": frozenset({"owner", "team_admin"}),
}

_MAX_CONTACTS = 5  # Minimal set: enough to know whom to contact, not a member directory.


@router.get("/capability-contacts")
async def capability_contacts(
    user: User = Depends(_current_user),
    store: UserStore = Depends(_store),
) -> dict[str, Any]:
    """按能力返回可配置者 display_name（登录可见；只回显示名）。"""
    if not user.team_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "no team")
    out: dict[str, list[str]] = {}
    for capability, roles in _CAPABILITY_ROLES.items():
        user_ids = await store.list_team_users_by_roles(
            user.team_id,
            set(roles),
            include_creator=True,
        )
        names: list[str] = []
        for uid in user_ids[:_MAX_CONTACTS]:
            u = await store.get_user(uid)
            display = (getattr(u, "display_name", None) or "").strip() if u else ""
            if not display and u is not None:
                # No display_name: fall back to email local part, avoiding the full email and minimizing information disclosure.
                display = (getattr(u, "email", "") or "").split("@")[0]
            if display:
                names.append(display)
        out[capability] = names
    return {"contacts": out}
