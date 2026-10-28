"""credential-readiness R9：kb 三项能力状态的成员可读最小查询面。

`/knowledge` 页对全体成员可见，但 `/api/v1/team-onboarding/status` 是
admin-gated（`_require_team_admin_dep`）——成员在上传现场看不到「视觉解析
未开启，扫描件不会被解析」这类降级警示（PRD §6.4）。本端点只回三个 kb 键
的四态枚举（与 R8 `optional_capabilities` 同一计算，`_kb_capability_states`），
不回任何 provider / 模型 / 密钥信息——Disclosure 级 `authenticated`。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from bowhead_core.team_llm_config import is_v2_config, migrate_legacy_config_to_v2
from fastapi import APIRouter, Depends, HTTPException, Request, status

from bowhead_gateway.routes.auth import _current_user, _store
from bowhead_gateway.team_onboarding import _kb_capability_states

if TYPE_CHECKING:
    from bowhead_core.user_store import User, UserStore

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("/kb-ingestion-readiness")
async def kb_ingestion_readiness(
    request: Request,
    user: User = Depends(_current_user),
    store: UserStore = Depends(_store),
) -> dict[str, str]:
    """Report whether this team can index new knowledge files without disclosing model configuration."""
    if not user.team_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "no team")
    try:
        config, version = await store.get_team_llm_config_with_version(user.team_id)
        if not config:
            return {"state": "missing"}
        kb = config.get("kb") if is_v2_config(config) else None
        models = kb.get("models") if isinstance(kb, dict) else None
        active = kb.get("active") if isinstance(kb, dict) else None
        if not isinstance(models, dict) or not isinstance(active, dict) or not active.get("embed") or active["embed"] not in models:
            return {"state": "missing"}
        registry = getattr(request.app.state, "team_kb_runtime_registry", None)
        runtime = await registry.get(user.team_id, config, version) if registry is not None else None
        return {"state": "ready" if runtime is not None and runtime.worker is not None else "unavailable"}
    except Exception:
        logger.warning("kb.ingestion_readiness_failed team=%s", user.team_id, exc_info=True)
        return {"state": "unavailable"}


@router.get("/kb-capabilities")
async def kb_capabilities(
    request: Request,
    user: User = Depends(_current_user),
    store: UserStore = Depends(_store),
) -> dict[str, Any]:
    """当前团队 kb 重排/视觉/行提取的生效状态（登录可见；只回四态枚举）。"""
    if not user.team_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "no team")
    raw_config, _version = await store.get_team_llm_config_with_version(user.team_id)
    if not raw_config:
        return {
            "capabilities": {
                "kb_rerank": "disabled",
                "kb_vision": "disabled",
                "kb_row_extractor": "disabled",
            },
        }
    config = raw_config if is_v2_config(raw_config) else migrate_legacy_config_to_v2(raw_config)
    return {"capabilities": _kb_capability_states(config)}
