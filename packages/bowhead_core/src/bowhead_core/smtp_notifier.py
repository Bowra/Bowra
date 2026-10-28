"""SMTP notifier for scheduled job completion (#126 Step 2).

email-config sprint 收口:本模块原本自己读一遍 env(第二套 SMTP 配置读取器)+ 自己开
socket。现统一走 email_config.resolve_smtp_config + deliver_email —— 全系统单一 resolver
chokepoint + 单一 socket 打开点(email_sender._send_smtp_detailed)。

`send_notification` 保持 `(ok, error)` 返回契约不变,调用方(_scheduled_runner)零改动除
新增 team_id/store 透传(实例级 env 兜底仍生效)。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bowhead_core.user_store import UserStore

logger = logging.getLogger(__name__)


async def send_notification(
    to: str,
    subject: str,
    body: str,
    *,
    team_id: str | None = None,
    store: UserStore | None = None,
) -> tuple[bool, str | None]:
    """发送一封邮件（非阻塞 asyncio-friendly）.

    返 `(ok, error)`. SMTP 未配置(team + env 皆无)时返 `(False, "smtp not configured")`，
    调用方自己决定是否 fallback 到 in-app / log.
    """
    from bowhead_core.email_config import deliver_email, resolve_smtp_config

    if not to or "@" not in to:
        return False, f"invalid to address: {to!r}"
    cfg = await resolve_smtp_config(team_id, store)
    if cfg is None:
        logger.info("smtp_notifier.skip_unconfigured to=%s subject=%s", to, subject)
        return False, "smtp not configured"
    return await deliver_email(to, subject, body, config=cfg)
