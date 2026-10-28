"""Content-sharing domain logic (user-initiated shares).

Behavior spec: docs/sprints/content-permissions/prd.md (r4); implementation
spec: docs/sprints/content-permissions/tech-design.md §2. This module stays
below the gateway layer (pure logic over ``AccessControlStore`` /
``UserStore`` duck types) — HTTP auth, request parsing, and FastAPI
exceptions live in ``bowhead_gateway.routes.shares``.

Wave-1 model in one paragraph: an owner shares team_subagent / team_skill /
report at a single per-kind permission (use/use/read). Audience is a
three-state spectrum (private / custom / team_wide) realized as resource_grants
rows — team_wide writes through to an team-principal allow grant (and, for
capability assets, the row's ``visibility`` column) inside ONE transaction.
Visibility checkpoints use ``is_visible`` with a per-request/per-turn
prefetched ``granted_ids`` set; deny rows win inside the prefetch itself.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from bowhead_core.resource_access import SHAREABLE_KINDS
from bowhead_core.subagent_write_boundary import (
    reject_obsolete_team_subagent_write,
)

if TYPE_CHECKING:
    import aiosqlite

    from bowhead_core.access_control import AccessControlStore, ResourceGrantRecord

_CONTENT_SHARING_ENV = "BOWHEAD_CONTENT_SHARING"
_content_sharing_cache: bool | None = None

AUDIENCES = ("private", "custom", "team_wide")

# kind -> visibility write-through table (None = no visibility column, e.g. report)
_VISIBILITY_TABLE_BY_KIND: dict[str, str | None] = {
    "team_subagent": "team_subagent",
    "team_skill": "team_skill",
    "report": None,
}


class SkillApprovalRequiredError(ValueError):
    """skill-team-governance：未过审批的团队 skill 不得广播/点名分享（PRD §3.1 三扇门）。

    ``set_audience``（target=team_wide）与 ``add_grant``（点名）对 kind=team_skill 重查
    approval，非 approved → raise 本异常；路由层转 422 ``E_SKILL_APPROVAL_REQUIRED``。"""

    code = "E_SKILL_APPROVAL_REQUIRED"


async def ensure_team_skill_grantable(user_store: Any, *, team_id: str, resource_id: str) -> None:
    """pending/rejected 的团队 skill 分享闸（服务层单一检查点）.

    shares 路由 / access-requests approve / admin grants 控制台三条写路共用。
    fail-closed：行缺失同样拒（防 id 猜测建 grant 孤儿行）。"""
    sk = await user_store.get_team_skill(team_id, resource_id)
    if sk is None or getattr(sk, "approval", "approved") != "approved":
        raise SkillApprovalRequiredError(f"team_skill not approved: {resource_id}")


def content_sharing_enabled() -> bool:
    """Feature switch (default ON; set BOWHEAD_CONTENT_SHARING=0 to disable).

    Gates the share API mount and the grant overlays on read paths. It NEVER
    gates the B1/B2 security fixes (tech-design §4 flag table) — those are
    unconditional. Cached per process (env is immutable at runtime in prod).
    """
    global _content_sharing_cache  # noqa: PLW0603 — process-level flag cache (same pattern as auth._auto_member_domains)
    if _content_sharing_cache is None:
        _content_sharing_cache = os.environ.get(_CONTENT_SHARING_ENV, "1").strip() != "0"
    return _content_sharing_cache


def reset_content_sharing_cache() -> None:
    """Test hook — clear the process-level flag cache."""
    global _content_sharing_cache  # noqa: PLW0603 — test hook
    _content_sharing_cache = None


async def can_manage_shares(user_store: Any, user: Any, owner_id: str | None) -> bool:
    """PRD §4.5 (wave 1, no re-share): owner OR team creator OR team_admin.

    SP-AUTH-3: 团队角色只按当前团队判 (get_team_role 对 team creator 返回
    "owner"); 不再用无 team_id 的 is_team_admin() 按主队回退.
    """
    if owner_id and getattr(user, "id", None) == owner_id:
        return True
    role = await user_store.get_team_role(user.id, user.team_id)
    return bool(role in {"owner", "team_admin"})


@dataclass(frozen=True)
class ResolvedShareable:
    """A shareable resource resolved to (owner, label) within an team. wave-1 shares
    and wave-2 access-requests both resolve to this. Capability assets carry the
    write-through visibility cache; report has none."""

    kind: str
    resource_id: str
    owner_id: str | None
    label: str
    visibility: str | None
    workspace_id: str = ""


async def resolve_shareable(
    *,
    kind: str,
    resource_id: str,
    team_id: str,
    user_store: Any,
    canvas_store: Any | None,
    subagent_enabled: bool,
    workflow_enabled: bool,
) -> ResolvedShareable | None:
    """Existence + owner + team-containment resolution, **non-raising, request-free**.

    Returns None for: unsupported/empty kind, dark capability flag, missing resource,
    or a report whose workspace isn't in ``team_id`` (cross-team → anti-enumeration).
    The caller-dependent 409-vs-404 nuance for the share API stays in the
    ``routes/shares.py`` wrapper (it holds the request ``user``); access-requests
    map None → silent no-op. report-only this wave; subagent/skill kept generic.
    """
    kind = (kind or "").strip()
    rid = (resource_id or "").strip()
    if kind not in SHAREABLE_KINDS or not rid:
        return None
    if kind == "team_subagent":
        if not subagent_enabled:
            return None
        sa = await user_store.get_team_subagent(team_id, rid)
        if sa is None:
            return None
        return ResolvedShareable(kind, rid, sa.created_by, sa.display_name or sa.name, sa.visibility)
    if kind == "team_skill":
        if not workflow_enabled:
            return None
        sk = await user_store.get_team_skill(team_id, rid)
        if sk is None:
            return None
        return ResolvedShareable(kind, rid, sk.created_by, sk.display_name or sk.name, sk.visibility)
    # report: rid = session_id; canvas is the resource (1:1, canvas_store UNIQUE(session_id))
    if canvas_store is None:
        return None
    canvas = await canvas_store.get_canvas_by_session(rid)
    if canvas is None:
        return None
    if not await user_store.workspace_in_team(team_id, canvas.workspace_id or ""):
        return None
    workspace_id = canvas.workspace_id or ""
    resolve_workspace = getattr(
        user_store,
        "resolve_workspace_id_for_team",
        None,
    )
    if callable(resolve_workspace):
        workspace_id = await resolve_workspace(team_id, workspace_id) or workspace_id
    return ResolvedShareable(
        kind,
        rid,
        canvas.user_id,
        canvas.title or rid,
        None,
        workspace_id,
    )


def is_visible(
    *,
    owner_id: str | None,
    visibility: str | None,
    resource_id: str,
    viewer_id: str,
    granted_ids: frozenset[str],
) -> bool:
    """Unified visibility formula (tech-design §2.1), pure and synchronous.

    owner OR visibility=="team_wide" (legacy fast path — write-through keeps it
    equivalent to the team grant) OR allow-grant. Callers fetch ``granted_ids``
    once per request/turn via ``grants_for_viewer`` and reuse it.
    """
    if owner_id and viewer_id == owner_id:
        return True
    if (visibility or "") == "team_wide":
        return True
    return resource_id in granted_ids


async def grants_for_viewer(
    access_store: AccessControlStore,
    *,
    team_id: str,
    user_id: str,
    kind: str,
    workspace_id: str = "",
    connection: aiosqlite.Connection | None = None,
) -> frozenset[str]:
    """Prefetch the viewer's granted resource-id set for one kind (one SQL +
    one group lookup). Returns ∅ when the sharing feature is off, so callers
    degrade to pure visibility filtering (fail-safe: shares stop applying)."""
    if not content_sharing_enabled():
        return frozenset()
    # Preserve the legacy duck-typed calls when no transaction is supplied.
    read_options = {"connection": connection} if connection is not None else {}
    group_ids = await access_store.principal_group_ids(team_id, user_id, workspace_id, **read_options)
    return await access_store.granted_resource_ids(
        team_id,
        user_id=user_id,
        group_ids=group_ids,
        kind=kind,
        **read_options,
    )


async def set_audience(
    access_store: AccessControlStore,
    *,
    kind: str,
    resource_id: str,
    team_id: str,
    target: str,
    actor_id: str | None,
    user_store: Any | None = None,
) -> dict[str, Any]:
    """Audience transition per the §2.3 matrix — atomic via ``set_audience_tx``.

    - → private: revoke ALL active allow grants (deny rows survive)
    - → custom: revoke the team grant only; user/group grants are the roster
    - → team_wide: upsert the team grant; existing user/group grants are KEPT
      (they become the roster again when shrinking back to custom)

    skill-team-governance（闸下沉，tech-design §0）：kind=team_skill ∧ target=team_wide
    时重查行态，须**已是 team_wide+approved**（写穿只服务已批广播态；visibility 转移
    必须走审批状态机）——put_audience / create 路 / PATCH / admin grants 控制台四调用点
    一次闭合；合法流程先翻行再调本函数，天然通过。fail-closed：该腿未传
    ``user_store``（新调用点忘传）同样 raise，不留旁路。
    """
    if kind not in SHAREABLE_KINDS:
        raise ValueError(f"kind not shareable: {kind}")
    if target not in AUDIENCES:
        raise ValueError(f"invalid audience: {target}")
    reject_obsolete_team_subagent_write(kind)
    if kind == "team_skill" and target == "team_wide":
        # Stronger than the approval check: the row must **already be** team_wide+approved; set_audience only writes grants through
        # for an approved broadcast state, never transitions visibility. Otherwise a creator_only row (always approved
        # for personal use) could broadcast through put_audience without approval (P2-12's no-unguarded-write rule, applied to grants).
        # Valid callers (direct approval on create / PATCH / approve) update the row first, then call here, so they pass naturally.
        if user_store is None:
            raise SkillApprovalRequiredError("user_store required to publish a team_skill team_wide")
        sk = await user_store.get_team_skill(team_id, resource_id)
        if sk is None or getattr(sk, "approval", "approved") != "approved" or getattr(sk, "visibility", "") != "team_wide":
            raise SkillApprovalRequiredError(f"team_skill not approved for broadcast: {resource_id}")
    return await access_store.set_audience_tx(
        team_id=team_id,
        resource_kind=kind,
        resource_id=resource_id,
        target=target,
        permission=SHAREABLE_KINDS[kind],
        created_by=actor_id,
        visibility_table=_VISIBILITY_TABLE_BY_KIND.get(kind),
    )


async def add_grant(
    access_store: AccessControlStore,
    *,
    kind: str,
    resource_id: str,
    team_id: str,
    principal_type: str,
    principal_id: str,
    actor_id: str | None,
    user_store: Any | None = None,
    expires_at: str | None = None,
) -> ResourceGrantRecord:
    """Direct share to one user/group at the kind's fixed permission.

    ``expires_at`` (access-request expansion, D3) lets an approver grant time-limited
    access; None keeps the permanent default.

    team principals are rejected — team_wide goes through ``set_audience`` only,
    so the visibility write-through can never be bypassed (tech-design §3).

    skill-team-governance：kind=team_skill 时点名分享闸下沉在此（shares 路由 +
    access-requests approve 两调用点闭合；admin 控制台直调 store 的路在
    ``routes/access_control.py`` 显式调 ``ensure_team_skill_grantable``）。
    fail-closed：未传 ``user_store`` 同样 raise。
    """
    if kind not in SHAREABLE_KINDS:
        raise ValueError(f"kind not shareable: {kind}")
    reject_obsolete_team_subagent_write(kind)
    if principal_type not in ("user", "group"):
        raise ValueError("principal_type must be 'user' or 'group'")
    if kind == "team_skill":
        if user_store is None:
            raise SkillApprovalRequiredError("user_store required to share a team_skill")
        await ensure_team_skill_grantable(user_store, team_id=team_id, resource_id=resource_id)
    return await access_store.create_or_update_resource_grant(
        team_id=team_id,
        resource_kind=kind,
        resource_id=resource_id,
        principal_type=principal_type,
        principal_id=principal_id,
        permission=SHAREABLE_KINDS[kind],
        effect="allow",
        created_by=actor_id,
        expires_at=expires_at,
    )


async def remove_grant(
    access_store: AccessControlStore,
    *,
    kind: str,
    team_id: str,
    grant_id: str,
) -> bool:
    reject_obsolete_team_subagent_write(kind)
    return bool(await access_store.revoke_resource_grant(team_id, grant_id))


def derive_audience(grants: list[Any], *, team_id: str) -> str:
    """Audience of a grants-only kind (report — PRD §4.3: no visibility column,
    grants ARE the single source of truth). Pass the resource's ACTIVE grants."""
    direct = False
    for g in grants:
        if getattr(g, "effect", "allow") != "allow":
            continue
        ptype = getattr(g, "principal_type", "")
        if ptype == "team" and getattr(g, "principal_id", "") == team_id:
            return "team_wide"
        if ptype in ("user", "group"):
            direct = True
    return "custom" if direct else "private"
