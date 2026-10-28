"""P4.5.1 — User / Team / member / token / credit_ledger / audit_log store.

Enterprise 版用户体系 (用户拍 v6 final).

6 表:
- teams  — BYO LLM 容器, 双层 credit pool 上层, per-team tier_credits limit
- users          — tier (pro/ultra) + credit_balance/quota (个人月余) + 必属 team
- workspace_members — workspace 内 role (admin/member/viewer)
- api_tokens     — token role/workspace snapshot at issue time
- credit_ledger  — 每 turn deduct + admin grant audit
- user_audit_log — login/logout/role change/grant/export, 跨 tier 全 tier 记 (12.11)
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
from collections.abc import Awaitable, Callable
from contextlib import nullcontext
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol
from uuid import uuid4

import aiosqlite

from bowhead_core._db import open_pooled_read_ctx
from bowhead_core._email_utils import normalize_email
from bowhead_core.access_control import _require_read_transaction
from bowhead_core.mcp_prompt_cache import validate_mcp_prompt_cache
from bowhead_core.public_entries import PublicEntryStore
from bowhead_core.rate_card import ModelUsageLine, credits_for_model_lines
from bowhead_core.space_governance import (
    SpaceGovernanceError,
    normalize_space_name,
    normalize_space_role,
)

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from bowhead_core.account_deletion import AccountDeletion

# cca P3 (review fix): a distinct "argument omitted" sentinel so update_team_subagent_meta can
# tell an OMITTED nullable field (skip — keep current value) from an EXPLICIT None (clear → SQL
# NULL). Used only for the two NULLABLE columns (model, output_schema_json); the NOT-NULL
# columns keep None = omit. The route maps PATCH-body `model_fields_set` onto this sentinel.
_UNSET: Any = object()
_CREDIT_ATTENTION_DISMISS_REASONS = {
    "not_relevant",
    "expected_demo",
    "handled_offline",
    "watch_later",
}
_CREDIT_ATTENTION_OPEN_TTL_DAYS = 30
_CREDIT_ATTENTION_DISMISS_TTL_DAYS = 7
_CREDIT_ATTENTION_VERIFICATION_WINDOW_DAYS = 7
_CREDIT_ATTENTION_PROTECTED_MIN_BALANCE_PCT = 0.20
_CREDIT_ATTENTION_PROTECTED_MIN_RUNWAY_DAYS = 7.0
_CREDIT_ATTENTION_ACTED_POLICY_MATCHES = {
    "grant_credit": {
        "blocked_valuable_user",
        "critical_balance_active_user",
        "low_balance_power_user",
    },
    "edit_user_quota": {
        "critical_balance_active_user",
        "growth_spike",
        "low_balance_power_user",
    },
    "edit_user_tier": {
        "critical_balance_active_user",
        "growth_spike",
        "low_balance_power_user",
        "new_user_activated",
    },
    "period_reset": {
        "blocked_valuable_user",
        "critical_balance_active_user",
        "low_balance_power_user",
    },
    "increase_team_quota": {
        "team_pool_critical",
        "team_pool_warning",
    },
}


class TeamOwnerTransferError(ValueError):
    """Stable owner-transfer failure that can cross the core → gateway boundary safely."""

    def __init__(
        self,
        code: Literal[
            "owner_transfer_target_not_active",
            "owner_transfer_conflict",
        ],
    ) -> None:
        self.code = code
        super().__init__(code)


class WorkspaceTeamMismatchError(ValueError):
    """A space grant target no longer has an active membership in the space's team."""

    code = "workspace_team_mismatch"

    def __init__(self) -> None:
        super().__init__("workspace_members requires active team membership")


def _row_value(row: Any, key: str) -> Any:
    try:
        return row[key]
    except (IndexError, KeyError, TypeError):
        return None


def _json_dict(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _json_list(raw: Any) -> list[str]:
    if isinstance(raw, list):
        return [str(item) for item in raw]
    try:
        parsed = json.loads(raw or "[]")
    except (json.JSONDecodeError, TypeError):
        return []
    return [str(item) for item in parsed] if isinstance(parsed, list) else []


def _parse_iso_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def _credit_attention_metric_attrs(
    *,
    attention_item_id: str | None,
    team_id: str | None,
    user_id: str | None,
    workspace_id: str | None,
    policy_key: str | None,
    dedupe_key: str | None,
    severity: str | None,
    payload: dict[str, Any],
    **extra: Any,
) -> dict[str, Any]:
    attrs: dict[str, Any] = {}
    for key, value in (
        ("attention_item_id", attention_item_id),
        ("team_id", team_id),
        ("user_id", user_id),
        ("workspace_id", workspace_id),
        ("policy_key", policy_key),
        ("dedupe_key", dedupe_key),
        ("severity", severity),
    ):
        if value is not None:
            attrs[key] = value
    for key in ("subject_type", "primary_segment"):
        value = payload.get(key)
        if value is not None:
            attrs[key] = value
    flags = payload.get("segment_flags")
    if isinstance(flags, list):
        attrs["flags"] = ",".join(str(flag) for flag in flags[:8])
    action_key = payload.get("recommended_action_key") or (_json_dict(payload.get("action_intent")).get("key"))
    if action_key:
        attrs["action_key"] = action_key
    attrs.update({key: value for key, value in extra.items() if value is not None})
    return attrs


def _credit_attention_event_from_candidate(
    name: str,
    *,
    team_id: str,
    item: Any,
    attention_item_id: str,
    **extra: Any,
) -> dict[str, Any]:
    payload = item.to_dict()
    return {
        "name": name,
        "user_id": item.user_id,
        "attrs": _credit_attention_metric_attrs(
            attention_item_id=attention_item_id,
            team_id=team_id,
            user_id=item.user_id,
            workspace_id=item.workspace_id,
            policy_key=item.policy_key,
            dedupe_key=item.dedupe_key,
            severity=item.severity,
            payload=payload,
            **extra,
        ),
    }


def _credit_attention_event_from_row(
    name: str,
    row: Any,
    **extra: Any,
) -> dict[str, Any]:
    payload = _json_dict(_row_value(row, "payload_json"))
    return {
        "name": name,
        "user_id": _row_value(row, "user_id"),
        "attrs": _credit_attention_metric_attrs(
            attention_item_id=_row_value(row, "attention_item_id"),
            team_id=_row_value(row, "team_id"),
            user_id=_row_value(row, "user_id"),
            workspace_id=_row_value(row, "workspace_id"),
            policy_key=_row_value(row, "policy_key"),
            dedupe_key=_row_value(row, "dedupe_key"),
            severity=_row_value(row, "severity"),
            payload=payload,
            **extra,
        ),
    }


SCHEMA = """
CREATE TABLE IF NOT EXISTS teams (
  id                       TEXT PRIMARY KEY,
  name                     TEXT NOT NULL,
  domain                   TEXT,
  llm_config_encrypted     TEXT NOT NULL DEFAULT '',
  max_seats                INTEGER NOT NULL DEFAULT 1,
  default_user_tier        TEXT NOT NULL DEFAULT 'pro',
  team_credit_balance       INTEGER NOT NULL DEFAULT 0,
  team_credit_quota         INTEGER NOT NULL DEFAULT 0,
  team_credit_period_start  TEXT,
  tier_credits_json        TEXT NOT NULL DEFAULT '{"pro":5000,"ultra":30000}',
  status                   TEXT NOT NULL DEFAULT 'active',
  created_at               TEXT NOT NULL,
  contract_expires_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_teams_domain ON teams(domain);
CREATE INDEX IF NOT EXISTS idx_teams_status ON teams(status);

CREATE TABLE IF NOT EXISTS users (
  id                       TEXT PRIMARY KEY,
  team_id                   TEXT NOT NULL,
  email                    TEXT UNIQUE NOT NULL,
  email_verified           INTEGER NOT NULL DEFAULT 0,
  display_name             TEXT,
  avatar_url               TEXT,
  password_hash            TEXT,
  oauth_providers_json     TEXT NOT NULL DEFAULT '[]',
  tier                     TEXT NOT NULL DEFAULT 'pro',
  credit_balance           INTEGER NOT NULL DEFAULT 0,
  credit_quota             INTEGER NOT NULL DEFAULT 0,
  credit_period_start      TEXT,
  locale                   TEXT NOT NULL DEFAULT 'zh-CN',
  timezone                 TEXT NOT NULL DEFAULT 'Asia/Shanghai',
  preferences_json         TEXT NOT NULL DEFAULT '{}',
  status                   TEXT NOT NULL DEFAULT 'active',
  created_at               TEXT NOT NULL,
  last_active_at           TEXT,
  FOREIGN KEY (team_id) REFERENCES teams(id)
);
CREATE INDEX IF NOT EXISTS idx_users_team ON users(team_id, status);
CREATE INDEX IF NOT EXISTS idx_users_tier ON users(tier);

CREATE TABLE IF NOT EXISTS workspace_members (
  workspace_id    TEXT NOT NULL,
  user_id         TEXT NOT NULL,
  role            TEXT NOT NULL DEFAULT 'member',  -- admin | member | viewer
  invited_by      TEXT,
  invited_at      TEXT,
  joined_at       TEXT NOT NULL,
  status          TEXT NOT NULL DEFAULT 'active',  -- active | invited | removed
  PRIMARY KEY (workspace_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_wsm_user ON workspace_members(user_id, status);

CREATE TABLE IF NOT EXISTS api_tokens (
  id                  TEXT PRIMARY KEY,
  user_id             TEXT NOT NULL,
  name                TEXT NOT NULL,
  hash                TEXT NOT NULL,
  workspace_id        TEXT,
  -- 12.9: snapshot role + workspace at issue time, role 变 token 不跟 (强制 reissue)
  role_snapshot       TEXT NOT NULL,
  expires_at          TEXT,
  created_at          TEXT NOT NULL,
  last_used_at        TEXT,
  revoked_at          TEXT
);
CREATE INDEX IF NOT EXISTS idx_tokens_user ON api_tokens(user_id);

CREATE TABLE IF NOT EXISTS credit_ledger (
  id            TEXT PRIMARY KEY,
  user_id       TEXT NOT NULL,
  team_id        TEXT NOT NULL,
  workspace_id  TEXT,
  turn_id       TEXT,
  delta         INTEGER NOT NULL,
  reason        TEXT NOT NULL,
  tokens_input  INTEGER NOT NULL DEFAULT 0,
  tokens_output INTEGER NOT NULL DEFAULT 0,
  created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ledger_user_at ON credit_ledger(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_ledger_team_at ON credit_ledger(team_id, created_at DESC);

-- credit-billing gap 7 — 逐次 LLM 调用的用量流水（计量，非计费）。
-- 与 credit_ledger 分表而不是塞 delta=0 的行：credits_summary 的「有效 turn」口径是
-- 按 ledger 行的 tokens_input+tokens_output >= 500 计数的，混入后台调用会直接把运营指标
-- 灌水。这张表回答「谁在什么上下文烧了多少 token」，credit_ledger 仍是唯一的钱账。
-- reason 区分 turn / dreaming / workflow_distill / session_title / unattributed，是决定
-- 某类用量该不该计费的关键维度；team_id/user_id 可空，因为后台调用未必有归属。
CREATE TABLE IF NOT EXISTS llm_usage_events (
  id            TEXT PRIMARY KEY,
  team_id       TEXT,
  user_id       TEXT,
  workspace_id  TEXT,
  turn_id       TEXT,
  reason        TEXT NOT NULL,
  agent_type    TEXT NOT NULL,
  provider      TEXT,
  model         TEXT,
  tokens_input  INTEGER NOT NULL DEFAULT 0,
  tokens_output INTEGER NOT NULL DEFAULT 0,
  tokens_cached INTEGER NOT NULL DEFAULT 0,
  streamed      INTEGER NOT NULL DEFAULT 0,
  created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_llm_usage_reason_at ON llm_usage_events(reason, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_llm_usage_team_at ON llm_usage_events(team_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_llm_usage_turn ON llm_usage_events(turn_id);

-- credit-billing gap 7 保留策略 —— 日级汇总（对应 `docs/credit-usage-activation-model.md` §9.1
-- 设想的 `user_usage_rollups`，这里按实际需要把 `reason` 也纳入主键）。
--
-- 为什么需要它：`llm_usage_events` 是逐次调用一行，实测每行 368 bytes（含索引）。300 人常规
-- 用量（每人每工作日约 10 turn、每 turn 约 15 次 LLM 调用）≈ 90 万行/月 = 3.7 GiB/年；整库是
-- 单文件 SQLite 且备份按整文件复制，留 3 份历史备份就是 ~15 GiB 占盘。所以原始明细只留一个
-- 排查窗口（默认 90 天），长期趋势靠这张汇总——它按天聚合后体积极小（300 人一年十几 MB），
-- 可永久保留，因此裁剪原始行**不损失任何分析能力**。
CREATE TABLE IF NOT EXISTS llm_usage_daily (
  day           TEXT NOT NULL,
  team_id       TEXT NOT NULL DEFAULT '',
  user_id       TEXT NOT NULL DEFAULT '',
  workspace_id  TEXT NOT NULL DEFAULT '',
  reason        TEXT NOT NULL,
  calls         INTEGER NOT NULL DEFAULT 0,
  tokens_input  INTEGER NOT NULL DEFAULT 0,
  tokens_output INTEGER NOT NULL DEFAULT 0,
  tokens_cached INTEGER NOT NULL DEFAULT 0,
  updated_at    TEXT NOT NULL,
  -- 主键内的可空列会让 UPSERT 的冲突判定失效（NULL != NULL），故上面几列用 '' 兜底而非 NULL
  PRIMARY KEY (day, team_id, user_id, workspace_id, reason)
);
CREATE INDEX IF NOT EXISTS idx_llm_usage_daily_day ON llm_usage_daily(day DESC);
CREATE INDEX IF NOT EXISTS idx_llm_usage_daily_reason ON llm_usage_daily(reason, day DESC);

CREATE TABLE IF NOT EXISTS credit_attention_state (
  attention_item_id TEXT PRIMARY KEY,
  team_id TEXT NOT NULL,
  user_id TEXT,
  workspace_id TEXT,
  policy_key TEXT NOT NULL,
  dedupe_key TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('open','dismissed','acted','protected','resolved','suppressed')),
  state_reason TEXT,
  severity TEXT NOT NULL CHECK(severity IN ('info','warning','critical')),
  severity_rank INTEGER NOT NULL,
  opened_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  first_shown_at TEXT,
  last_shown_at TEXT,
  shown_count INTEGER NOT NULL DEFAULT 0,
  dismissed_by TEXT,
  dismissed_at TEXT,
  acted_by TEXT,
  acted_at TEXT,
  protected_at TEXT,
  protection_reason TEXT,
  protection_snapshot_json TEXT NOT NULL DEFAULT '{}',
  resolved_at TEXT,
  linked_event_type TEXT,
  linked_event_id TEXT,
  payload_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_credit_attention_team_state
  ON credit_attention_state(team_id, state, severity_rank, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_credit_attention_dedupe
  ON credit_attention_state(team_id, dedupe_key, state, expires_at);
CREATE INDEX IF NOT EXISTS idx_credit_attention_user
  ON credit_attention_state(team_id, user_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_credit_attention_policy
  ON credit_attention_state(team_id, policy_key, state, updated_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS uq_credit_attention_active_dedupe
  ON credit_attention_state(team_id, dedupe_key)
  WHERE state IN ('open', 'acted');

CREATE TABLE IF NOT EXISTS credit_exhaustion_events (
  id TEXT PRIMARY KEY,
  team_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  workspace_id TEXT,
  code TEXT NOT NULL CHECK(code IN ('user_credit_exhausted','team_pool_exhausted','credit_deduct_failed')),
  credit_balance INTEGER,
  credit_quota INTEGER,
  team_credit_balance INTEGER,
  team_credit_quota INTEGER,
  turn_id TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_credit_exhaustion_team_user_at
  ON credit_exhaustion_events(team_id, user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_credit_exhaustion_team_code_at
  ON credit_exhaustion_events(team_id, code, created_at DESC);

CREATE TABLE IF NOT EXISTS user_audit_log (
  id            TEXT PRIMARY KEY,
  user_id       TEXT,
  team_id        TEXT,
  event_type    TEXT NOT NULL,  -- login_success / login_fail / logout / role_change / grant / export / delete
  ip            TEXT,
  user_agent    TEXT,
  payload_json  TEXT NOT NULL DEFAULT '{}',
  created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_user_at ON user_audit_log(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_event ON user_audit_log(event_type, created_at DESC);

-- P4.5.15: oauth state (multi-worker safe, 替 in-memory)
CREATE TABLE IF NOT EXISTS oauth_state (
  state       TEXT PRIMARY KEY,
  provider    TEXT NOT NULL,
  next_path   TEXT,
  public_entry_id TEXT,
  redirect_uri TEXT,
  expires_at  TEXT NOT NULL,
  created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_oauth_exp ON oauth_state(expires_at);

CREATE TABLE IF NOT EXISTS invite_tokens (
  id              TEXT PRIMARY KEY,
  workspace_id    TEXT NOT NULL,
  team_id          TEXT,
  role            TEXT NOT NULL,
  invited_email   TEXT NOT NULL,
  token_hash      TEXT NOT NULL UNIQUE,
  status          TEXT NOT NULL DEFAULT 'active',  -- active|revoked|consumed|expired
  invited_by      TEXT,
  created_at      TEXT NOT NULL,
  expires_at      TEXT NOT NULL,
  consumed_at     TEXT,
  revoked_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_invite_ws    ON invite_tokens(workspace_id);
CREATE INDEX IF NOT EXISTS idx_invite_email ON invite_tokens(invited_email);
CREATE INDEX IF NOT EXISTS idx_invite_status ON invite_tokens(status);

CREATE TABLE IF NOT EXISTS team_invite_tokens (
  id              TEXT PRIMARY KEY,
  team_id          TEXT NOT NULL,
  team_role        TEXT NOT NULL DEFAULT 'member',
  invited_email   TEXT NOT NULL,
  token_hash      TEXT NOT NULL UNIQUE,
  status          TEXT NOT NULL DEFAULT 'active',
  invited_by      TEXT,
  created_at      TEXT NOT NULL,
  expires_at      TEXT NOT NULL,
  consumed_at     TEXT,
  revoked_at      TEXT,
  assignment_mode TEXT NOT NULL DEFAULT 'team_only_legacy'
      CHECK(assignment_mode IN ('spaces', 'team_only', 'team_only_legacy')),
  assignment_revision INTEGER NOT NULL DEFAULT 1,
  public_entry_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_team_invite_team    ON team_invite_tokens(team_id);
CREATE INDEX IF NOT EXISTS idx_team_invite_email  ON team_invite_tokens(invited_email);
CREATE INDEX IF NOT EXISTS idx_team_invite_status ON team_invite_tokens(status);

CREATE TABLE IF NOT EXISTS team_invite_workspace_grants (
  invite_id       TEXT NOT NULL,
  workspace_id    TEXT NOT NULL,
  role            TEXT NOT NULL CHECK(role IN ('admin', 'member', 'viewer')),
  created_at      TEXT NOT NULL,
  PRIMARY KEY (invite_id, workspace_id),
  FOREIGN KEY (invite_id) REFERENCES team_invite_tokens(id) ON DELETE CASCADE,
  FOREIGN KEY (workspace_id) REFERENCES workspaces(id)
);
CREATE INDEX IF NOT EXISTS ix_team_invite_workspace_grants_workspace
  ON team_invite_workspace_grants(workspace_id, invite_id);

CREATE TABLE IF NOT EXISTS invite_intents (
  secret_hash     TEXT PRIMARY KEY,
  invite_type     TEXT NOT NULL CHECK(invite_type IN ('team','workspace')),
  invite_id       TEXT NOT NULL,
  token_version   INTEGER NOT NULL,
  created_at      TEXT NOT NULL,
  expires_at      TEXT NOT NULL,
  consumed_at     TEXT,
  invalidated_at  TEXT,
  last_seen_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_invite_intents_exp
  ON invite_intents(expires_at);
CREATE INDEX IF NOT EXISTS idx_invite_intents_target
  ON invite_intents(invite_type, invite_id);

CREATE TABLE IF NOT EXISTS user_federated_identities (
  provider       TEXT NOT NULL,
  subject        TEXT NOT NULL,
  user_id        TEXT NOT NULL,
  email_at_link  TEXT NOT NULL,
  created_at     TEXT NOT NULL,
  last_login_at  TEXT,
  PRIMARY KEY(provider, subject),
  FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_federated_user
  ON user_federated_identities(user_id, provider);

CREATE TABLE IF NOT EXISTS credential_recovery_tokens (
  token_hash          TEXT PRIMARY KEY,
  user_id             TEXT NOT NULL,
  team_id             TEXT NOT NULL,
  purpose             TEXT NOT NULL CHECK(purpose IN ('password_reset','password_setup')),
  status              TEXT NOT NULL CHECK(status IN ('active','consumed','revoked')),
  created_by_user_id  TEXT NOT NULL,
  created_at          TEXT NOT NULL,
  expires_at          TEXT NOT NULL,
  consumed_at         TEXT,
  revoked_at          TEXT,
  FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_recovery_user_status
  ON credential_recovery_tokens(user_id, purpose, status);
CREATE UNIQUE INDEX IF NOT EXISTS uq_recovery_active
  ON credential_recovery_tokens(user_id, purpose)
  WHERE status = 'active';

CREATE TABLE IF NOT EXISTS user_store_migrations (
  name        TEXT PRIMARY KEY,
  applied_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS refresh_tokens (
  id              TEXT PRIMARY KEY,
  user_id         TEXT NOT NULL,
  token_hash      TEXT NOT NULL UNIQUE,
  expires_at      TEXT NOT NULL,
  created_at      TEXT NOT NULL,
  last_used_at    TEXT,
  revoked_at      TEXT,
  user_agent      TEXT,
  ip              TEXT
);
CREATE INDEX IF NOT EXISTS idx_refresh_user    ON refresh_tokens(user_id, revoked_at);
CREATE INDEX IF NOT EXISTS idx_refresh_expires ON refresh_tokens(expires_at);

-- Sprint α (0.35.0) new tables — G3 SMTP outbox + G9 SQLite rate limit
CREATE TABLE IF NOT EXISTS smtp_outbox (
  id                TEXT PRIMARY KEY,
  to_email          TEXT NOT NULL,
  subject           TEXT NOT NULL,
  body              TEXT NOT NULL,
  purpose           TEXT NOT NULL,
  status            TEXT NOT NULL DEFAULT 'pending',
  attempts          INTEGER NOT NULL DEFAULT 0,
  last_error        TEXT,
  created_at        TEXT NOT NULL,
  sent_at           TEXT,
  next_attempt_at   TEXT
  ,team_id           TEXT
  ,html_body         TEXT
  ,public_entry_id   TEXT
  ,brand_revision_id TEXT
  ,branding_snapshot TEXT
);
CREATE INDEX IF NOT EXISTS idx_smtp_outbox_pending ON smtp_outbox(status, next_attempt_at);

CREATE TABLE IF NOT EXISTS rate_limit_buckets (
  bucket_key   TEXT NOT NULL,
  window_start INTEGER NOT NULL,
  count        INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (bucket_key, window_start)
);
CREATE INDEX IF NOT EXISTS idx_rate_limit_cleanup ON rate_limit_buckets(window_start);

-- Sprint β (0.36.0) — G8 admin audit log + D-3=B append-only triggers
CREATE TABLE IF NOT EXISTS admin_audit_log (
    id              TEXT PRIMARY KEY,
    actor_user_id   TEXT NOT NULL,
    team_id          TEXT NOT NULL,
    action          TEXT NOT NULL,
    target_kind     TEXT,
    target_id       TEXT,
    target_team_id   TEXT,
    scope           TEXT NOT NULL,
    payload_json    TEXT NOT NULL DEFAULT '{}',
    ts              TEXT NOT NULL,
    ip              TEXT,
    user_agent      TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_team_ts        ON admin_audit_log(team_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_audit_actor_ts      ON admin_audit_log(actor_user_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_audit_action_ts     ON admin_audit_log(action, ts DESC);
CREATE INDEX IF NOT EXISTS idx_audit_target        ON admin_audit_log(target_kind, target_id, ts DESC);

CREATE TRIGGER IF NOT EXISTS admin_audit_log_no_update
  BEFORE UPDATE ON admin_audit_log
  BEGIN SELECT RAISE(ABORT, 'admin_audit_log is append-only'); END;
-- 批4: 曾有 admin_audit_log_no_delete_full 触发器（BEFORE DELETE, WHEN NEW IS NULL,
-- body=SELECT 1）——NEW 在 DELETE trigger 里非法且 body 是 no-op，enforce 什么都不做，
-- 却在 schema re-prepare 时炸 boot。已删除定义；存量库由 _db.bootstrap_shared_db 拆除。
-- DELETE 保护是 application-level（retention cron 才允许删）。

-- Sprint β G5 — team_members multi-admin + single-team D-2=C trigger
CREATE TABLE IF NOT EXISTS team_members (
    id              TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL,
    team_id          TEXT NOT NULL,
    role            TEXT NOT NULL CHECK(role IN ('team_admin', 'audit_admin', 'member')),
    promoted_by     TEXT,
    promoted_at     TEXT,
    removed_at      TEXT,
    removed_by      TEXT,
    status          TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'removed')),
    created_at      TEXT NOT NULL,
    UNIQUE(user_id, team_id),
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
    FOREIGN KEY (team_id) REFERENCES teams(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_team_members_team_role    ON team_members(team_id, role, status);
CREATE INDEX IF NOT EXISTS idx_team_members_user_status ON team_members(user_id, status);

-- Sprint β G2 — Bowhead canonical workspaces table (D-1=B)
CREATE TABLE IF NOT EXISTS workspaces (
    id                       TEXT PRIMARY KEY,
    team_id                   TEXT NOT NULL,
    name                     TEXT NOT NULL,
    name_key                 TEXT,
    datarc_workspace_id      TEXT,            -- default datarc PROJECT for this workspace (connector mapping; seeds active_datarc_project). NOT the workspace identity — that is `id` (a ws_*). ws-datarc-decouple S3.
    description              TEXT,
    status                   TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'archived')),
    revision                 INTEGER NOT NULL DEFAULT 1,
    created_at               TEXT NOT NULL,
    updated_at               TEXT NOT NULL,
    last_synced_at           TEXT,
    FOREIGN KEY (team_id) REFERENCES teams(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_workspaces_team_status ON workspaces(team_id, status);
CREATE INDEX IF NOT EXISTS idx_workspaces_datarc    ON workspaces(datarc_workspace_id);
CREATE INDEX IF NOT EXISTS ix_workspaces_team_status_name
  ON workspaces(team_id, status, name_key, id);

-- Canonical Space memberships must stay inside one Team. Legacy rows that
-- reference a pre-migration/non-canonical workspace id are allowed here because
-- no workspaces row exists for them; runtime auth still requires membership.
CREATE TRIGGER IF NOT EXISTS workspace_members_same_team_check_insert
  BEFORE INSERT ON workspace_members
  WHEN (SELECT team_id FROM workspaces WHERE id = NEW.workspace_id) IS NOT NULL
   AND (SELECT team_id FROM workspaces WHERE id = NEW.workspace_id)
       NOT IN (SELECT team_id FROM team_members WHERE user_id = NEW.user_id AND status = 'active')
   AND (SELECT team_id FROM users WHERE id = NEW.user_id)
       != (SELECT team_id FROM workspaces WHERE id = NEW.workspace_id)
  BEGIN SELECT RAISE(ABORT, 'workspace_members requires active team membership'); END;

-- Conversational Capability Authoring (PRD §8.1) — team-scoped OpenAPI→MCP tool sources.
-- Runtime isolation is via the per-turn/per-team tool view (NOT name-mangling, §6.10).
-- auth_ref = Fernet ciphertext (NEVER plaintext for auth_type != none; F14 guard in update_team_tool_source_auth).
CREATE TABLE IF NOT EXISTS team_tool_source (
  id                   TEXT PRIMARY KEY,
  team_id               TEXT NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
  name                 TEXT NOT NULL,                 -- display label (mutable; NOT part of the registered tool name)
  kind                 TEXT NOT NULL DEFAULT 'openapi',
  spec_url             TEXT,
  openapi_spec_json    TEXT,                          -- cached raw spec
  selected_op_ids_json TEXT NOT NULL DEFAULT '[]',    -- HITL-selected operationId subset
  base_url             TEXT NOT NULL,
  auth_type            TEXT NOT NULL DEFAULT 'none',   -- none|api_key|bearer|basic
  auth_ref             TEXT,                          -- Fernet ciphertext of creds (decrypted only at execute-time)
  tools_schema_json    TEXT NOT NULL DEFAULT '[]',    -- enumerate cache (mcp tool metadata)
  tool_overrides_json  TEXT NOT NULL DEFAULT '{}',    -- cca P2: per-op {operationId:{description}} desc overrides
  visibility           TEXT NOT NULL DEFAULT 'team_wide',  -- team_wide|creator_only (留字段)
  created_by           TEXT,
  status               TEXT NOT NULL DEFAULT 'enabled',   -- enabled|disabled|failed
  created_at           TEXT NOT NULL,
  updated_at           TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_ots_team_name ON team_tool_source(team_id, name);
CREATE INDEX IF NOT EXISTS idx_ots_team ON team_tool_source(team_id, status);

-- tools sprint WP2b-2 — short-lived pending OAuth authorization (PKCE + CSRF state) for an
-- team_tool_source of kind='mcp'. Bridges the two legs of the web flow: the authorize endpoint
-- INSERTs one row keyed by an opaque random `state`, the callback endpoint consumes it EXACTLY
-- ONCE (delete-on-read) after matching `state`. Holds the PKCE code_verifier (ephemeral, single-
-- use — like the login `oauth_state` table) plus the endpoint/client_id/resource the exchange
-- reuses. team-scoped + created_by so a callback can only complete an authorization the same team's
-- admin started. Expires fast; a sweeper / lazy expiry check drops stale rows.
CREATE TABLE IF NOT EXISTS mcp_oauth_pending (
  state          TEXT PRIMARY KEY,
  team_id         TEXT NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
  source_id      TEXT NOT NULL,
  code_verifier  TEXT NOT NULL,                  -- ephemeral PKCE secret; single-use, short TTL
  redirect_uri   TEXT NOT NULL,                  -- exact redirect_uri used at authorize (RFC 6749 §4.1.3)
  token_endpoint TEXT NOT NULL,
  client_id      TEXT NOT NULL,
  resource       TEXT,                            -- RFC 8707 resource binding (nullable)
  public_entry_id TEXT,                           -- browser-visible callback entry
  created_by     TEXT,
  expires_at     TEXT NOT NULL,
  created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mcp_oauth_pending_exp ON mcp_oauth_pending(expires_at);

-- CCA P3 (subagent-creator) — team-scoped subagents: a member assembles a SUBSET of
-- the team's existing teammcp.* tools (+ native analyst tools) into a routable expert.
-- Mirrors team_tool_source (team-scoped, IDOR-safe). NO secret column — a subagent holds
-- no upstream creds; the referenced team tools keep their own auth_ref. tool_refs_json =
-- the declared bundle (teammcp.<src>.<op> derived names + native names); the per-turn
-- binding (tool_registry._effective_tool_whitelist) intersects the team overlay with it.
CREATE TABLE IF NOT EXISTS team_subagent (
  id                 TEXT PRIMARY KEY,
  team_id             TEXT NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
  name               TEXT NOT NULL,                  -- peer/spawn routing name (slug); immutable post-create
  display_name       TEXT NOT NULL DEFAULT '',
  description        TEXT NOT NULL DEFAULT '',        -- Lead-facing routing desc (untrusted → sanitized at render)
  system_prompt      TEXT NOT NULL DEFAULT '',        -- persona (linted at create + resolve)
  tool_refs_json     TEXT NOT NULL DEFAULT '[]',      -- the declared SUBSET: teammcp.<src>.<op> + native names
  model              TEXT,                            -- NULL → analyst; else an allowlisted llm_agent_type
  max_iterations     INTEGER NOT NULL DEFAULT 10,     -- [1,50]
  skills_json        TEXT NOT NULL DEFAULT '[]',      -- auto_read_skills → prelude at resolve
  output_schema_json TEXT,                            -- NULL | user JSON Schema (well-formed + bounded at create)
  visibility         TEXT NOT NULL DEFAULT 'team_wide',-- team_wide | creator_only (reserved)
  created_by         TEXT,
  status             TEXT NOT NULL DEFAULT 'enabled', -- enabled | disabled
  created_at         TEXT NOT NULL,
  updated_at         TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_osa_team_name ON team_subagent(team_id, name);
CREATE INDEX IF NOT EXISTS idx_osa_team ON team_subagent(team_id, status);

-- workflow-solidify S4 — team-scoped shared recipes (固化的可复用 SKILL.md，team_wide 共享).
-- Mirrors team_subagent (team-scoped, IDOR-safe, team_id-keyed CRUD). NO secret column. The
-- SKILL.md + scripts live on disk at source_path (.data/team_skills/<team_id>/<name>/); this
-- row is the metadata + dry-run verdict index. Resolved per-request by (team_id, name) via
-- skill_resolver team branch — NEVER enters the global name-keyed catalog (tenant isolation).
CREATE TABLE IF NOT EXISTS team_skill (
  id                 TEXT PRIMARY KEY,
  team_id             TEXT NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
  name               TEXT NOT NULL,                   -- skill slug; immutable post-create
  display_name       TEXT NOT NULL DEFAULT '',
  description        TEXT NOT NULL DEFAULT '',         -- advertise/trigger desc (untrusted → sanitized)
  source_path        TEXT NOT NULL DEFAULT '',         -- .data/team_skills/<team_id>/<name>/ (SKILL.md + scripts)
  visibility         TEXT NOT NULL DEFAULT 'team_wide', -- team_wide | creator_only (reserved)
  verdict            TEXT,                             -- dry-run judge: pass|fail|skipped|error|NULL
  verdict_json       TEXT,
  risk_report_json   TEXT,                             -- skill-multiruntime S1 (G5): 上传 risk report
  created_by         TEXT,
  status             TEXT NOT NULL DEFAULT 'enabled',  -- enabled | disabled
  size_bytes         INTEGER NOT NULL DEFAULT 0,
  created_at         TEXT NOT NULL,
  updated_at         TEXT NOT NULL,
  -- skill-team-governance P1b — 双槽审批状态机（tech-design §1）。approval 只描述「已批槽」
  -- （首提生命周期）；提交更新完全走 candidate_* 候选槽、approval 保持 approved 不动——
  -- 否则更新在审会把 live 团队版从 only_approved 面藏掉（审批期间团队继续用旧已批版）。
  approval           TEXT NOT NULL DEFAULT 'approved', -- pending | approved | rejected（默认 approved 兼容 creator_only 缺省路）
  approved_sha256    TEXT,                             -- 已批内容 hash（落后/已同步判定）
  approved_by        TEXT,
  approved_at        TEXT,
  rejected_reason    TEXT,                             -- 首提拒（L1）；候选拒走 candidate_rejected_reason
  rejected_at        TEXT,
  candidate_path     TEXT,                             -- 候选 bundle 目录（<name>__candidate/）；NULL=无候选
  candidate_submitted_at TEXT,                         -- CAS 键之一（approve/reject 带回比对）
  candidate_sha256   TEXT,                             -- CAS 键之二 + 落后判定
  candidate_state    TEXT,                             -- pending | rejected（仅候选腿）
  candidate_rejected_reason TEXT,
  submitted_by       TEXT                              -- 最近一次提交人
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_oskill_team_name ON team_skill(team_id, name);
CREATE INDEX IF NOT EXISTS idx_oskill_team ON team_skill(team_id, status);

-- Per-tool-call execution/usage log (PRD §6.8 / F22) — NOT admin_audit_log (that's the admin-action
-- integrity store). Holds NO decrypted secret and NO raw args (args_digest only).
CREATE TABLE IF NOT EXISTS team_tool_call_log (
  id              TEXT PRIMARY KEY,
  team_id          TEXT NOT NULL,
  user_id         TEXT,
  source_id       TEXT,
  tool_name       TEXT,
  args_digest     TEXT,
  status          TEXT,
  error_kind      TEXT,
  ssrf_verdict    TEXT,
  duration_ms     INTEGER,
  upstream_status INTEGER,
  bytes_out       INTEGER,
  created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_otcl_team ON team_tool_call_log(team_id, created_at);

-- Licensing §6.1 — verified-license durable mirror (on-prem). Empty + unused on a saas/101 deploy (OPS-8):
-- the in-memory verified token is the source of truth; this row is the last-known-good store + DB mirror.
CREATE TABLE IF NOT EXISTS team_license (
  team_id                    TEXT PRIMARY KEY REFERENCES teams(id),
  license_token             TEXT NOT NULL,
  license_id                TEXT NOT NULL,
  key_id                    TEXT NOT NULL,
  issued_at                 TEXT NOT NULL,
  expires_at                TEXT,
  grace_period_days         INTEGER NOT NULL DEFAULT 14,
  max_seats                 INTEGER NOT NULL DEFAULT 1,
  default_user_tier         TEXT NOT NULL DEFAULT 'pro',
  tier_credits_json         TEXT NOT NULL DEFAULT '{}',
  features_json             TEXT NOT NULL DEFAULT '[]',
  deployment_mode           TEXT NOT NULL DEFAULT 'local',
  max_offline_lifetime_days INTEGER,
  activated_at              TEXT NOT NULL,
  last_verified_at          TEXT,
  last_good_health          TEXT,
  instance_fingerprint      TEXT
);

-- Licensing §7.4 — clock-rollback backstop (local only; saas ClockGuard is a no-op). A dedicated monotonic
-- single-row table (admin_audit_log DELETE is not DB-enforced). Advanced only on startup-verify + recheck,
-- never on the turn hot path. Bar-raiser vs casual `date` rollback, NOT cryptographic (file-level tamper defeats it).
CREATE TABLE IF NOT EXISTS entitlement_clock (
  id              TEXT PRIMARY KEY DEFAULT 'singleton',
  highest_seen_ts TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS entitlement_clock_monotonic BEFORE UPDATE ON entitlement_clock
  WHEN NEW.highest_seen_ts < OLD.highest_seen_ts
  BEGIN SELECT RAISE(ABORT, 'clock rollback'); END;
"""


def _now() -> str:
    return datetime.now(UTC).isoformat()


async def _read_team_role_in_tx(
    connection: aiosqlite.Connection,
    *,
    user_id: str,
    team_id: str,
    require_active_principal: bool = False,
) -> str | None:
    """Canonical connection-local team role lookup for atomic callers."""

    async with connection.execute(
        """
        SELECT t.created_by_user_id,t.status AS team_status,
               u.status AS user_status,tm.role,tm.status AS membership_status
        FROM teams t
        LEFT JOIN users u ON u.id=?
        LEFT JOIN team_members tm
          ON tm.team_id=t.id AND tm.user_id=?
        WHERE t.id=?
        """,
        (user_id, user_id, team_id),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        return None
    if require_active_principal and (row["team_status"] != "active" or row["user_status"] != "active"):
        return None
    if row["created_by_user_id"] == user_id:
        return "owner"
    if row["membership_status"] == "active":
        return row["role"]
    return None


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:12]}"


@dataclass(frozen=True, slots=True)
class MemberDeactivationResult:
    """Detailed result for the member endpoint without breaking tuple callers."""

    changed: bool
    error_code: str | None = None
    error_details: dict[str, Any] | None = None
    identity_deleted: bool = False


# ─────────────────────────────────────────────────────────
# Conversational Capability Authoring — team tool source (PRD §8.1)
# ─────────────────────────────────────────────────────────


@dataclass
class TeamToolSource:
    id: str
    team_id: str
    name: str
    base_url: str
    kind: str = "openapi"
    spec_url: str | None = None
    openapi_spec_json: str | None = None
    selected_op_ids: list[str] = field(default_factory=list)
    auth_type: str = "none"  # none|api_key|bearer|basic
    auth_ref: str | None = None  # Fernet ciphertext; never plaintext for auth_type != none
    tools_schema: list[dict[str, Any]] = field(default_factory=list)
    # MCP prompts/list metadata only. Expanded prompt content is runtime material
    # and must never be written to the connection registry.
    prompts_schema: list[dict[str, Any]] = field(default_factory=list)
    # cca P2: per-op LLM-proposed / user-edited overrides, keyed by operationId → {"description": str}.
    # Overrides only the LLM-facing tool DESCRIPTION; the registered tool NAME stays the derived stable
    # identity teammcp.<src>.<op> (security — never user-mutable).
    tool_overrides: dict[str, dict[str, Any]] = field(default_factory=dict)
    visibility: str = "team_wide"
    created_by: str | None = None
    status: str = "enabled"
    created_at: str = ""
    updated_at: str = ""


@dataclass
class TeamBuiltinTool:
    """A team-owned configuration for an image-bundled third-party capability.

    ``auth_ref`` is Fernet ciphertext and is deliberately absent from every API
    projection. Providers are a closed server-side registry; providers whose
    service is self-hostable may also carry an administrator-supplied endpoint.
    """

    id: str
    team_id: str
    provider: str
    display_name: str
    endpoint: str
    status: str
    auth_ref: str
    key_hint: str | None = None
    scope_state: str = "unknown"
    scope: dict[str, Any] = field(default_factory=dict)
    scope_fingerprint: str | None = None
    scope_checked_at: str | None = None
    # Provider-owned non-secret settings (0116). ``scope_*`` stays the Datarc key-scope snapshot.
    config: dict[str, Any] = field(default_factory=dict)
    created_by: str | None = None
    created_at: str = ""
    updated_at: str = ""
    connection_revision: int = 1


def _row_to_team_builtin_tool(row: Any) -> TeamBuiltinTool:
    return TeamBuiltinTool(
        id=row["id"],
        team_id=row["team_id"],
        provider=row["provider"],
        display_name=row["display_name"],
        endpoint=row["endpoint"],
        status=row["status"],
        auth_ref=row["auth_ref"],
        key_hint=row["key_hint"],
        scope_state=_row_value(row, "scope_state") or "unknown",
        scope=_json_dict(_row_value(row, "scope_json")),
        scope_fingerprint=_row_value(row, "scope_fingerprint"),
        scope_checked_at=_row_value(row, "scope_checked_at"),
        config=_json_dict(_row_value(row, "config_json")),
        connection_revision=_row_value(row, "connection_revision") or 1,
        created_by=row["created_by"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_team_tool_source(row: Any) -> TeamToolSource:
    return TeamToolSource(
        id=row["id"],
        team_id=row["team_id"],
        name=row["name"],
        kind=row["kind"],
        spec_url=row["spec_url"],
        openapi_spec_json=row["openapi_spec_json"],
        selected_op_ids=json.loads(row["selected_op_ids_json"] or "[]"),
        base_url=row["base_url"],
        auth_type=row["auth_type"],
        auth_ref=row["auth_ref"],
        tools_schema=json.loads(row["tools_schema_json"] or "[]"),
        prompts_schema=(json.loads(row["prompts_schema_json"] or "[]") if "prompts_schema_json" in tuple(row.keys()) else []),
        tool_overrides=json.loads(row["tool_overrides_json"] or "{}"),
        visibility=row["visibility"],
        created_by=row["created_by"],
        status=row["status"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


@dataclass
class TeamSubagent:
    """cca P3 — an team-scoped subagent (a routable expert assembled from a SUBSET of
    the team's teammcp.* tools + native tools). NO secret field (holds no creds)."""

    id: str
    team_id: str
    name: str  # peer/spawn routing name (slug); immutable post-create
    display_name: str = ""
    description: str = ""
    system_prompt: str = ""
    tool_refs: list[str] = field(default_factory=list)  # declared bundle: teammcp.<src>.<op> + native names
    model: str | None = None  # None → analyst; else allowlisted llm_agent_type
    max_iterations: int = 10
    skills: list[str] = field(default_factory=list)
    output_schema: dict[str, Any] | None = None  # user JSON Schema (well-formed + bounded at create)
    visibility: str = "team_wide"
    created_by: str | None = None
    status: str = "enabled"
    created_at: str = ""
    updated_at: str = ""
    row_version: int = 1
    origin_mode: str = "manual"
    source_draft_id: str | None = None
    # Read from the live revision definition (not a projected column): the
    # published definition is the only authority for this opt-in.
    capability_protocol_block: bool = False


@dataclass(frozen=True, slots=True)
class TeamSubagentSummaryRecord:
    """Minimum database projection used by field-safe management lists."""

    id: str
    name: str
    display_name: str
    description: str
    model: str | None
    visibility: str
    created_by: str | None
    status: str
    updated_at: str
    row_version: int
    tool_count: int
    team_tool_count: int
    skill_count: int


_TEAM_SUBAGENT_SUMMARY_SELECT = """
SELECT
  id,
  name,
  display_name,
  description,
  model,
  visibility,
  created_by,
  status,
  updated_at,
  row_version,
  json_array_length(COALESCE(tool_refs_json, '[]')) AS tool_count,
  (
    SELECT COUNT(*)
    FROM json_each(COALESCE(team_subagent.tool_refs_json, '[]'))
    WHERE CAST(value AS TEXT) LIKE 'teammcp.%'
  ) AS team_tool_count,
  json_array_length(COALESCE(skills_json, '[]')) AS skill_count
FROM team_subagent
"""


async def _live_capability_protocol_block(db: aiosqlite.Connection, row: Any) -> bool:
    """Return the capability protocol opt-in frozen in the live revision.

    Agents without a published revision (legacy definitions) are off. The key
    must be a JSON boolean; anything else is treated as off.
    """
    live_revision_id = row["live_revision_id"] if "live_revision_id" in tuple(row.keys()) else None
    if not live_revision_id:
        return False
    async with db.execute(
        """
        SELECT json_type(definition_json,'$.capability_protocol_block') = 'true'
        FROM team_subagent_revision
        WHERE id=? AND team_id=? AND subagent_id=?
        """,
        (live_revision_id, row["team_id"], row["id"]),
    ) as cur:
        result = await cur.fetchone()
    return bool(result is not None and result[0])


def _row_to_team_subagent(row: Any) -> TeamSubagent:
    return TeamSubagent(
        id=row["id"],
        team_id=row["team_id"],
        name=row["name"],
        display_name=row["display_name"],
        description=row["description"],
        system_prompt=row["system_prompt"],
        tool_refs=json.loads(row["tool_refs_json"] or "[]"),
        model=row["model"],
        max_iterations=row["max_iterations"],
        skills=json.loads(row["skills_json"] or "[]"),
        output_schema=(json.loads(row["output_schema_json"]) if row["output_schema_json"] else None),
        visibility=row["visibility"],
        created_by=row["created_by"],
        status=row["status"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        row_version=(int(row["row_version"]) if "row_version" in tuple(row.keys()) else 1),
        origin_mode=(str(row["origin_mode"]) if "origin_mode" in tuple(row.keys()) else "manual"),
        source_draft_id=(row["source_draft_id"] if "source_draft_id" in tuple(row.keys()) else None),
    )


def _row_to_team_subagent_summary(
    row: Any,
) -> TeamSubagentSummaryRecord:
    return TeamSubagentSummaryRecord(
        id=str(row["id"]),
        name=str(row["name"]),
        display_name=str(row["display_name"] or ""),
        description=str(row["description"] or ""),
        model=(str(row["model"]) if row["model"] is not None else None),
        visibility=str(row["visibility"] or "team_wide"),
        created_by=(str(row["created_by"]) if row["created_by"] is not None else None),
        status=str(row["status"]),
        updated_at=str(row["updated_at"]),
        row_version=int(row["row_version"]),
        tool_count=int(row["tool_count"]),
        team_tool_count=int(row["team_tool_count"]),
        skill_count=int(row["skill_count"]),
    )


@dataclass
class TeamSkill:
    """workflow-solidify S4 — an team-scoped shared recipe (固化的可复用 SKILL.md). NO secret
    field. SKILL.md + scripts on disk at source_path; this is the metadata + verdict index."""

    id: str
    team_id: str
    name: str  # skill slug; immutable post-create
    display_name: str = ""
    description: str = ""
    source_path: str = ""  # .data/team_skills/<team_id>/<name>/
    visibility: str = "team_wide"  # team_wide | creator_only (reserved)
    verdict: str | None = None  # Dry-run judge result (None = not yet run)
    verdict_json: str | None = None
    risk_report_json: str | None = None  # skill-multiruntime S1 (G5): upload risk report
    created_by: str | None = None
    status: str = "enabled"  # enabled | disabled
    size_bytes: int = 0
    created_at: str = ""
    updated_at: str = ""
    # skill-team-governance P1b: dual approval slots (tech-design §1); approval = approved slot (initial-submission lifecycle),
    # candidate_* = candidate slot (update lifecycle); approval unchanged keeps live content visible.
    approval: str = "approved"  # pending | approved | rejected
    approved_sha256: str | None = None
    approved_by: str | None = None
    approved_at: str | None = None
    rejected_reason: str | None = None  # Initial rejection (L1)
    rejected_at: str | None = None
    candidate_path: str | None = None  # <name>__candidate/ directory; None = no candidate
    candidate_submitted_at: str | None = None  # First CAS key
    candidate_sha256: str | None = None  # Second CAS key
    candidate_state: str | None = None  # pending | rejected (candidate branch only)
    candidate_rejected_reason: str | None = None
    submitted_by: str | None = None  # Most recent submitter
    # Track B batch 4 (migration 0064): provenance of promotion after author force-activation with blockers (import_status='overridden').
    # Server computes from personal instance at promote time, never client input. None = another path (all legacy rows None).
    # ⚠ Sole write entry is `set_team_skill_import_provenance`; `create_team_skill`'s INSERT **omits** this column
    #   (prevents supplying fabricated clean provenance at creation). Assigning here does not persist it.
    import_provenance_json: str | None = None


def _row_opt(row: Any, column: str) -> Any:
    """读一个**可能还没迁移出来**的列；缺列返回 None（sqlite3.Row 直接取会 IndexError）。"""

    return row[column] if column in row.keys() else None  # noqa: SIM118 -- Row.keys() contains column names; `in row` compares values


def _row_to_team_skill(row: Any) -> TeamSkill:
    return TeamSkill(
        id=row["id"],
        team_id=row["team_id"],
        name=row["name"],
        display_name=row["display_name"],
        description=row["description"],
        source_path=row["source_path"],
        visibility=row["visibility"],
        verdict=row["verdict"],
        verdict_json=row["verdict_json"],
        risk_report_json=row["risk_report_json"],
        created_by=row["created_by"],
        status=row["status"],
        size_bytes=int(row["size_bytes"] or 0),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        approval=row["approval"] or "approved",
        approved_sha256=row["approved_sha256"],
        approved_by=row["approved_by"],
        approved_at=row["approved_at"],
        rejected_reason=row["rejected_reason"],
        rejected_at=row["rejected_at"],
        candidate_path=row["candidate_path"],
        candidate_submitted_at=row["candidate_submitted_at"],
        candidate_sha256=row["candidate_sha256"],
        candidate_state=row["candidate_state"],
        candidate_rejected_reason=row["candidate_rejected_reason"],
        submitted_by=row["submitted_by"],
        # Before migration 0064, this column is absent: SQLite Row raises IndexError; treat missing as None.
        # ⚠ Compare `row.keys()`: sqlite3.Row `in row` checks **values**, not column names (ruff SIM118 is a false positive here).
        import_provenance_json=_row_opt(row, "import_provenance_json"),
    )


class TeamSkillStoreAdapter:
    """workflow-solidify S4c — adapt UserStore → ``resolve_skill`` 的 team_skill_store duck-type.

    ``bowhead_skill.skill_resolver`` 不 import bowhead_core（保分层），它 duck-type 期望
    ``get_by_name(team_id, name, only_enabled=True) -> rec(.source_path/.visibility/.created_by)``.
    UserStore 的方法名是 ``get_team_skill_by_name``，故由本侧（已知 UserStore）建此 adapter，
    注入 ctx.team_skill_store / HTTP 路由，桥接方法名."""

    __slots__ = ("_us",)

    def __init__(self, user_store: UserStore) -> None:
        self._us = user_store

    async def get_by_name(
        self,
        team_id: str,
        name: str,
        only_enabled: bool = True,
    ) -> TeamSkill | None:
        return await self._us.get_team_skill_by_name(team_id, name, only_enabled=only_enabled)

    async def list(
        self,
        team_id: str,
        only_enabled: bool = True,
    ) -> list[TeamSkill]:
        # workflow-solidify P1 (review): used by skill_list's team branch. Adapter previously lacked list, so
        # tools._list's .list() raised swallowed AttributeError, making team skills undiscoverable in skill_list.
        # Return all; caller tools._list applies creator_only visibility using ctx.user_id.
        return await self._us.list_team_skills(team_id, only_enabled=only_enabled)


# ─────────────────────────────────────────────────────────
# Password hash (Python stdlib scrypt; no argon2 dependency)
# ─────────────────────────────────────────────────────────


def hash_password(password: str) -> str:
    """scrypt(N=16384, r=8, p=1) — OWASP 推荐, 跟 argon2id 安全度相当."""
    salt = secrets.token_bytes(16)
    h = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=16384,
        r=8,
        p=1,
        dklen=64,
    )
    # Format: scrypt$N$r$p$salt_hex$hash_hex
    return f"scrypt$16384$8$1${salt.hex()}${h.hex()}"


def verify_password(password: str, stored: str) -> bool:
    if not stored or not password:
        return False
    parts = stored.split("$")
    if len(parts) != 6 or parts[0] != "scrypt":
        return False
    try:
        n, r, p = int(parts[1]), int(parts[2]), int(parts[3])
        salt = bytes.fromhex(parts[4])
        expected = bytes.fromhex(parts[5])
    except (ValueError, IndexError):
        return False
    h = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=n,
        r=r,
        p=p,
        dklen=len(expected),
    )
    return hmac.compare_digest(h, expected)


# ─────────────────────────────────────────────────────────
# Dataclasses
# ─────────────────────────────────────────────────────────


@dataclass
class Team:
    id: str
    name: str
    domain: str | None = None
    llm_config_encrypted: str = ""
    llm_config_version: int = 0
    max_seats: int = 1
    default_user_tier: str = "pro"
    team_credit_balance: int = 0
    team_credit_quota: int = 0
    team_credit_period_start: str | None = None
    tier_credits: dict[str, int] = field(default_factory=lambda: {"pro": 5000, "ultra": 30000})
    status: str = "active"
    created_at: str = ""
    contract_expires_at: str | None = None


@dataclass
class TeamLicense:
    """Durable mirror of a verified on-prem license (licensing §6.1). The in-memory verified token is the
    source of truth; this row persists last-known-good for startup + a degraded fail-open."""

    team_id: str
    license_token: str
    license_id: str
    key_id: str
    issued_at: str
    activated_at: str
    expires_at: str | None = None
    grace_period_days: int = 14
    max_seats: int = 1
    default_user_tier: str = "pro"
    tier_credits: dict[str, int] = field(default_factory=dict)
    features: list[str] = field(default_factory=list)
    deployment_mode: str = "local"
    max_offline_lifetime_days: int | None = None
    last_verified_at: str | None = None
    last_good_health: str | None = None
    instance_fingerprint: str | None = None


@dataclass
class User:
    id: str
    team_id: str
    email: str
    email_verified: bool = False
    display_name: str | None = None
    avatar_url: str | None = None
    password_hash: str | None = None
    oauth_providers: list[str] = field(default_factory=list)
    tier: str = "pro"
    credit_balance: int = 0
    credit_quota: int = 0
    credit_period_start: str | None = None
    locale: str = "en-US"
    timezone: str = "Asia/Shanghai"
    preferences: dict[str, Any] = field(default_factory=dict)
    status: str = "active"
    created_at: str = ""
    last_active_at: str | None = None
    # Sprint β G10 — JWT pwd_v + SSE/WS kick
    pwd_version: int = 0
    session_token_version: int = 0
    last_password_change_at: str | None = None
    # FEATURE_MULTI_TEAM_AUTHZ: current Team projection. ``team_id`` remains
    # the legacy/default Team column during the compatibility window.
    active_team_id: str | None = None


# ─────────────────────────────────────────────────────────
# Store
# ─────────────────────────────────────────────────────────


class SeatGuard(Protocol):
    """Minimal seat-gate surface the ``create_user`` funnel needs (licensing §4.2).

    Declared here — NOT imported from ``bowhead_core.entitlements`` — so core never imports the resolver
    (ARCH-1 import-cycle avoidance). ``EntitlementResolver`` satisfies this structurally; in shadow mode its
    ``assert_can_add_seat`` logs a would-deny and returns (never raises), so wiring it is a no-op until
    ``BOWHEAD_ENTITLEMENT_ENFORCE=1``.
    """

    async def assert_can_add_seat(self, team_id: str | None, *, active_count: int) -> None: ...


def _canonical_workspace_member_role(role: str | None) -> str | None:
    if role is None:
        return None
    r = str(role).strip().lower()
    if r in {"admin", "ws_admin", "owner", "workspace_admin"}:
        return "admin"
    if r in {"member", "user"}:
        return "member"
    if r in {"viewer", "read_only", "readonly"}:
        return "viewer"
    return r or None


async def _validate_team_invite_assignment_in_tx(
    db: Any,
    *,
    invite_id: str,
    assignment_mode: str,
) -> None:
    async with db.execute(
        "SELECT COUNT(*) FROM team_invite_workspace_grants WHERE invite_id=?",
        (invite_id,),
    ) as cur:
        count_row = await cur.fetchone()
    grant_count = int(count_row[0]) if count_row else 0
    if (assignment_mode == "spaces" and not 1 <= grant_count <= 20) or (assignment_mode in {"team_only", "team_only_legacy"} and grant_count != 0):
        raise SpaceGovernanceError(
            "invite_assignment_corrupt",
            {
                "assignment_mode": assignment_mode,
                "grant_count": grant_count,
            },
        )


async def _apply_team_invite_workspace_grants_in_tx(
    db: Any,
    *,
    invite_id: str,
    team_id: str,
    user_id: str,
    now: str,
) -> dict[str, Any]:
    """Apply valid initial Space grants without blocking the Team invitation."""

    async with db.execute(
        "SELECT assignment_mode FROM team_invite_tokens WHERE id=?",
        (invite_id,),
    ) as cur:
        invite = await cur.fetchone()
    if invite is None:
        raise ValueError("invite token invalid / revoked / expired")
    assignment_mode = str(invite["assignment_mode"] or "team_only_legacy")
    await _validate_team_invite_assignment_in_tx(
        db,
        invite_id=invite_id,
        assignment_mode=assignment_mode,
    )
    if assignment_mode != "spaces":
        return {"granted_workspace_count": 0, "skipped_workspace_grants": []}
    async with db.execute(
        "SELECT g.workspace_id,g.role,w.team_id,w.status "
        "FROM team_invite_workspace_grants g "
        "LEFT JOIN workspaces w ON w.id=g.workspace_id "
        "WHERE g.invite_id=? ORDER BY g.workspace_id",
        (invite_id,),
    ) as cur:
        grants = await cur.fetchall()
    granted = 0
    skipped: list[dict[str, str]] = []
    for grant in grants:
        reason: str | None = None
        if grant["team_id"] is None:
            reason = "workspace_missing"
        elif grant["team_id"] != team_id:
            reason = "workspace_team_mismatch"
        elif grant["status"] != "active":
            reason = "workspace_not_active"
        if reason:
            skipped.append(
                {
                    "workspace_id": str(grant["workspace_id"]),
                    "reason": reason,
                }
            )
            continue
        await db.execute(
            "INSERT INTO workspace_members(workspace_id,user_id,role,invited_by,"
            "joined_at,last_active_at,status) VALUES (?,?,?,?,?,?,'active') "
            "ON CONFLICT(workspace_id,user_id) DO UPDATE SET "
            "role=CASE WHEN workspace_members.status='active' "
            "THEN workspace_members.role ELSE excluded.role END,"
            "status='active'",
            (
                grant["workspace_id"],
                user_id,
                grant["role"],
                None,
                now,
                now,
            ),
        )
        granted += 1
    return {
        "granted_workspace_count": granted,
        "skipped_workspace_grants": skipped,
    }


async def _validate_invite_in_tx(
    db: Any,
    *,
    invite_type: str,
    invite_id: str,
    team_id: str,
    email_norm: str,
    now: str,
    workspace_id: str | None,
    workspace_role: str | None,
    token_version: int | None = None,
) -> str | None:
    """Validate an invite row (and its workspace, for workspace invites) inside an open
    BEGIN IMMEDIATE transaction on ``db``. Returns the canonical role carried by the
    invite: the workspace role for a workspace invite, the team role for a team invite.
    Raises ValueError on any invalid/revoked/consumed/expired/mismatched state.

    Shared by ``create_invited_user_atomic`` and ``accept_invite_for_user_atomic`` so the
    two paths cannot drift (a future tightening applies to both at once).
    """
    if invite_type == "team":
        async with db.execute(
            "SELECT id,team_id,team_role,invited_email,status,expires_at,token_version,assignment_mode FROM team_invite_tokens WHERE id = ?",
            (invite_id,),
        ) as cur:
            invite_row = await cur.fetchone()
        if (
            invite_row is None
            or invite_row["team_id"] != team_id
            or invite_row["invited_email"] != email_norm
            or invite_row["status"] != "active"
            or invite_row["expires_at"] < now
            or (token_version is not None and invite_row["token_version"] != token_version)
        ):
            raise ValueError("invite token invalid / revoked / expired")
        # Fix (concept/term audit batch): team branch formerly returned None, inserted by both callers into
        # team_members.role, failing NOT NULL and rolling back transactions, breaking team-invite acceptance entirely.
        team_role = str(invite_row["team_role"] or "member").strip().lower()
        if team_role not in {"team_admin", "audit_admin", "member"}:
            raise ValueError(f"invalid team_role on invite: {team_role}")
        await _validate_team_invite_assignment_in_tx(
            db,
            invite_id=invite_id,
            assignment_mode=str(invite_row["assignment_mode"] or "team_only_legacy"),
        )
        return team_role
    if invite_type == "workspace":
        if not workspace_id:
            raise ValueError("workspace_id required for workspace invite")
        async with db.execute(
            "SELECT id,workspace_id,team_id,role,invited_email,status,expires_at,token_version FROM invite_tokens WHERE id = ?",
            (invite_id,),
        ) as cur:
            invite_row = await cur.fetchone()
        canonical_role = (
            _canonical_workspace_member_role(
                workspace_role or (invite_row["role"] if invite_row else None),
            )
            or "member"
        )
        if (
            invite_row is None
            or invite_row["workspace_id"] != workspace_id
            or (invite_row["team_id"] is not None and invite_row["team_id"] != team_id)
            or invite_row["invited_email"] != email_norm
            or invite_row["status"] != "active"
            or invite_row["expires_at"] < now
            or (token_version is not None and invite_row["token_version"] != token_version)
        ):
            raise ValueError("invite token invalid / revoked / expired")
        async with db.execute(
            "SELECT team_id FROM workspaces WHERE id = ?",
            (workspace_id,),
        ) as cur:
            ws_row = await cur.fetchone()
        if ws_row is None:
            raise ValueError(f"workspace not found: {workspace_id}")
        if ws_row["team_id"] != team_id:
            raise ValueError("workspace_members user/workspace team mismatch")
        return canonical_role
    raise ValueError(f"unknown invite_type: {invite_type!r}")


async def _stable_credit_ledger_row_exists_in_tx(
    db: aiosqlite.Connection,
    *,
    ledger_id: str,
    user_id: str,
    team_id: str,
    cost_credits: int,
    turn_id: str | None,
    workspace_id: str | None,
    tokens_input: int,
    tokens_output: int,
    reason: str,
    rate_card_version: str,
    model_usage: tuple[ModelUsageLine, ...] = (),
) -> bool:
    """Point-read one stable ledger identity without creating or repairing it."""

    if not db.in_transaction:
        raise RuntimeError("credit ledger validation requires an active transaction")

    async with db.execute(
        "SELECT user_id,team_id,workspace_id,turn_id,delta,reason,tokens_input,tokens_output,rate_card_version FROM credit_ledger WHERE id=?",
        (ledger_id,),
    ) as cur:
        existing = await cur.fetchone()
    expected = (
        user_id,
        team_id,
        workspace_id,
        turn_id,
        -cost_credits,
        reason,
        tokens_input,
        tokens_output,
        rate_card_version,
    )
    if existing is not None:
        if tuple(existing) != expected:
            raise ValueError("credit_ledger_identity_conflict")
        if model_usage:
            async with db.execute(
                "SELECT provider,model,tokens_input,tokens_cached,tokens_output,rate_input,rate_cached,rate_output,weighted_tokens "
                "FROM credit_model_usage WHERE ledger_id=? ORDER BY line_no",
                (ledger_id,),
            ) as cur:
                actual_lines = await cur.fetchall()
            expected_lines = [
                (
                    line.provider,
                    line.model,
                    line.usage.input,
                    line.usage.cached_input,
                    line.usage.output,
                    str(line.usage.rates.input),
                    str(line.usage.rates.cached_input),
                    str(line.usage.rates.output),
                    str(line.usage.weighted_tokens()),
                )
                for line in model_usage
            ]
            if [tuple(line) for line in actual_lines] != expected_lines:
                raise ValueError("credit_model_usage_identity_conflict")
        return True
    return False


async def _deduct_credit_with_stable_id_in_tx(
    db: aiosqlite.Connection,
    *,
    ledger_id: str,
    user_id: str,
    team_id: str,
    cost_credits: int,
    turn_id: str | None,
    workspace_id: str | None,
    tokens_input: int,
    tokens_output: int,
    reason: str,
    rate_card_version: str,
    model_usage: tuple[ModelUsageLine, ...] = (),
    cumulative_v2: bool = False,
) -> tuple[Literal["confirmed_applied", "confirmed_not_applied"], str]:
    """Deduct balances and write one exact ledger row in the caller's BEGIN IMMEDIATE.

    这是**已交付用量的结算**，不是准入：余额不足以覆盖本笔时照实扣、允许透支到负数。
    准入只在 preflight（`balance <= 0` ⇒ 402）。反过来「不足就整笔不扣」会让余额永远停在
    (0, 单轮成本) 之间，preflight 永远放行 ⇒ 此后每一轮都白跑（2026-09-19 实测复现）。
    """

    if rate_card_version == "v2" and not cumulative_v2 and credits_for_model_lines(model_usage) != cost_credits:
        raise ValueError("v2 credit amount does not match model usage")
    if await _stable_credit_ledger_row_exists_in_tx(
        db,
        ledger_id=ledger_id,
        user_id=user_id,
        team_id=team_id,
        cost_credits=cost_credits,
        turn_id=turn_id,
        workspace_id=workspace_id,
        tokens_input=tokens_input,
        tokens_output=tokens_output,
        reason=reason,
        rate_card_version=rate_card_version,
        model_usage=model_usage,
    ):
        return "confirmed_applied", ""

    async with db.execute(
        "SELECT credit_balance FROM users WHERE id=?",
        (user_id,),
    ) as cur:
        user_row = await cur.fetchone()
    # Only absent user rows now skip deduction; retain legacy reason code and caller mappings.
    if user_row is None:
        return "confirmed_not_applied", "user_balance_zero"

    async with db.execute(
        "SELECT team_credit_balance FROM teams WHERE id=?",
        (team_id,),
    ) as cur:
        team_row = await cur.fetchone()
    if team_row is None:
        return "confirmed_not_applied", "team_pool_zero"

    user_update = await db.execute(
        "UPDATE users SET credit_balance=credit_balance-? WHERE id=? AND credit_balance=?",
        (cost_credits, user_id, int(user_row[0])),
    )
    if user_update.rowcount != 1:
        raise RuntimeError("user credit balance changed inside write transaction")
    team_update = await db.execute(
        "UPDATE teams SET team_credit_balance=team_credit_balance-? WHERE id=? AND team_credit_balance=?",
        (cost_credits, team_id, int(team_row[0])),
    )
    if team_update.rowcount != 1:
        raise RuntimeError("team credit balance changed inside write transaction")
    await db.execute(
        "INSERT INTO credit_ledger(id,user_id,team_id,workspace_id,turn_id,"
        "delta,reason,tokens_input,tokens_output,created_at,rate_card_version) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            ledger_id,
            user_id,
            team_id,
            workspace_id,
            turn_id,
            -cost_credits,
            reason,
            tokens_input,
            tokens_output,
            _now(),
            rate_card_version,
        ),
    )
    await _insert_credit_model_usage_in_tx(db, ledger_id=ledger_id, model_usage=model_usage)
    return "confirmed_applied", ""


async def _insert_credit_model_usage_in_tx(
    db: aiosqlite.Connection,
    *,
    ledger_id: str,
    model_usage: tuple[ModelUsageLine, ...],
) -> None:
    for line_no, line in enumerate(model_usage, start=1):
        usage = line.usage
        await db.execute(
            "INSERT INTO credit_model_usage(ledger_id,line_no,provider,model,tokens_input,tokens_cached,tokens_output,"
            "rate_input,rate_cached,rate_output,weighted_tokens) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                ledger_id,
                line_no,
                line.provider,
                line.model,
                usage.input,
                usage.cached_input,
                usage.output,
                str(usage.rates.input),
                str(usage.rates.cached_input),
                str(usage.rates.output),
                str(usage.weighted_tokens()),
            ),
        )


class UserStore:
    def __init__(
        self,
        db_path: str | Path,
        *,
        terminal_auth_fence_enabled: bool = False,
    ) -> None:
        self.db_path = str(db_path)
        self._terminal_auth_fence_enabled = terminal_auth_fence_enabled
        # Licensing §4.2 two-phase wiring: the resolver needs UserStore, the funnel needs the resolver.
        # Injected via set_seat_guard() in lifespan AFTER initialize(). None ⇒ create_user skips the seat check.
        self._seat_guard: SeatGuard | None = None
        self.account_deletion_recorder: Callable[[AccountDeletion], Awaitable[None]] | None = None

    def set_terminal_auth_fence_enabled(self, enabled: bool) -> None:
        """Enable only after Terminal Protocol schema/readiness validation."""

        self._terminal_auth_fence_enabled = bool(enabled)

    async def _bump_team_membership_epoch_conn(
        self,
        db: aiosqlite.Connection,
        team_id: str,
        *,
        now: str | None = None,
    ) -> None:
        if not self._terminal_auth_fence_enabled:
            return
        from bowhead_core.terminal_auth_epochs import bump_terminal_auth_epoch
        from bowhead_core.terminal_protocol import AuthFenceScopeKind

        await bump_terminal_auth_epoch(
            db,
            scope_kind=AuthFenceScopeKind.TEAM_MEMBERSHIP,
            team_id=team_id,
            scope_ref=team_id,
            now=now,
        )

    async def _bump_user_account_epoch_conn(
        self,
        db: aiosqlite.Connection,
        user_id: str,
        *,
        now: str | None = None,
    ) -> None:
        if not self._terminal_auth_fence_enabled:
            return
        from bowhead_core.terminal_auth_epochs import bump_terminal_auth_epoch
        from bowhead_core.terminal_protocol import AuthFenceScopeKind

        await bump_terminal_auth_epoch(
            db,
            scope_kind=AuthFenceScopeKind.USER_ACCOUNT,
            team_id="__global__",
            scope_ref=user_id,
            now=now,
        )

    @staticmethod
    async def _workspace_team_id_conn(
        db: aiosqlite.Connection,
        workspace_id: str,
    ) -> str:
        async with db.execute(
            "SELECT team_id FROM workspaces WHERE id=?",
            (workspace_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None or not row[0]:
            raise ValueError("workspace team identity is missing")
        return str(row[0])

    def set_seat_guard(self, guard: SeatGuard | None) -> None:
        """Inject the seat gate (the EntitlementResolver) post-construction (§4.2/ARCH-1)."""
        self._seat_guard = guard

    async def initialize(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            await db.executescript(SCHEMA)
            await self._migrate_credit_exhaustion_code_check(db)
            # Idempotent column addition (SQLite lacks column-level IF NOT EXISTS)
            for sql in (
                # P4.5.20
                "ALTER TABLE teams ADD COLUMN created_by_user_id TEXT",
                # Sprint α (0.35.0) — G3 jti + G4 datarc_member_uid
                "ALTER TABLE users ADD COLUMN verify_token_jti TEXT",
                "ALTER TABLE users ADD COLUMN datarc_member_uid TEXT",
                # Sprint β (0.36.0) — G10 pwd_v JWT (P0-S-3) + session kick (P1-O-1) + lock (P2-S-5)
                "ALTER TABLE users ADD COLUMN pwd_version INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE users ADD COLUMN session_token_version INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE users ADD COLUMN password_change_in_progress TEXT",
                "ALTER TABLE users ADD COLUMN last_password_change_at TEXT",
                # Sprint β G7 — Fernet OCC version (P0-T-7 fix)
                "ALTER TABLE teams ADD COLUMN llm_config_version INTEGER NOT NULL DEFAULT 0",
                # Sprint β G2 — workspace_members extension (D-1=B + F-9)
                "ALTER TABLE workspace_members ADD COLUMN last_active_at TEXT",
                "ALTER TABLE workspace_members ADD COLUMN cached_name TEXT",
                # cca P2 — per-op tool description overrides (LLM-proposed/user-edited)
                "ALTER TABLE team_tool_source ADD COLUMN tool_overrides_json TEXT NOT NULL DEFAULT '{}'",
                # email-config sprint: team SMTP (BYO, Fernet) + env fallback (mirrors llm_config column pair)
                "ALTER TABLE teams ADD COLUMN email_config_encrypted TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE teams ADD COLUMN email_config_version INTEGER NOT NULL DEFAULT 0",
                # email-config: outbox carries team_id (NULL = env fallback, legacy behavior preserved)
                "ALTER TABLE smtp_outbox ADD COLUMN team_id TEXT",
                "ALTER TABLE smtp_outbox ADD COLUMN html_body TEXT",
                "ALTER TABLE smtp_outbox ADD COLUMN public_entry_id TEXT",
                "ALTER TABLE smtp_outbox ADD COLUMN brand_revision_id TEXT",
                "ALTER TABLE smtp_outbox ADD COLUMN branding_snapshot TEXT",
                # invite landing / protected-route recovery — OAuth returns to the original safe path.
                "ALTER TABLE oauth_state ADD COLUMN next_path TEXT",
                # Multi-public-entry OAuth continuity.
                "ALTER TABLE oauth_state ADD COLUMN public_entry_id TEXT",
                "ALTER TABLE mcp_oauth_pending ADD COLUMN public_entry_id TEXT",
                # invite-only activation — additive, flag-gated identity schema.
                "ALTER TABLE oauth_state ADD COLUMN flow_kind TEXT NOT NULL DEFAULT 'normal'",
                "ALTER TABLE oauth_state ADD COLUMN invite_intent_hash TEXT",
                "ALTER TABLE invite_tokens ADD COLUMN token_version INTEGER NOT NULL DEFAULT 1",
                "ALTER TABLE invite_tokens ADD COLUMN consumed_by_user_id TEXT",
                "ALTER TABLE invite_tokens ADD COLUMN satisfied_at TEXT",
                "ALTER TABLE team_invite_tokens ADD COLUMN token_version INTEGER NOT NULL DEFAULT 1",
                "ALTER TABLE team_invite_tokens ADD COLUMN consumed_by_user_id TEXT",
                "ALTER TABLE team_invite_tokens ADD COLUMN satisfied_at TEXT",
                # Space administration M0/M1 — additive invitation assignment contract.
                "ALTER TABLE team_invite_tokens ADD COLUMN assignment_mode TEXT NOT NULL DEFAULT 'team_only_legacy'",
                "ALTER TABLE team_invite_tokens ADD COLUMN assignment_revision INTEGER NOT NULL DEFAULT 1",
                "ALTER TABLE team_invite_tokens ADD COLUMN public_entry_id TEXT",
                # Space administration M0 expand phase. Contract migration adds
                # required triggers/unique index only after the conflict manifest
                # has been reviewed against production data.
                "ALTER TABLE workspaces ADD COLUMN name_key TEXT",
                "ALTER TABLE workspaces ADD COLUMN revision INTEGER NOT NULL DEFAULT 1",
                # skill-multiruntime S1 (G5): team recipe upload risk report (language/keywords/shebang)
                "ALTER TABLE team_skill ADD COLUMN risk_report_json TEXT",
                # credit-billing gap 8: record rate-card version for every deduction, making historical accounts explainable
                # (model §5: billing admins should understand why each deduction costs this amount).
                # Legacy NULL = unrecorded before v1, semantically v1 (v1 is byte-equivalent to the former formula).
                "ALTER TABLE credit_ledger ADD COLUMN rate_card_version TEXT",
                # FEATURE_MULTI_TEAM_AUTHZ: users.team_id remains the legacy
                # default Team; this records the explicit current Team.
                "ALTER TABLE users ADD COLUMN active_team_id TEXT",
                # skill-team-governance P1b: dual approval slots (see schema comments; nullable/defaulted, no schema downgrade on rollback)
                "ALTER TABLE team_skill ADD COLUMN approval TEXT NOT NULL DEFAULT 'approved'",
                "ALTER TABLE team_skill ADD COLUMN approved_sha256 TEXT",
                "ALTER TABLE team_skill ADD COLUMN approved_by TEXT",
                "ALTER TABLE team_skill ADD COLUMN approved_at TEXT",
                "ALTER TABLE team_skill ADD COLUMN rejected_reason TEXT",
                "ALTER TABLE team_skill ADD COLUMN rejected_at TEXT",
                "ALTER TABLE team_skill ADD COLUMN candidate_path TEXT",
                "ALTER TABLE team_skill ADD COLUMN candidate_submitted_at TEXT",
                "ALTER TABLE team_skill ADD COLUMN candidate_sha256 TEXT",
                "ALTER TABLE team_skill ADD COLUMN candidate_state TEXT",
                "ALTER TABLE team_skill ADD COLUMN candidate_rejected_reason TEXT",
                "ALTER TABLE team_skill ADD COLUMN submitted_by TEXT",
            ):
                try:  # noqa: SIM105
                    await db.execute(sql)
                except Exception:  # noqa: S110
                    pass  # already exists
            # Replace old single-team triggers in existing SQLite databases.
            await db.execute("DROP TRIGGER IF EXISTS team_members_single_team_check_insert")
            await db.execute("DROP TRIGGER IF EXISTS workspace_members_same_team_check_insert")
            await db.executescript(
                """
                CREATE TRIGGER IF NOT EXISTS workspace_members_same_team_check_insert
                  BEFORE INSERT ON workspace_members
                  WHEN (SELECT team_id FROM workspaces WHERE id = NEW.workspace_id) IS NOT NULL
                   AND (SELECT team_id FROM workspaces WHERE id = NEW.workspace_id)
                       NOT IN (SELECT team_id FROM team_members WHERE user_id = NEW.user_id AND status = 'active')
                   AND (SELECT team_id FROM users WHERE id = NEW.user_id)
                       != (SELECT team_id FROM workspaces WHERE id = NEW.workspace_id)
                  BEGIN SELECT RAISE(ABORT, 'workspace_members requires active team membership'); END;
                """
            )
            # Existing users get a membership matching their legacy default.
            await db.execute(
                "INSERT OR IGNORE INTO team_members(id,user_id,team_id,role,status,created_at) "
                "SELECT 'om_backfill_' || id, id, team_id, 'member', 'active', created_at "
                "FROM users WHERE status = 'active'"
            )
            now = _now()
            for table in ("invite_tokens", "team_invite_tokens"):
                await db.execute(
                    f"UPDATE {table} SET status='expired' WHERE status='active' AND expires_at < ?",
                    (now,),
                )
            await db.execute(
                "WITH ranked AS ("
                "SELECT id,ROW_NUMBER() OVER (PARTITION BY team_id,invited_email "
                "ORDER BY expires_at DESC,created_at DESC,id DESC) AS position "
                "FROM team_invite_tokens WHERE status='active') "
                "UPDATE team_invite_tokens SET status='revoked',revoked_at=? "
                "WHERE id IN (SELECT id FROM ranked WHERE position > 1)",
                (now,),
            )
            await db.execute(
                "WITH ranked AS ("
                "SELECT id,ROW_NUMBER() OVER (PARTITION BY workspace_id,invited_email "
                "ORDER BY expires_at DESC,created_at DESC,id DESC) AS position "
                "FROM invite_tokens WHERE status='active') "
                "UPDATE invite_tokens SET status='revoked',revoked_at=? "
                "WHERE id IN (SELECT id FROM ranked WHERE position > 1)",
                (now,),
            )
            await db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_team_invite_active ON team_invite_tokens(team_id,invited_email) WHERE status='active'",
            )
            await db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_space_invite_active ON invite_tokens(workspace_id,invited_email) WHERE status='active'",
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS ix_workspaces_team_status_name ON workspaces(team_id,status,name_key,id)",
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS ix_team_invite_workspace_grants_workspace ON team_invite_workspace_grants(workspace_id,invite_id)",
            )
            migration = await db.execute(
                "INSERT OR IGNORE INTO user_store_migrations(name,applied_at) VALUES ('identity_membership_split_v1',?)",
                (_now(),),
            )
            if migration.rowcount == 1:
                # Historical ``deleted`` is ambiguous (team removal vs global
                # account deletion). Preserve those rows as an inactive,
                # fail-closed state for manual review. The marker makes this a
                # one-shot migration so future explicit global deletions retain
                # their current lifecycle semantics.
                await db.execute(
                    "UPDATE users SET status='legacy_deleted' WHERE status='deleted'",
                )
            await db.commit()

    async def _migrate_credit_exhaustion_code_check(
        self,
        db: aiosqlite.Connection,
    ) -> None:
        """Canonicalize the pre-team credit event enum without losing rows."""
        cur = await db.execute(
            "SELECT sql FROM sqlite_schema WHERE type='table' AND name='credit_exhaustion_events'",
        )
        row = await cur.fetchone()
        if row is None:
            return
        existing_sql = row[0] or ""
        if "'team_pool_exhausted'" in existing_sql and "'org_pool_exhausted'" not in existing_sql:
            return

        await db.execute("BEGIN IMMEDIATE")
        try:
            await db.execute("DROP TABLE IF EXISTS credit_exhaustion_events_new")
            await db.execute("""
                CREATE TABLE credit_exhaustion_events_new (
                  id TEXT PRIMARY KEY,
                  team_id TEXT NOT NULL,
                  user_id TEXT NOT NULL,
                  workspace_id TEXT,
                  code TEXT NOT NULL CHECK(code IN (
                    'user_credit_exhausted',
                    'team_pool_exhausted',
                    'credit_deduct_failed'
                  )),
                  credit_balance INTEGER,
                  credit_quota INTEGER,
                  team_credit_balance INTEGER,
                  team_credit_quota INTEGER,
                  turn_id TEXT,
                  created_at TEXT NOT NULL
                )
            """)
            await db.execute("""
                INSERT INTO credit_exhaustion_events_new (
                  id, team_id, user_id, workspace_id, code,
                  credit_balance, credit_quota,
                  team_credit_balance, team_credit_quota,
                  turn_id, created_at
                )
                SELECT
                  id, team_id, user_id, workspace_id,
                  CASE
                    WHEN code = 'org_pool_exhausted' THEN 'team_pool_exhausted'
                    ELSE code
                  END,
                  credit_balance, credit_quota,
                  team_credit_balance, team_credit_quota,
                  turn_id, created_at
                FROM credit_exhaustion_events
            """)
            await db.execute("DROP TABLE credit_exhaustion_events")
            await db.execute(
                "ALTER TABLE credit_exhaustion_events_new RENAME TO credit_exhaustion_events",
            )
            await db.execute(
                "CREATE INDEX idx_credit_exhaustion_team_user_at ON credit_exhaustion_events(team_id, user_id, created_at DESC)",
            )
            await db.execute(
                "CREATE INDEX idx_credit_exhaustion_team_code_at ON credit_exhaustion_events(team_id, code, created_at DESC)",
            )
            await db.execute("COMMIT")
        except Exception:
            await db.execute("ROLLBACK")
            raise

    # ─── team ───────────────────────────────────

    async def create_team(
        self,
        *,
        name: str,
        domain: str | None = None,
        max_seats: int = 1,
        tier_credits: dict[str, int] | None = None,
    ) -> Team:
        tc = tier_credits or {"pro": 5000, "ultra": 30000}
        # Default team pool = ultra tier × max_seats (admin may later add through /credit-grant).
        # Without initial pool, chat fails (team_pool_exhausted), a poor experience for new enterprise users.
        default_quota = int(tc.get("ultra", 30000)) * max(1, max_seats)
        team = Team(
            id=_id("team"),
            name=name,
            domain=domain,
            max_seats=max_seats,
            tier_credits=tc,
            team_credit_balance=default_quota,
            team_credit_quota=default_quota,
            team_credit_period_start=_now(),
            created_at=_now(),
        )
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                # Persist team_credit_period_start too: already calculated above; omitting it leaves new teams'
                # period start NULL, unknown until first reset, while monthly cron uses
                # this column to determine which teams still need this month's quota.
                "INSERT INTO teams(id,name,domain,max_seats,default_user_tier,"
                "team_credit_balance,team_credit_quota,team_credit_period_start,"
                "tier_credits_json,status,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    team.id,
                    team.name,
                    team.domain,
                    team.max_seats,
                    team.default_user_tier,
                    team.team_credit_balance,
                    team.team_credit_quota,
                    team.team_credit_period_start,
                    json.dumps(team.tier_credits),
                    team.status,
                    team.created_at,
                ),
            )
            await db.commit()
        return team

    async def bootstrap_local_installation(
        self,
        *,
        team_license: TeamLicense,
        team_name: str,
        email: str,
        password: str,
        display_name: str | None = None,
        public_entry_store: PublicEntryStore | None = None,
        public_base_url: str | None = None,
        public_entry_verification_method: str = "trusted_proxy_bootstrap",
    ) -> User:
        """Atomically create the local team, license, administrator, and optional public entry.

        The caller verifies the signed license before this write. This method deliberately has no
        seat-guard call: the first user is created in the same transaction as the license which
        grants its first seat.
        """
        if (public_entry_store is None) != (public_base_url is None):
            raise ValueError("public entry store and base URL must be provided together")
        if public_entry_store is not None and Path(public_entry_store.db_path).resolve() != Path(self.db_path).resolve():
            raise ValueError("public entry registry must share the user database")
        now = _now()
        email_norm = email.lower().strip()
        licensed_tier_credits = dict(team_license.tier_credits)
        # An empty signed table is a license-generation omission, not a valid
        # instruction to make the first administrator unable to use Bowhead.
        # Keep the raw payload in team_license for audit/re-issue, while the
        # operational team mirrors the same safe defaults as create_team().
        tier_credits = licensed_tier_credits or {"pro": 5000, "ultra": 30000}
        quota = int(tier_credits.get(team_license.default_user_tier, 0))
        team_quota = max(0, int(tier_credits.get("ultra", quota))) * max(1, team_license.max_seats)
        user = User(
            id=_id("usr"),
            team_id=team_license.team_id,
            email=email_norm,
            email_verified=True,
            display_name=display_name,
            password_hash=hash_password(password),
            tier=team_license.default_user_tier,
            credit_balance=quota,
            credit_quota=quota,
            credit_period_start=now,
            created_at=now,
        )
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                # Re-activation guard: existing license row means local installation initialized (renewals/replacements use
                # admin_billing upsert, not bootstrap). This guard was lost when allowing
                # preexisting teams for migration; second calls violated team_license UNIQUE,
                # crashing transactions instead of clean rejection.
                async with db.execute("SELECT 1 FROM team_license WHERE team_id = ?", (team_license.team_id,)) as cur:
                    if await cur.fetchone() is not None:
                        raise ValueError("local installation is already initialized")
                async with db.execute("SELECT COUNT(*) FROM teams") as cur:
                    existing_teams = int((await cur.fetchone())[0])
                if existing_teams:
                    async with db.execute("SELECT 1 FROM teams WHERE id = ?", (team_license.team_id,)) as cur:
                        if await cur.fetchone() is None:
                            raise ValueError("license team does not exist")
                else:
                    await db.execute(
                        "INSERT INTO teams(id,name,max_seats,default_user_tier,team_credit_balance,team_credit_quota,"
                        "team_credit_period_start,tier_credits_json,status,created_at,created_by_user_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            team_license.team_id,
                            team_name,
                            team_license.max_seats,
                            team_license.default_user_tier,
                            team_quota,
                            team_quota,
                            now,
                            json.dumps(tier_credits),
                            "active",
                            now,
                            user.id,
                        ),
                    )
                await db.execute(
                    "INSERT INTO users(id,team_id,email,email_verified,display_name,password_hash,oauth_providers_json,tier,"
                    "credit_balance,credit_quota,credit_period_start,locale,timezone,preferences_json,status,created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        user.id,
                        user.team_id,
                        user.email,
                        1,
                        user.display_name,
                        user.password_hash,
                        "[]",
                        user.tier,
                        user.credit_balance,
                        user.credit_quota,
                        user.credit_period_start,
                        user.locale,
                        user.timezone,
                        json.dumps(user.preferences),
                        user.status,
                        user.created_at,
                    ),
                )
                await db.execute(
                    "INSERT INTO team_members(id,user_id,team_id,role,status,created_at) VALUES (?,?,?,?,?,?)",
                    (_id("om"), user.id, team_license.team_id, "team_admin", "active", now),
                )
                await db.execute(
                    "INSERT INTO team_license(team_id,license_token,license_id,key_id,issued_at,expires_at,grace_period_days,"
                    "max_seats,default_user_tier,tier_credits_json,features_json,deployment_mode,max_offline_lifetime_days,"
                    "activated_at,last_verified_at,last_good_health) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        team_license.team_id,
                        team_license.license_token,
                        team_license.license_id,
                        team_license.key_id,
                        team_license.issued_at,
                        team_license.expires_at,
                        team_license.grace_period_days,
                        team_license.max_seats,
                        team_license.default_user_tier,
                        json.dumps(licensed_tier_credits),
                        json.dumps(team_license.features),
                        team_license.deployment_mode,
                        team_license.max_offline_lifetime_days,
                        team_license.activated_at,
                        team_license.last_verified_at,
                        team_license.last_good_health,
                    ),
                )
                if public_entry_store is not None and public_base_url is not None:
                    await public_entry_store.create_bootstrap_active(
                        team_id=team_license.team_id,
                        base_url=public_base_url,
                        actor_id=user.id,
                        verification_method=public_entry_verification_method,
                        connection=db,
                    )
                await db.commit()
            except Exception:
                await db.rollback()
                raise
        return user

    async def list_active_team_ids(self, *, limit: int = 500, offset: int = 0) -> list[str]:
        """Return active team ids for lightweight background maintenance jobs."""

        capped_limit = max(1, min(int(limit), 5_000))
        safe_offset = max(0, int(offset))
        async with (
            aiosqlite.connect(self.db_path) as db,
            db.execute(
                "SELECT id FROM teams WHERE status = 'active' ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (capped_limit, safe_offset),
            ) as cur,
        ):
            rows = await cur.fetchall()
        return [str(row[0]) for row in rows]

    async def update_team_llm_config(self, team_id: str, config: dict[str, Any]) -> None:
        """P4.5.2: encrypt + 存 team LLM provider config (BYO LLM)."""
        from bowhead_core.llm_config_crypto import encrypt_llm_config

        encrypted = encrypt_llm_config(config)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE teams SET llm_config_encrypted = ?,     llm_config_version = llm_config_version + 1 WHERE id = ?",
                (encrypted, team_id),
            )
            await db.commit()

    async def update_team_llm_config_if_version(
        self,
        team_id: str,
        config: dict[str, Any],
        *,
        expected_version: int,
    ) -> bool:
        """Atomically save team LLM config when its OCC version is unchanged."""
        from bowhead_core.llm_config_crypto import encrypt_llm_config

        encrypted = encrypt_llm_config(config)
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "UPDATE teams SET llm_config_encrypted = ?, llm_config_version = llm_config_version + 1 WHERE id = ? AND llm_config_version = ?",
                (encrypted, team_id, expected_version),
            )
            await db.commit()
        return cursor.rowcount == 1

    async def get_team_llm_config_decrypted(self, team_id: str) -> dict[str, Any]:
        """P4.5.2: read + decrypt. 返 {} 如果没配."""
        from bowhead_core.llm_config_crypto import decrypt_llm_config

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT llm_config_encrypted FROM teams WHERE id = ?",
                (team_id,),
            ) as cur:
                row = await cur.fetchone()
        if not row or not row["llm_config_encrypted"]:
            return {}
        return decrypt_llm_config(row["llm_config_encrypted"])

    async def get_team_llm_config_with_version(self, team_id: str) -> tuple[dict[str, Any], int]:
        """Read decrypted team LLM config with the OCC/cache version."""
        from bowhead_core.llm_config_crypto import decrypt_llm_config

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT llm_config_encrypted, llm_config_version FROM teams WHERE id = ?",
                (team_id,),
            ) as cur:
                row = await cur.fetchone()
        if not row:
            return {}, 0
        stored = row["llm_config_encrypted"] or ""
        cfg = decrypt_llm_config(stored) if stored else {}
        return cfg, int(row["llm_config_version"] or 0)

    # ─── Team email (SMTP) config: email-config sprint, mirrors llm_config pair ───

    async def update_team_email_config(self, team_id: str, config: dict[str, Any]) -> None:
        """encrypt(Fernet, 复用 llm_config_crypto)+ 存 team SMTP config。"""
        from bowhead_core.llm_config_crypto import encrypt_llm_config

        encrypted = encrypt_llm_config(config)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE teams SET email_config_encrypted = ?,     email_config_version = email_config_version + 1 WHERE id = ?",
                (encrypted, team_id),
            )
            await db.commit()

    async def clear_team_email_config(self, team_id: str) -> None:
        """清 team SMTP config(回落 env)。version+1 让缓存失效。"""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE teams SET email_config_encrypted = '',     email_config_version = email_config_version + 1 WHERE id = ?",
                (team_id,),
            )
            await db.commit()

    async def get_team_email_config_decrypted(self, team_id: str) -> dict[str, Any]:
        """read + decrypt team SMTP config。返 {} 如果没配。"""
        from bowhead_core.llm_config_crypto import decrypt_llm_config

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT email_config_encrypted FROM teams WHERE id = ?",
                (team_id,),
            ) as cur:
                row = await cur.fetchone()
        if not row or not row["email_config_encrypted"]:
            return {}
        return decrypt_llm_config(row["email_config_encrypted"])

    async def get_team_email_config_with_version(
        self,
        team_id: str,
    ) -> tuple[dict[str, Any], int]:
        """read decrypted team SMTP config + OCC/cache version。"""
        from bowhead_core.llm_config_crypto import decrypt_llm_config

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT email_config_encrypted, email_config_version FROM teams WHERE id = ?",
                (team_id,),
            ) as cur:
                row = await cur.fetchone()
        if not row:
            return {}, 0
        stored = row["email_config_encrypted"] or ""
        cfg = decrypt_llm_config(stored) if stored else {}
        return cfg, int(row["email_config_version"] or 0)

    async def list_teams_with_pending_embed_reindex(
        self,
    ) -> list[tuple[str, dict[str, Any], int, dict[str, Any]]]:
        """Return team configs with kb.pending_embed for gateway startup recovery."""
        from bowhead_core.llm_config_crypto import decrypt_llm_config

        out: list[tuple[str, dict[str, Any], int, dict[str, Any]]] = []
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT id, llm_config_encrypted, llm_config_version FROM teams WHERE llm_config_encrypted != ''",
            ) as cur:
                rows = await cur.fetchall()
        for row in rows:
            stored = row["llm_config_encrypted"] or ""
            try:
                cfg = decrypt_llm_config(stored) if stored else {}
            except Exception:  # noqa: S112
                continue
            pending = (cfg.get("kb") or {}).get("pending_embed") or {}
            if isinstance(pending, dict) and pending.get("task_id"):
                out.append(
                    (
                        str(row["id"]),
                        cfg,
                        int(row["llm_config_version"] or 0),
                        pending,
                    )
                )
        return out

    # ─── conversational capability authoring: team tool sources (PRD §8) ───

    async def create_team_tool_source(self, src: TeamToolSource) -> TeamToolSource:
        """Insert a new team tool source. Enforces (team_id, name) uniqueness via BEGIN IMMEDIATE (TOCTOU)."""
        prompt_cache = validate_mcp_prompt_cache(src.prompts_schema)
        if prompt_cache and src.kind != "mcp":
            raise ValueError("MCP Prompt metadata requires an MCP source")
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            async with db.execute(
                "SELECT 1 FROM team_tool_source WHERE team_id=? AND name=?",
                (src.team_id, src.name),
            ) as cur:
                if await cur.fetchone():
                    await db.rollback()
                    raise ValueError(f"tool source name already exists in team: {src.name!r}")
            async with db.execute("PRAGMA table_info(team_tool_source)") as cursor:
                columns = {str(row[1]) for row in await cursor.fetchall()}
            prompt_cache_ready = "prompts_schema_json" in columns
            if prompt_cache and not prompt_cache_ready:
                await db.rollback()
                raise RuntimeError("mcp_prompt_cache_schema_unavailable")
            names = [
                "id",
                "team_id",
                "name",
                "kind",
                "spec_url",
                "openapi_spec_json",
                "selected_op_ids_json",
                "base_url",
                "auth_type",
                "auth_ref",
                "tools_schema_json",
                "tool_overrides_json",
                "visibility",
                "created_by",
                "status",
                "created_at",
                "updated_at",
            ]
            values: list[Any] = [
                src.id,
                src.team_id,
                src.name,
                src.kind,
                src.spec_url,
                src.openapi_spec_json,
                json.dumps(src.selected_op_ids),
                src.base_url,
                src.auth_type,
                src.auth_ref,
                json.dumps(src.tools_schema, ensure_ascii=False),
                json.dumps(src.tool_overrides, ensure_ascii=False),
                src.visibility,
                src.created_by,
                src.status,
                src.created_at,
                src.updated_at,
            ]
            if prompt_cache_ready:
                names.append("prompts_schema_json")
                values.append(json.dumps(prompt_cache, ensure_ascii=False))
            await db.execute(
                f"INSERT INTO team_tool_source({','.join(names)}) VALUES ({','.join('?' for _ in names)})",
                values,
            )
            await db.commit()
        return src

    async def get_team_tool_source(self, team_id: str, source_id: str) -> TeamToolSource | None:
        """team-bound read (id AND team_id) — cross-team access returns None (§6.3)."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM team_tool_source WHERE id=? AND team_id=?",
                (source_id, team_id),
            ) as cur:
                row = await cur.fetchone()
        return _row_to_team_tool_source(row) if row else None

    async def list_team_tool_sources(
        self,
        team_id: str,
        *,
        only_enabled: bool = False,
    ) -> list[TeamToolSource]:
        sql = "SELECT * FROM team_tool_source WHERE team_id=?"
        params: list[Any] = [team_id]
        if only_enabled:
            sql += " AND status='enabled'"
        sql += " ORDER BY created_at ASC"
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, params) as cur:
                rows = await cur.fetchall()
        return [_row_to_team_tool_source(r) for r in rows]

    async def update_team_tool_source_meta(
        self,
        team_id: str,
        source_id: str,
        *,
        name: str | None = None,
        selected_op_ids: list[str] | None = None,
        tools_schema: list[dict[str, Any]] | None = None,
        prompts_schema: list[dict[str, Any]] | None = None,
        tool_overrides: dict[str, dict[str, Any]] | None = None,
        status: str | None = None,
        expected_updated_at: str | None = None,
    ) -> str | None:
        """Edit display metadata (§9#9). Does NOT touch auth (see update_team_tool_source_auth) or the
        registered tool name (that derives from the immutable id, §8.1/F12). ``tool_overrides`` is the
        per-op description override map (tools sprint WP1c — editable descriptions post-create)."""
        sets: list[str] = []
        params: list[Any] = []
        prompt_cache = validate_mcp_prompt_cache(prompts_schema) if prompts_schema is not None else None
        if name is not None:
            sets.append("name=?")
            params.append(name)
        if selected_op_ids is not None:
            sets.append("selected_op_ids_json=?")
            params.append(json.dumps(selected_op_ids))
        if tools_schema is not None:
            sets.append("tools_schema_json=?")
            params.append(json.dumps(tools_schema, ensure_ascii=False))
        if tool_overrides is not None:
            sets.append("tool_overrides_json=?")
            params.append(json.dumps(tool_overrides, ensure_ascii=False))
        if status is not None:
            sets.append("status=?")
            params.append(status)
        if not sets and prompt_cache is None:
            return None
        async with aiosqlite.connect(self.db_path) as db:
            if prompt_cache is not None:
                if prompt_cache:
                    async with db.execute(
                        "SELECT kind FROM team_tool_source WHERE id=? AND team_id=?",
                        (source_id, team_id),
                    ) as cursor:
                        row = await cursor.fetchone()
                    if row is not None and str(row[0]) != "mcp":
                        raise ValueError("MCP Prompt metadata requires an MCP source")
                async with db.execute(
                    "PRAGMA table_info(team_tool_source)",
                ) as cursor:
                    columns = {str(row[1]) for row in await cursor.fetchall()}
                if "prompts_schema_json" not in columns:
                    if prompt_cache:
                        raise RuntimeError("mcp_prompt_cache_schema_unavailable")
                else:
                    sets.append("prompts_schema_json=?")
                    params.append(json.dumps(prompt_cache, ensure_ascii=False))
            if not sets:
                return None
            updated_at = _now()
            sets.append("updated_at=?")
            params.append(updated_at)
            params += [source_id, team_id]
            where = "id=? AND team_id=?"
            if expected_updated_at is not None:
                where += " AND updated_at=?"
                params.append(expected_updated_at)
            cursor = await db.execute(
                f"UPDATE team_tool_source SET {', '.join(sets)} WHERE {where}",
                params,
            )
            await db.commit()
            return updated_at if cursor.rowcount == 1 else None

    async def update_team_tool_source_auth(
        self,
        team_id: str,
        source_id: str,
        *,
        auth_type: str,
        auth: dict[str, Any] | None = None,
    ) -> None:
        """Encrypt + store upstream auth creds (§6.2). F14 hard guard: when auth_type != none, REFUSE to
        store if no BOWHEAD_FERNET_KEY (encrypt would fall back to plaintext) — tool creds must never be
        at rest in plaintext."""
        from bowhead_core.llm_config_crypto import encrypt_llm_config

        if auth_type == "none":
            auth_ref: str | None = None
        else:
            token = encrypt_llm_config(auth or {})
            if token.startswith("PLAIN:"):
                raise RuntimeError(
                    "BOWHEAD_FERNET_KEY required to store tool auth credentials (refusing to persist plaintext for auth_type != 'none')",
                )
            auth_ref = token
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE team_tool_source SET auth_type=?, auth_ref=?, updated_at=? WHERE id=? AND team_id=?",
                (auth_type, auth_ref, _now(), source_id, team_id),
            )
            await db.commit()

    async def get_team_tool_auth_decrypted(self, team_id: str, source_id: str) -> dict[str, Any]:
        """Decrypt upstream creds for execute-time header injection. {} if none. team-bound read."""
        from bowhead_core.llm_config_crypto import decrypt_llm_config

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT auth_ref FROM team_tool_source WHERE id=? AND team_id=?",
                (source_id, team_id),
            ) as cur:
                row = await cur.fetchone()
        if not row or not row["auth_ref"]:
            return {}
        return decrypt_llm_config(row["auth_ref"])

    async def get_team_builtin_tool(self, team_id: str, provider: str) -> TeamBuiltinTool | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM team_builtin_tool WHERE team_id=? AND provider=? ORDER BY created_at,id LIMIT 1",
                (team_id, provider),
            ) as cur:
                row = await cur.fetchone()
        return _row_to_team_builtin_tool(row) if row else None

    async def list_team_builtin_tools(self, team_id: str) -> list[TeamBuiltinTool]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM team_builtin_tool WHERE team_id=? ORDER BY provider,display_name", (team_id,)) as cur:
                rows = await cur.fetchall()
        return [_row_to_team_builtin_tool(row) for row in rows]

    async def upsert_team_builtin_tool(
        self, *, team_id: str, provider: str, auth: dict[str, Any], status: str, created_by: str | None
    ) -> TeamBuiltinTool:
        """Encrypt credentials; no key means fail closed rather than plaintext storage."""
        from bowhead_core.llm_config_crypto import encrypt_llm_config

        token = encrypt_llm_config(auth)
        if token.startswith("PLAIN:"):
            raise RuntimeError("BOWHEAD_FERNET_KEY required to store built-in tool credentials")
        now = _now()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT id FROM team_builtin_tool WHERE team_id=? AND provider=? ORDER BY created_at,id LIMIT 1",
                (team_id, provider),
            ) as cur:
                existing = await cur.fetchone()
            if existing:
                await db.execute(
                    "UPDATE team_builtin_tool SET auth_ref=?,status=?,updated_at=? WHERE id=? AND team_id=?",
                    (token, status, now, existing["id"], team_id),
                )
            else:
                await db.execute(
                    """INSERT INTO team_builtin_tool(
                         id,team_id,provider,display_name,endpoint,status,auth_ref,key_hint,
                         created_by,created_at,updated_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (_id("ibt"), team_id, provider, "Tavily", "https://api.tavily.com", status, token, None, created_by, now, now),
                )
            await db.commit()
            async with db.execute(
                "SELECT * FROM team_builtin_tool WHERE team_id=? AND provider=? ORDER BY created_at,id LIMIT 1",
                (team_id, provider),
            ) as cur:
                row = await cur.fetchone()
        return _row_to_team_builtin_tool(row)

    async def create_team_builtin_tool_instance(
        self,
        *,
        team_id: str,
        provider: str,
        display_name: str,
        endpoint: str,
        auth: dict[str, Any],
        key_hint: str | None,
        status: str,
        created_by: str | None,
        scope_state: str = "unknown",
        scope: dict[str, Any] | None = None,
        scope_fingerprint: str | None = None,
        scope_checked_at: str | None = None,
        config: dict[str, Any] | None = None,
    ) -> TeamBuiltinTool:
        from bowhead_core.llm_config_crypto import encrypt_llm_config

        token = encrypt_llm_config(auth)
        if token.startswith("PLAIN:"):
            raise RuntimeError("BOWHEAD_FERNET_KEY required to store built-in tool credentials")
        now = _now()
        tool_id = _id("ibt")
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute(
                """INSERT INTO team_builtin_tool(
                     id,team_id,provider,display_name,endpoint,status,auth_ref,key_hint,
                     scope_state,scope_json,scope_fingerprint,scope_checked_at,config_json,
                     created_by,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    tool_id,
                    team_id,
                    provider,
                    display_name,
                    endpoint,
                    status,
                    token,
                    key_hint,
                    scope_state,
                    json.dumps(scope or {}, ensure_ascii=False, sort_keys=True),
                    scope_fingerprint,
                    scope_checked_at,
                    json.dumps(config, ensure_ascii=False, sort_keys=True) if config is not None else None,
                    created_by,
                    now,
                    now,
                ),
            )
            await db.commit()
            async with db.execute("SELECT * FROM team_builtin_tool WHERE id=? AND team_id=?", (tool_id, team_id)) as cur:
                row = await cur.fetchone()
        return _row_to_team_builtin_tool(row)

    async def get_team_builtin_tool_instance(self, team_id: str, tool_id: str) -> TeamBuiltinTool | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM team_builtin_tool WHERE id=? AND team_id=?", (tool_id, team_id)) as cur:
                row = await cur.fetchone()
        return _row_to_team_builtin_tool(row) if row else None

    async def update_team_builtin_tool_instance(
        self,
        *,
        team_id: str,
        tool_id: str,
        expected_revision: int | None = None,
        display_name: str | None = None,
        endpoint: str | None = None,
        auth: dict[str, Any] | None = None,
        key_hint: str | None = None,
        status: str | None = None,
        scope_state: str | None = None,
        scope: dict[str, Any] | None = None,
        scope_fingerprint: str | None = None,
        scope_checked_at: str | None = None,
        config: dict[str, Any] | None = None,
    ) -> TeamBuiltinTool | None:
        updates: list[str] = []
        values: list[Any] = []
        if display_name is not None:
            updates.append("display_name=?")
            values.append(display_name)
        if endpoint is not None:
            updates.append("endpoint=?")
            values.append(endpoint)
        if auth is not None:
            from bowhead_core.llm_config_crypto import encrypt_llm_config

            token = encrypt_llm_config(auth)
            if token.startswith("PLAIN:"):
                raise RuntimeError("BOWHEAD_FERNET_KEY required to store built-in tool credentials")
            updates.extend(("auth_ref=?", "key_hint=?"))
            values.extend((token, key_hint))
        if status is not None:
            updates.append("status=?")
            values.append(status)
        if scope_state is not None:
            updates.append("scope_state=?")
            values.append(scope_state)
        if scope is not None:
            updates.append("scope_json=?")
            values.append(json.dumps(scope, ensure_ascii=False, sort_keys=True))
        if scope_fingerprint is not None:
            updates.append("scope_fingerprint=?")
            values.append(scope_fingerprint)
        if scope_checked_at is not None:
            updates.append("scope_checked_at=?")
            values.append(scope_checked_at)
        if config is not None:
            updates.append("config_json=?")
            values.append(json.dumps(config, ensure_ascii=False, sort_keys=True))
        if not updates:
            return await self.get_team_builtin_tool_instance(team_id, tool_id)
        updates.append("updated_at=?")
        values.extend((_now(), tool_id, team_id))
        guard = ""
        if expected_revision is not None:
            guard = " AND connection_revision=?"
            values.append(expected_revision)
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                f"UPDATE team_builtin_tool SET {','.join(updates)} WHERE id=? AND team_id=?{guard}",  # noqa: S608 - columns and guard are static
                values,
            )
            if cursor.rowcount != 1:
                return None
            await db.commit()
            async with db.execute("SELECT * FROM team_builtin_tool WHERE id=? AND team_id=?", (tool_id, team_id)) as cur:
                row = await cur.fetchone()
        return _row_to_team_builtin_tool(row) if row else None

    async def delete_team_builtin_tool_instance(self, team_id: str, tool_id: str) -> bool:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA foreign_keys=ON")
            cursor = await db.execute("DELETE FROM team_builtin_tool WHERE id=? AND team_id=?", (tool_id, team_id))
            await db.commit()
        return cursor.rowcount == 1

    async def list_builtin_pbi_templates(self, team_id: str, tool_id: str) -> list[dict[str, str]]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT id,name,dax_template FROM builtin_pbi_template WHERE team_id=? AND tool_id=? ORDER BY name",
                (team_id, tool_id),
            ) as cur:
                return [dict(row) for row in await cur.fetchall()]

    async def create_builtin_pbi_template(self, team_id: str, tool_id: str, name: str, dax_template: str) -> dict[str, str]:
        template_id = _id("pbit")
        now = _now()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA foreign_keys=ON")
            await db.execute(
                "INSERT INTO builtin_pbi_template(id,team_id,tool_id,name,dax_template,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                (template_id, team_id, tool_id, name, dax_template, now, now),
            )
            await db.commit()
        return {"id": template_id, "name": name, "dax_template": dax_template}

    async def delete_builtin_pbi_template(self, team_id: str, tool_id: str, template_id: str) -> bool:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "DELETE FROM builtin_pbi_template WHERE id=? AND team_id=? AND tool_id=?",
                (template_id, team_id, tool_id),
            )
            await db.commit()
        return cursor.rowcount == 1

    async def update_team_builtin_tool_status(self, team_id: str, provider: str, status: str) -> TeamBuiltinTool | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "UPDATE team_builtin_tool SET status=?,updated_at=? WHERE team_id=? AND provider=?", (status, _now(), team_id, provider)
            )
            if cursor.rowcount != 1:
                return None
            await db.commit()
            async with db.execute("SELECT * FROM team_builtin_tool WHERE team_id=? AND provider=?", (team_id, provider)) as cur:
                row = await cur.fetchone()
        return _row_to_team_builtin_tool(row) if row else None

    async def get_team_builtin_tool_auth_decrypted(self, team_id: str, provider: str) -> dict[str, Any]:
        from bowhead_core.llm_config_crypto import decrypt_llm_config

        tool = await self.get_team_builtin_tool(team_id, provider)
        return decrypt_llm_config(tool.auth_ref) if tool else {}

    async def get_team_builtin_tool_instance_auth_decrypted(self, team_id: str, tool_id: str) -> dict[str, Any]:
        from bowhead_core.llm_config_crypto import decrypt_llm_config

        tool = await self.get_team_builtin_tool_instance(team_id, tool_id)
        return decrypt_llm_config(tool.auth_ref) if tool else {}

    async def create_mcp_oauth_pending(
        self,
        *,
        state: str,
        team_id: str,
        source_id: str,
        code_verifier: str,
        redirect_uri: str,
        token_endpoint: str,
        client_id: str,
        resource: str | None,
        created_by: str | None,
        public_entry_id: str | None = None,
        ttl_seconds: int = 600,
    ) -> None:
        """Persist one pending OAuth authorization (WP2b-2). Keyed by opaque random ``state``;
        consumed once by ``take_mcp_oauth_pending`` in the callback. Short TTL (default 10 min)."""
        expires_at = (datetime.now(UTC) + timedelta(seconds=ttl_seconds)).isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            # review P3: opportunistic sweep of expired rows (no dedicated sweeper) — ISO-UTC strings
            # sort chronologically, so a lexicographic compare is correct.
            await db.execute("DELETE FROM mcp_oauth_pending WHERE expires_at < ?", (_now(),))
            await db.execute(
                "INSERT INTO mcp_oauth_pending "
                "(state, team_id, source_id, code_verifier, redirect_uri, token_endpoint, "
                "client_id, resource, public_entry_id, created_by, expires_at, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    state,
                    team_id,
                    source_id,
                    code_verifier,
                    redirect_uri,
                    token_endpoint,
                    client_id,
                    resource,
                    public_entry_id,
                    created_by,
                    expires_at,
                    _now(),
                ),
            )
            await db.commit()

    async def take_mcp_oauth_pending(self, state: str) -> dict[str, Any] | None:
        """Consume a pending OAuth authorization EXACTLY ONCE (WP2b-2, CSRF single-use): delete the
        row for ``state`` and return its fields, or None if absent / already used / expired. Uses
        ``DELETE … RETURNING`` so the consume is ATOMIC (review P2) — two concurrent callbacks with the
        same ``state`` can't both read the row before it's deleted; only one gets the record, the
        other gets None. Replay of a used ``state`` is a no-op."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("DELETE FROM mcp_oauth_pending WHERE state=? RETURNING *", (state,)) as cur:
                row = await cur.fetchone()
            await db.commit()
        if row is None:
            return None
        rec = dict(row)
        try:
            expired = datetime.fromisoformat(rec["expires_at"]) < datetime.now(UTC)
        except Exception:
            expired = True
        if expired:
            return None
        return rec

    async def delete_team_tool_source(self, team_id: str, source_id: str) -> bool:
        """team-bound delete. Runtime effect = next turn's per-turn view no longer includes it (§6.10)."""
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "DELETE FROM team_tool_source WHERE id=? AND team_id=?",
                (source_id, team_id),
            ) as cur:
                changed = cur.rowcount
            await db.commit()
        return changed > 0

    # ─── cca P3: team subagents (tech-design §1.2) — mirrors team_tool_source ───

    async def create_team_subagent(self, sa: TeamSubagent) -> TeamSubagent:
        """Compatibility delegate; ``TeamSubagentStore`` owns all write SQL."""
        from bowhead_core.team_subagent_store import TeamSubagentStore

        return await TeamSubagentStore(self.db_path).create_team_subagent(
            sa,
            discover_access_store=True,
        )

    async def get_team_subagent(
        self,
        team_id: str,
        subagent_id: str,
        *,
        only_enabled: bool = False,
    ) -> TeamSubagent | None:
        """team-bound read (id AND team_id) — cross-team returns None."""
        sql = "SELECT * FROM team_subagent WHERE id=? AND team_id=?"
        if only_enabled:
            sql += " AND status='enabled'"
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, (subagent_id, team_id)) as cur:
                row = await cur.fetchone()
        return _row_to_team_subagent(row) if row else None

    async def get_team_subagent_by_name(
        self,
        team_id: str,
        name: str,
        *,
        only_enabled: bool = True,
    ) -> TeamSubagent | None:
        """Resolve a subagent by its routing name within an team (the dispatch/advertise
        lookup, tech-design §3.1). Defaults to enabled-only (dispatch ignores disabled)."""
        sql = "SELECT * FROM team_subagent WHERE team_id=? AND name=?"
        if only_enabled:
            sql += " AND status='enabled'"
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, (team_id, name)) as cur:
                row = await cur.fetchone()
            if row is None:
                return None
            capability_protocol_block = await _live_capability_protocol_block(db, row)
        return replace(
            _row_to_team_subagent(row),
            capability_protocol_block=capability_protocol_block,
        )

    async def list_team_subagents(
        self,
        team_id: str,
        *,
        only_enabled: bool = False,
    ) -> list[TeamSubagent]:
        sql = "SELECT * FROM team_subagent WHERE team_id=?"
        if only_enabled:
            sql += " AND status='enabled'"
        sql += " ORDER BY created_at ASC"
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, (team_id,)) as cur:
                rows = await cur.fetchall()
        return [_row_to_team_subagent(r) for r in rows]

    async def list_team_subagent_summaries(
        self,
        team_id: str,
    ) -> list[TeamSubagentSummaryRecord]:
        """Read only fields and aggregate counts approved for summary mapping."""
        sql = _TEAM_SUBAGENT_SUMMARY_SELECT + " WHERE team_id=? ORDER BY created_at ASC"
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, (team_id,)) as cur:
                rows = await cur.fetchall()
        return [_row_to_team_subagent_summary(row) for row in rows]

    async def get_team_subagent_summary(
        self,
        team_id: str,
        subagent_id: str,
    ) -> TeamSubagentSummaryRecord | None:
        """Read one field-safe summary without loading definition content."""
        sql = _TEAM_SUBAGENT_SUMMARY_SELECT + " WHERE team_id=? AND id=?"
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, (team_id, subagent_id)) as cur:
                row = await cur.fetchone()
        return _row_to_team_subagent_summary(row) if row else None

    async def update_team_subagent_meta(
        self,
        team_id: str,
        subagent_id: str,
        *,
        display_name: str | None = None,
        description: str | None = None,
        system_prompt: str | None = None,
        tool_refs: list[str] | None = None,
        model: str | None = _UNSET,  # _UNSET = omit (keep); None = clear → SQL NULL
        max_iterations: int | None = None,
        skills: list[str] | None = None,
        output_schema: dict[str, Any] | None = _UNSET,  # _UNSET = omit; None = clear → NULL
        status: str | None = None,
    ) -> None:
        """Edit a subagent. Does NOT touch `name` — the routing/peer identity is
        immutable post-create (a rename would orphan Lead routing + break dispatch
        resolve by name). team-bound (WHERE id=? AND team_id=?)."""
        from bowhead_core.team_subagent_store import TeamSubagentStore

        await TeamSubagentStore(self.db_path).update_team_subagent_meta(
            team_id,
            subagent_id,
            display_name=display_name,
            description=description,
            system_prompt=system_prompt,
            tool_refs=tool_refs,
            model=(None if model is _UNSET else model),
            model_is_set=model is not _UNSET,
            max_iterations=max_iterations,
            skills=skills,
            output_schema=(None if output_schema is _UNSET else output_schema),
            output_schema_is_set=output_schema is not _UNSET,
            status=status,
        )

    async def delete_team_subagent(self, team_id: str, subagent_id: str) -> bool:
        """Compatibility delegate; legacy callers keep the same behavior."""
        from bowhead_core.team_subagent_store import TeamSubagentStore

        return await TeamSubagentStore(self.db_path).delete_team_subagent(
            team_id,
            subagent_id,
            discover_access_store=True,
        )

    # ─── workflow-solidify S4: team_skill CRUD (clone team_subagent; all team_id-keyed, IDOR-safe) ───
    async def create_team_skill(self, sk: TeamSkill) -> TeamSkill:
        """Insert a new team skill. BEGIN IMMEDIATE makes (team_id, name) check + INSERT atomic (TOCTOU)."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            async with db.execute(
                "SELECT 1 FROM team_skill WHERE team_id=? AND name=?",
                (sk.team_id, sk.name),
            ) as cur:
                if await cur.fetchone():
                    await db.rollback()
                    raise ValueError(f"skill name already exists in team: {sk.name!r}")
            await db.execute(
                "INSERT INTO team_skill(id,team_id,name,display_name,description,source_path,"
                "visibility,verdict,verdict_json,risk_report_json,created_by,status,size_bytes,created_at,updated_at,"
                "approval,approved_sha256,approved_by,approved_at,rejected_reason,rejected_at,"
                "candidate_path,candidate_submitted_at,candidate_sha256,candidate_state,candidate_rejected_reason,submitted_by) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    sk.id,
                    sk.team_id,
                    sk.name,
                    sk.display_name,
                    sk.description,
                    sk.source_path,
                    sk.visibility,
                    sk.verdict,
                    sk.verdict_json,
                    sk.risk_report_json,
                    sk.created_by,
                    sk.status,
                    sk.size_bytes,
                    sk.created_at,
                    sk.updated_at,
                    sk.approval,
                    sk.approved_sha256,
                    sk.approved_by,
                    sk.approved_at,
                    sk.rejected_reason,
                    sk.rejected_at,
                    sk.candidate_path,
                    sk.candidate_submitted_at,
                    sk.candidate_sha256,
                    sk.candidate_state,
                    sk.candidate_rejected_reason,
                    sk.submitted_by,
                ),
            )
            await db.commit()
        return sk

    async def set_team_skill_import_provenance(
        self,
        team_id: str,
        skill_id: str,
        provenance_json: str | None,
    ) -> bool:
        """写入导入出处（B 线批 4，迁移 0064）。promote 路由在服务端算好后调它。

        ⚠ 只该由 promote 路径调用，且值必须**从个人实例现算**——这份东西的意义是告诉审核者
        作者可能不愿主动说的事，一旦接受调用方传入的任意值，作者就能提交一份 red_count:0
        的干净出处瞒过审批（跨 session 定案，见迁移 0064 docstring）。
        """

        async with aiosqlite.connect(self.db_path) as db:
            # Databases created before migration 0064 (including all initialize()-created test dbs) lack this column; bare UPDATE
            # raises OperationalError. Reader `_row_opt` tolerates absence; writes likewise return False to tell callers
            # no write occurred (extra provenance must not block promotion), with a log rather than silent failure.
            cols = {str(r[1]) for r in await (await db.execute("PRAGMA table_info(team_skill)")).fetchall()}
            if "import_provenance_json" not in cols:
                logger.warning("team_skill.import_provenance_json missing (migration 0064 not applied); provenance not recorded")
                return False
            cur = await db.execute(
                "UPDATE team_skill SET import_provenance_json=?, updated_at=? WHERE id=? AND team_id=?",
                (provenance_json, _now(), skill_id, team_id),
            )
            await db.commit()
            return cur.rowcount == 1

    async def get_team_skill(
        self,
        team_id: str,
        skill_id: str,
        *,
        only_enabled: bool = False,
        connection: aiosqlite.Connection | None = None,
    ) -> TeamSkill | None:
        """team-bound read (id AND team_id) — cross-team returns None."""
        _require_read_transaction(connection)
        sql = "SELECT * FROM team_skill WHERE id=? AND team_id=?"
        if only_enabled:
            sql += " AND status='enabled'"
        async with (
            nullcontext(connection) if connection is not None else aiosqlite.connect(self.db_path) as db,
            db.execute(sql, (skill_id, team_id)) as cur,
        ):
            cur.row_factory = aiosqlite.Row
            row = await cur.fetchone()
        return _row_to_team_skill(row) if row else None

    async def get_team_skill_by_name(
        self,
        team_id: str,
        name: str,
        *,
        only_enabled: bool = True,
        only_approved: bool = True,
    ) -> TeamSkill | None:
        """Resolve an team skill by name (skill_resolver team branch lookup). enabled-only default.

        skill-team-governance P1b：``only_approved=True`` 默认参是 pending/rejected 可见性
        收敛的唯一机制（PRD §3.1）——resolve/effective/skill_list/home_proposals/picker 五个
        消费面不改代码自动闭合，未来新消费面 fail-closed。管理面（团队 skills 路由 /
        solidify verdict 写回）显式传 False opt-out。"""
        sql = "SELECT * FROM team_skill WHERE team_id=? AND name=?"
        if only_enabled:
            sql += " AND status='enabled'"
        if only_approved:
            sql += " AND approval='approved'"
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, (team_id, name)) as cur:
                row = await cur.fetchone()
        return _row_to_team_skill(row) if row else None

    async def list_team_skills(
        self,
        team_id: str,
        *,
        only_enabled: bool = False,
        only_approved: bool = True,
    ) -> list[TeamSkill]:
        """List an team's skills. ``only_approved`` 语义同 ``get_team_skill_by_name``（默认参
        闭合消费面；L2 更新在审的行 approval 仍是 approved → live 不受影响）。"""
        sql = "SELECT * FROM team_skill WHERE team_id=?"
        if only_enabled:
            sql += " AND status='enabled'"
        if only_approved:
            sql += " AND approval='approved'"
        sql += " ORDER BY created_at ASC"
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, (team_id,)) as cur:
                rows = await cur.fetchall()
        return [_row_to_team_skill(r) for r in rows]

    async def update_team_skill_meta(
        self,
        team_id: str,
        skill_id: str,
        *,
        display_name: str | None = None,
        description: str | None = None,
        status: str | None = None,
        risk_report_json: Any = _UNSET,
    ) -> None:
        """Edit an team skill's metadata. Does NOT touch `name` (immutable identity) or
        source_path (content path). team-bound (WHERE id=? AND team_id=?).

        skill-team-governance P1b：visibility 参数已删——visibility 转移是审批状态机的
        转移（§1 转移表），只能走 submit/withdraw/set_audience 写穿，不留裸写后门。"""
        sets: list[str] = []
        params: list[Any] = []
        if display_name is not None:
            sets.append("display_name=?")
            params.append(display_name)
        if description is not None:
            sets.append("description=?")
            params.append(description)
        if status is not None:
            sets.append("status=?")
            params.append(status)
        if risk_report_json is not _UNSET:
            # None is meaningful here (the submitted content has no risk findings), so absence uses a sentinel.
            sets.append("risk_report_json=?")
            params.append(risk_report_json)
        if not sets:
            return
        sets.append("updated_at=?")
        params.append(_now())
        params += [skill_id, team_id]
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                f"UPDATE team_skill SET {', '.join(sets)} WHERE id=? AND team_id=?",
                params,
            )
            await db.commit()

    # ─── skill-team-governance P1b: approval state transitions (match tech-design §1 transition table) ───
    # Orthogonal slots: approval = approved slot (L1 initial-submission lifecycle); candidate_* = updates (L2).
    # approval stays approved so teams use the old approved version during review. Each UPDATE's WHERE
    # guards the transition's prior state (mismatch → 0 rows → caller 409/404), naturally providing CAS.

    async def submit_team_skill(
        self,
        team_id: str,
        skill_id: str,
        *,
        submitted_by: str,
    ) -> bool:
        """L1 提交：creator_only(approved) → team_wide/pending（PATCH 门 2 / promote 本人
        creator_only 腿——不建候选槽，杜绝「creator_only+candidate」怪态）。"""
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "UPDATE team_skill SET visibility='team_wide', approval='pending', submitted_by=?, "
                "rejected_reason=NULL, rejected_at=NULL, updated_at=? "
                "WHERE id=? AND team_id=? AND approval='approved' AND visibility='creator_only' AND candidate_path IS NULL",
                (submitted_by, _now(), skill_id, team_id),
            ) as cur:
                changed = cur.rowcount
            await db.commit()
        return changed > 0

    async def resubmit_team_skill(
        self,
        team_id: str,
        skill_id: str,
        *,
        submitted_by: str,
    ) -> bool:
        """L1 重提（rejected → pending，清拒因）/ L1 等审重提（pending 保持 pending——CAS 键
        =盘 hash，作者覆盖 source_path 后自然刷新 → admin 旧键 409）。"""
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "UPDATE team_skill SET approval='pending', submitted_by=?, "
                "rejected_reason=NULL, rejected_at=NULL, updated_at=? "
                "WHERE id=? AND team_id=? AND approval IN ('pending','rejected') "
                "AND visibility='team_wide' AND candidate_path IS NULL",
                (submitted_by, _now(), skill_id, team_id),
            ) as cur:
                changed = cur.rowcount
            await db.commit()
        return changed > 0

    async def submit_team_skill_candidate(
        self,
        team_id: str,
        skill_id: str,
        *,
        candidate_path: str,
        candidate_sha256: str,
        submitted_by: str,
    ) -> str | None:
        """L2 提交更新/重提（覆盖语义，明示）：候选槽整置 pending，approval 不动（live 不藏）。
        返回新 candidate_submitted_at（CAS 键之一）；行非 team_wide+approved → None——
        WHERE 含 visibility 守卫，结构性杜绝「creator_only+candidate」怪态（r3 P1-2：
        promote 的本人 creator_only 腿=L1 提交，不建候选槽）。"""
        now = _now()
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "UPDATE team_skill SET candidate_path=?, candidate_submitted_at=?, candidate_sha256=?, "
                "candidate_state='pending', candidate_rejected_reason=NULL, submitted_by=?, updated_at=? "
                "WHERE id=? AND team_id=? AND approval='approved' AND visibility='team_wide'",
                (candidate_path, now, candidate_sha256, submitted_by, now, skill_id, team_id),
            ) as cur:
                changed = cur.rowcount
            await db.commit()
        return now if changed > 0 else None

    async def approve_team_skill(
        self,
        team_id: str,
        skill_id: str,
        *,
        approved_by: str,
        approved_sha256: str,
        expected_candidate_submitted_at: str | None = None,
        expected_candidate_sha256: str | None = None,
    ) -> bool:
        """approve 区分双腿（候选键缺省=L1）。L1：翻已批槽；盘 hash 与请求键的比对由路由在
        per-skill 锁内先行，此处 WHERE approval='pending' 是 DB 层 CAS。L2：由路由在盘替换
        （approved→__old→candidate 换名）成功后调，BEGIN IMMEDIATE 内候选双键二次 CAS——
        0 行命中=锁外理论竞态，路由回滚盘（反向 rename）+ 409。"""
        now = _now()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            if expected_candidate_submitted_at is None:
                cur = await db.execute(
                    "UPDATE team_skill SET approval='approved', approved_sha256=?, approved_by=?, "
                    "approved_at=?, updated_at=? "
                    "WHERE id=? AND team_id=? AND approval='pending' AND candidate_path IS NULL",
                    (approved_sha256, approved_by, now, now, skill_id, team_id),
                )
            else:
                cur = await db.execute(
                    "UPDATE team_skill SET approved_sha256=?, approved_by=?, approved_at=?, "
                    "candidate_path=NULL, candidate_submitted_at=NULL, candidate_sha256=NULL, "
                    "candidate_state=NULL, candidate_rejected_reason=NULL, updated_at=? "
                    "WHERE id=? AND team_id=? AND approval='approved' AND candidate_state='pending' "
                    "AND candidate_submitted_at=? AND candidate_sha256=?",
                    (
                        approved_sha256,
                        approved_by,
                        now,
                        now,
                        skill_id,
                        team_id,
                        expected_candidate_submitted_at,
                        expected_candidate_sha256,
                    ),
                )
            changed = cur.rowcount
            await db.commit()
        return changed > 0

    async def reject_team_skill(
        self,
        team_id: str,
        skill_id: str,
        *,
        reason: str,
        expected_candidate_submitted_at: str | None = None,
        expected_candidate_sha256: str | None = None,
    ) -> bool:
        """reject 分腿写不同字段。L1（候选键缺省）：approval=rejected+rejected_*，内容留
        source_path 仅作者可见。L2：candidate_state=rejected+候选拒因，live 不动照跑；
        候选保留供作者查看（重提覆盖、删 skill 清理）。"""
        now = _now()
        async with aiosqlite.connect(self.db_path) as db:
            if expected_candidate_submitted_at is None:
                cur = await db.execute(
                    "UPDATE team_skill SET approval='rejected', rejected_reason=?, rejected_at=?, updated_at=? "
                    "WHERE id=? AND team_id=? AND approval='pending' AND candidate_path IS NULL",
                    (reason, now, now, skill_id, team_id),
                )
            else:
                cur = await db.execute(
                    "UPDATE team_skill SET candidate_state='rejected', candidate_rejected_reason=?, updated_at=? "
                    "WHERE id=? AND team_id=? AND candidate_state='pending' "
                    "AND candidate_submitted_at=? AND candidate_sha256=?",
                    (reason, now, skill_id, team_id, expected_candidate_submitted_at, expected_candidate_sha256),
                )
            changed = cur.rowcount
            await db.commit()
        return changed > 0

    async def withdraw_team_skill(self, team_id: str, skill_id: str) -> str | None:
        """作者撤回，按行内有无候选分腿。L2（有候选，pending/rejected 均可）：只清候选槽——
        live/visibility 不动（不得下架已批资产）。L1（pending/rejected 无候选）：
        visibility=creator_only + approval=approved（自用态），清 rejected_*/submitted_by
        ——兼作「拒后收回」转移。返回 'candidate' / 'submission' / None（无可撤）。"""
        now = _now()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            async with db.execute(
                "SELECT candidate_path, approval FROM team_skill WHERE id=? AND team_id=?",
                (skill_id, team_id),
            ) as cur:
                row = await cur.fetchone()
            if row is None:
                await db.rollback()
                return None
            if row["candidate_path"] is not None:
                await db.execute(
                    "UPDATE team_skill SET candidate_path=NULL, candidate_submitted_at=NULL, "
                    "candidate_sha256=NULL, candidate_state=NULL, candidate_rejected_reason=NULL, updated_at=? "
                    "WHERE id=? AND team_id=?",
                    (now, skill_id, team_id),
                )
                await db.commit()
                return "candidate"
            if row["approval"] in ("pending", "rejected"):
                await db.execute(
                    "UPDATE team_skill SET visibility='creator_only', approval='approved', "
                    "rejected_reason=NULL, rejected_at=NULL, submitted_by=NULL, updated_at=? "
                    "WHERE id=? AND team_id=?",
                    (now, skill_id, team_id),
                )
                await db.commit()
                return "submission"
            await db.rollback()
            return None

    async def set_team_skill_verdict(
        self,
        team_id: str,
        skill_id: str,
        *,
        verdict: str,
        verdict_json: str | None = None,
    ) -> bool:
        """workflow-solidify S4 (§4.1): write team recipe dry-run verdict. team-bound."""
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "UPDATE team_skill SET verdict=?, verdict_json=?, updated_at=? WHERE id=? AND team_id=?",
                (verdict, verdict_json, _now(), skill_id, team_id),
            ) as cur:
                changed = cur.rowcount
            await db.commit()
        return changed > 0

    async def restamp_team_skill_bundle(
        self,
        team_id: str,
        skill_id: str,
        *,
        size_bytes: int,
        description: str,
        risk_report_json: str | None,
        approved_sha256: str | None = None,
    ) -> bool:
        """配置页就地编辑落盘后刷新派生字段（size_bytes / 已批指纹）. team-bound.

        ``approved_sha256`` 只在「改的就是已批 live 快照」时传：已批槽记的是 admin 批过的
        那一份内容，盘变了而指纹不变 → 审批 CAS 与 drawer 的「已同步/落后」判定全会说谎。
        pending 行不传（L1 的 CAS 键是读侧现算的盘 hash，天然跟着盘走）。"""
        sets = ["size_bytes=?", "updated_at=?", "description=?", "risk_report_json=?", "verdict=NULL", "verdict_json=NULL"]
        params: list[Any] = [size_bytes, _now(), description, risk_report_json]
        if approved_sha256 is not None:
            sets.insert(1, "approved_sha256=?")
            params.insert(1, approved_sha256)
        params += [skill_id, team_id]
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                f"UPDATE team_skill SET {', '.join(sets)} WHERE id=? AND team_id=?",
                params,
            ) as cur:
                changed = cur.rowcount
            await db.commit()
        return changed > 0

    async def delete_team_skill(self, team_id: str, skill_id: str) -> bool:
        """team-bound delete."""
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "DELETE FROM team_skill WHERE id=? AND team_id=?",
                (skill_id, team_id),
            ) as cur:
                changed = cur.rowcount
            await db.commit()
        return changed > 0

    async def log_tool_call(
        self,
        *,
        team_id: str,
        tool_name: str,
        status: str,
        user_id: str | None = None,
        source_id: str | None = None,
        args_digest: str | None = None,
        error_kind: str | None = None,
        ssrf_verdict: str | None = None,
        duration_ms: int | None = None,
        upstream_status: int | None = None,
        bytes_out: int | None = None,
    ) -> None:
        """PRD §6.8/F22 exec-audit. NO decrypted secret / NO raw args (digest only)."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO team_tool_call_log(id,team_id,user_id,source_id,tool_name,args_digest,"
                "status,error_kind,ssrf_verdict,duration_ms,upstream_status,bytes_out,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    _id("otcl"),
                    team_id,
                    user_id,
                    source_id,
                    tool_name,
                    args_digest,
                    status,
                    error_kind,
                    ssrf_verdict,
                    duration_ms,
                    upstream_status,
                    bytes_out,
                    _now(),
                ),
            )
            await db.commit()

    async def get_team(self, team_id: str) -> Team | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM teams WHERE id = ?",
                (team_id,),
            ) as cur:
                row = await cur.fetchone()
        return _row_to_team(row) if row else None

    async def list_teams(self) -> list[Team]:
        """List team identities for the pre-license local activation screen."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM teams ORDER BY created_at, id") as cur:
                rows = await cur.fetchall()
        return [_row_to_team(row) for row in rows]

    # ─── licensing (on-prem, §6.1/§7.4) ──────────────────

    async def get_team_license(self, team_id: str) -> TeamLicense | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM team_license WHERE team_id = ?", (team_id,)) as cur:
                row = await cur.fetchone()
        return _row_to_team_license(row) if row else None

    async def get_single_team_license(self) -> TeamLicense | None:
        """The single license row (an on-prem/local deployment is single-team). Used as the startup team anchor
        when ``BOWHEAD_LICENSE_TEAM_ID`` is not pinned (§6.4)."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM team_license ORDER BY team_id LIMIT 1") as cur:
                row = await cur.fetchone()
        return _row_to_team_license(row) if row else None

    async def upsert_team_license(self, lic: TeamLicense) -> None:
        """Persist/replace the durable license mirror (§6.5 upload accept path). Idempotent per team_id."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO team_license(team_id, license_token, license_id, key_id, issued_at, expires_at, "
                "grace_period_days, max_seats, default_user_tier, tier_credits_json, features_json, "
                "deployment_mode, max_offline_lifetime_days, activated_at, last_verified_at, last_good_health, "
                "instance_fingerprint) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(team_id) DO UPDATE SET "
                "  license_token=excluded.license_token, license_id=excluded.license_id, key_id=excluded.key_id, "
                "  issued_at=excluded.issued_at, expires_at=excluded.expires_at, "
                "  grace_period_days=excluded.grace_period_days, max_seats=excluded.max_seats, "
                "  default_user_tier=excluded.default_user_tier, tier_credits_json=excluded.tier_credits_json, "
                "  features_json=excluded.features_json, deployment_mode=excluded.deployment_mode, "
                "  max_offline_lifetime_days=excluded.max_offline_lifetime_days, "
                "  last_verified_at=excluded.last_verified_at, last_good_health=excluded.last_good_health, "
                "  instance_fingerprint=excluded.instance_fingerprint",
                (
                    lic.team_id,
                    lic.license_token,
                    lic.license_id,
                    lic.key_id,
                    lic.issued_at,
                    lic.expires_at,
                    lic.grace_period_days,
                    lic.max_seats,
                    lic.default_user_tier,
                    json.dumps(lic.tier_credits),
                    json.dumps(lic.features),
                    lic.deployment_mode,
                    lic.max_offline_lifetime_days,
                    lic.activated_at,
                    lic.last_verified_at,
                    lic.last_good_health,
                    lic.instance_fingerprint,
                ),
            )
            await db.commit()

    async def apply_local_license_mirror(
        self,
        *,
        team_id: str,
        max_seats: int,
        default_user_tier: str,
        tier_credits: dict[str, int],
        contract_expires_at: str | None,
        last_verified_at: str,
        last_good_health: str,
    ) -> None:
        """LOCAL-ONLY (§6.4 OPS-6) re-derive: mirror the verified license onto the team row (informational — no
        reader treats the team row as authoritative in local) + stamp the license-row verification. The CALLER
        (``_license_startup``) gates this on ``mode == 'local'`` so a saas startup issues ZERO ``UPDATE
        teams``. One tx.

        **空 `tier_credits` 不写**（dev 2026-07-23 事故的根因）：`create_team` 对空值有兜底
        （`tier_credits or {默认}`），本方法原先没有 —— 于是「license 漏签 tier_credits」的后果是
        建 team 时拿到正确默认值、**首次启动被 mirror 用 `{}` 覆盖**，此后每个新开通用户
        `credit_quota=0`、每轮 chat 撞 402。空额度表一定是签发疏漏而非「该团队所有 tier 都是零额度」
        这种意图，所以保留现有值 + ERROR 告警，与 `monthly_credit_reset` 的缺键 fail-safe 同口径。
        （签发侧现已拒签这种 license，但这一层要能挡住已经签出去的。）
        """
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                if tier_credits:
                    await db.execute(
                        "UPDATE teams SET max_seats = ?, default_user_tier = ?, tier_credits_json = ?, contract_expires_at = ? WHERE id = ?",
                        (max_seats, default_user_tier, json.dumps(tier_credits), contract_expires_at, team_id),
                    )
                else:
                    logger.error(
                        "license_mirror.empty_tier_credits team=%s -- license 未签 tier_credits; "
                        "保留 teams.tier_credits_json 现值以免新用户被开通成 0 额度。重签带 --tier-credits 的 license 修复。",
                        team_id,
                    )
                    await db.execute(
                        "UPDATE teams SET max_seats = ?, default_user_tier = ?, contract_expires_at = ? WHERE id = ?",
                        (max_seats, default_user_tier, contract_expires_at, team_id),
                    )
                await db.execute(
                    "UPDATE team_license SET last_verified_at = ?, last_good_health = ? WHERE team_id = ?",
                    (last_verified_at, last_good_health, team_id),
                )
                await db.commit()
            except Exception:
                await db.rollback()
                raise

    async def get_highest_seen_ts(self) -> str | None:
        """ClockGuard read (§7.4): the monotonic highest wall-clock the local deployment has ever verified at."""
        async with (
            aiosqlite.connect(self.db_path) as db,
            db.execute(
                "SELECT highest_seen_ts FROM entitlement_clock WHERE id = 'singleton'",
            ) as cur,
        ):
            row = await cur.fetchone()
        return row[0] if row else None

    async def advance_clock(self, now_iso: str) -> bool:
        """ClockGuard advance (§7.4): bump ``highest_seen_ts`` to ``now_iso`` if it is newer. Returns False
        (and does NOT raise) if the monotonic trigger detected a rollback (``now_iso`` < highest seen) — the
        caller (verify/recheck) treats that as clock-tamper. Written ONLY on startup-verify + recheck."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT OR IGNORE INTO entitlement_clock(id, highest_seen_ts) VALUES ('singleton', ?)",
                (now_iso,),
            )
            try:
                await db.execute(
                    "UPDATE entitlement_clock SET highest_seen_ts = ? WHERE id = 'singleton'",
                    (now_iso,),
                )
            except Exception:
                await db.rollback()
                return False
            await db.commit()
        return True

    # ─── user ───────────────────────────────────────────

    async def count_active_seats(self, team_id: str, conn: Any | None = None) -> int:
        """Distinct active-user seat count for an team (licensing §4.1).

        Distinctness is structural — ``users.email`` UNIQUE + ``id`` PK ⇒ one row per active user, no
        ``DISTINCT`` needed; never count ``workspace_members`` (one row per (ws,user) → N× over-count).
        Covering index ``idx_users_team(team_id,status)``. Pass the caller's ``conn`` so the seat-check + INSERT
        in ``create_user`` run inside one ``BEGIN IMMEDIATE`` tx (TOCTOU, §4.2).
        """
        sql = (
            "SELECT COUNT(*) FROM team_members om JOIN users u ON u.id = om.user_id "
            "WHERE om.team_id = ? AND om.status = 'active' AND u.status = 'active'"
        )
        if conn is not None:
            async with conn.execute(sql, (team_id,)) as cur:
                row = await cur.fetchone()
            return int(row[0]) if row else 0
        async with aiosqlite.connect(self.db_path) as db, db.execute(sql, (team_id,)) as cur:
            row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def create_user(
        self,
        *,
        team_id: str,
        email: str,
        password: str | None = None,
        display_name: str | None = None,
        tier: str = "pro",
        oauth_providers: list[str] | None = None,
        email_verified: bool = False,
        avatar_url: str | None = None,
        replace_deleted_user_id: str | None = None,
    ) -> User:
        email = normalize_email(email)
        # Licensing §4.2: the seat-check + INSERT run on ONE connection inside a BEGIN IMMEDIATE tx so two
        # concurrent same-team registrations cannot both read `max_seats-1` and both insert (TOCTOU/LIC-3).
        # The team read also moves onto this conn (was a separate short-lived conn). The seat gate is the
        # injected resolver; in shadow mode (101 default) it logs a would-deny and returns — zero behavior change.
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row  # _row_to_team needs key access
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute(
                    "SELECT * FROM teams WHERE id = ?",
                    (team_id,),
                ) as cur:
                    team_row = await cur.fetchone()
                team = _row_to_team(team_row) if team_row else None
                if not team:
                    raise ValueError(f"team not found: {team_id}")
                if replace_deleted_user_id is not None:
                    async with db.execute(
                        "SELECT email,status FROM users WHERE id=?",
                        (replace_deleted_user_id,),
                    ) as cur:
                        replaced_user = await cur.fetchone()
                    if replaced_user is None or replaced_user["status"] != "deleted" or replaced_user["email"] != email:
                        raise ValueError("deleted identity replacement mismatch")
                    await self._tombstone_identity_conn(
                        db,
                        user_id=replace_deleted_user_id,
                        now=_now(),
                    )
                    await self._bump_user_account_epoch_conn(
                        db,
                        replace_deleted_user_id,
                    )
                if self._seat_guard is not None:
                    active = await self.count_active_seats(team_id, conn=db)
                    # raises EntitlementDenied(seat_exceeded / frozen-team reason) when enforce=1; gateway adapts
                    await self._seat_guard.assert_can_add_seat(team_id, active_count=active)
                quota = team.tier_credits.get(tier, 0)
                u = User(
                    id=_id("usr"),
                    team_id=team_id,
                    email=email,
                    email_verified=email_verified,
                    display_name=display_name,
                    avatar_url=avatar_url,
                    password_hash=hash_password(password) if password else None,
                    oauth_providers=oauth_providers or [],
                    tier=tier,
                    credit_quota=quota,
                    credit_balance=quota,
                    credit_period_start=_now(),
                    created_at=_now(),
                )
                await db.execute(
                    "INSERT INTO users(id,team_id,email,email_verified,display_name,avatar_url,"
                    "password_hash,oauth_providers_json,tier,credit_balance,credit_quota,"
                    "credit_period_start,locale,timezone,preferences_json,status,created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        u.id,
                        u.team_id,
                        u.email,
                        int(u.email_verified),
                        u.display_name,
                        u.avatar_url,
                        u.password_hash,
                        json.dumps(u.oauth_providers),
                        u.tier,
                        u.credit_balance,
                        u.credit_quota,
                        u.credit_period_start,
                        u.locale,
                        u.timezone,
                        json.dumps(u.preferences),
                        u.status,
                        u.created_at,
                    ),
                )
                await db.execute(
                    "INSERT INTO team_members(id,user_id,team_id,role,status,created_at) VALUES (?,?,?,?,?,?)",
                    (_id("om"), u.id, team_id, "member", "active", _now()),
                )
                await db.commit()
            except Exception:
                await db.rollback()
                raise
        return u

    async def has_external_auth_session(self, generation: str) -> bool:
        """Keep external sessions fail-closed when their edition adapter is absent."""
        async with open_pooled_read_ctx(self.db_path) as db:
            async with db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='oidc_auth_sessions'") as cursor:
                if await cursor.fetchone() is None:
                    return False
            async with db.execute(
                "SELECT 1 FROM oidc_auth_sessions WHERE auth_session_generation=?",
                (generation,),
            ) as cursor:
                return await cursor.fetchone() is not None

    async def _tombstone_identity_conn(
        self,
        db: aiosqlite.Connection,
        *,
        user_id: str,
        now: str,
    ) -> None:
        """Release a deleted identity's email without reviving its credentials."""
        await db.execute(
            "UPDATE users SET email=?,email_verified=0,password_hash=NULL,avatar_url=NULL,oauth_providers_json='[]' WHERE id=?",
            (f"{user_id}@deleted.invalid", user_id),
        )
        await db.execute(
            "UPDATE refresh_tokens SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL",
            (now, user_id),
        )
        await db.execute(
            "UPDATE credential_recovery_tokens SET status='revoked',revoked_at=? WHERE user_id=? AND status='active'",
            (now, user_id),
        )
        await db.execute(
            "DELETE FROM user_federated_identities WHERE user_id=?",
            (user_id,),
        )
        async with db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='oidc_identities'",
        ) as cur:
            has_local_oidc = await cur.fetchone() is not None
        if has_local_oidc:
            await db.execute(
                "UPDATE oidc_auth_sessions SET revoked_at=?,revoke_reason='identity_deleted' WHERE user_id=? AND revoked_at IS NULL",
                (now, user_id),
            )
            await db.execute(
                "DELETE FROM oidc_identities WHERE user_id=?",
                (user_id,),
            )

    async def add_existing_user_to_local_team(
        self,
        *,
        user_id: str,
        team_id: str,
    ) -> str:
        """Add an existing identity to the sole local Team, restoring it when necessary.

        The seat gate is evaluated only when this call creates or restores an active
        ``team_members`` row.  Orphaned identities therefore consume no local license seat.
        """
        now = _now()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute(
                    "SELECT status FROM users WHERE id = ?",
                    (user_id,),
                ) as cur:
                    user_row = await cur.fetchone()
                if user_row is None:
                    raise ValueError("user not found")
                async with db.execute(
                    "SELECT 1 FROM teams WHERE id = ?",
                    (team_id,),
                ) as cur:
                    if await cur.fetchone() is None:
                        raise ValueError("team not found")
                async with db.execute(
                    "SELECT status FROM team_members WHERE user_id = ? AND team_id = ?",
                    (user_id, team_id),
                ) as cur:
                    membership = await cur.fetchone()
                if membership is not None and membership["status"] == "active":
                    await db.commit()
                    return "already_member"
                if self._seat_guard is not None:
                    active = await self.count_active_seats(team_id, conn=db)
                    await self._seat_guard.assert_can_add_seat(team_id, active_count=active)
                await db.execute(
                    "INSERT INTO team_members(id,user_id,team_id,role,status,created_at) VALUES (?,?,?,?,?,?) "
                    "ON CONFLICT(user_id,team_id) DO UPDATE SET status='active',removed_at=NULL,removed_by=NULL",
                    (_id("om"), user_id, team_id, "member", "active", now),
                )
                if user_row["status"] != "active":
                    await db.execute(
                        "UPDATE users SET status = 'active' WHERE id = ?",
                        (user_id,),
                    )
                    await self._bump_user_account_epoch_conn(db, user_id, now=now)
                await self._bump_team_membership_epoch_conn(db, team_id, now=now)
                await db.commit()
                return "membership_restored" if membership is not None else "member_added"
            except Exception:
                await db.rollback()
                raise

    async def create_invite_intent(
        self,
        *,
        raw_secret: str,
        invite_type: str,
        invite_id: str,
        token_version: int,
        expires_at: str,
    ) -> str:
        secret_hash = hashlib.sha256(raw_secret.encode()).hexdigest()
        now = _now()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO invite_intents(secret_hash,invite_type,invite_id,"
                "token_version,created_at,expires_at,last_seen_at) VALUES (?,?,?,?,?,?,?)",
                (
                    secret_hash,
                    invite_type,
                    invite_id,
                    token_version,
                    now,
                    expires_at,
                    now,
                ),
            )
            await db.execute(
                "DELETE FROM invite_intents WHERE expires_at < ? AND consumed_at IS NOT NULL",
                ((datetime.now(UTC) - timedelta(days=1)).isoformat(),),
            )
            await db.commit()
        return secret_hash

    async def get_invite_intent(
        self,
        raw_secret: str,
    ) -> dict[str, Any] | None:
        return await self.get_invite_intent_by_hash(
            hashlib.sha256(raw_secret.encode()).hexdigest(),
        )

    async def get_invite_intent_by_hash(
        self,
        secret_hash: str,
    ) -> dict[str, Any] | None:
        now = _now()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT secret_hash,invite_type,invite_id,token_version,created_at,"
                "expires_at,consumed_at,invalidated_at,last_seen_at "
                "FROM invite_intents WHERE secret_hash = ?",
                (secret_hash,),
            ) as cur:
                row = await cur.fetchone()
            if row is None or row["consumed_at"] is not None or row["invalidated_at"] is not None or row["expires_at"] < now:
                return None
            await db.execute(
                "UPDATE invite_intents SET last_seen_at = ? WHERE secret_hash = ?",
                (now, secret_hash),
            )
            await db.commit()
        return dict(row)

    async def invalidate_invite_intent(self, raw_secret: str) -> bool:
        secret_hash = hashlib.sha256(raw_secret.encode()).hexdigest()
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                "UPDATE invite_intents SET invalidated_at = ? WHERE secret_hash = ? AND consumed_at IS NULL AND invalidated_at IS NULL",
                (_now(), secret_hash),
            )
            await db.commit()
            return cur.rowcount > 0

    async def activate_invited_identity_atomic(
        self,
        *,
        intent_hash: str,
        invite_type: str,
        invite_id: str,
        token_version: int,
        team_id: str,
        email: str,
        password_hash: str | None,
        display_name: str | None = None,
        tier: str = "pro",
        email_verified: bool = False,
        workspace_id: str | None = None,
        workspace_role: str | None = None,
        invite_after_verify_next_route: str | None = None,
        federated_provider: str | None = None,
        federated_subject: str | None = None,
        avatar_url: str | None = None,
    ) -> User:
        """Create one invited identity and consume its server-side intent atomically."""
        invite_type = invite_type.strip().lower()
        email_norm = email.lower().strip()
        now = _now()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute(
                    "SELECT invite_type,invite_id,token_version FROM invite_intents "
                    "WHERE secret_hash = ? AND consumed_at IS NULL "
                    "AND invalidated_at IS NULL AND expires_at >= ?",
                    (intent_hash, now),
                ) as cur:
                    intent_row = await cur.fetchone()
                if (
                    intent_row is None
                    or intent_row["invite_type"] != invite_type
                    or intent_row["invite_id"] != invite_id
                    or intent_row["token_version"] != token_version
                ):
                    raise ValueError("invite intent invalid or expired")

                async with db.execute("SELECT 1 FROM users WHERE email = ?", (email_norm,)) as cur:
                    if await cur.fetchone() is not None:
                        raise ValueError("email already registered")

                async with db.execute("SELECT * FROM teams WHERE id = ?", (team_id,)) as cur:
                    team_row = await cur.fetchone()
                team = _row_to_team(team_row) if team_row else None
                if team is None:
                    raise ValueError(f"team not found: {team_id}")
                if self._seat_guard is not None:
                    active = await self.count_active_seats(team_id, conn=db)
                    await self._seat_guard.assert_can_add_seat(team_id, active_count=active)

                canonical_role = await _validate_invite_in_tx(
                    db,
                    invite_type=invite_type,
                    invite_id=invite_id,
                    team_id=team_id,
                    email_norm=email_norm,
                    now=now,
                    workspace_id=workspace_id,
                    workspace_role=workspace_role,
                    token_version=token_version,
                )
                oauth_providers = [federated_provider] if federated_provider else []
                quota = team.tier_credits.get(tier, 0)
                user = User(
                    id=_id("usr"),
                    team_id=team_id,
                    email=email_norm,
                    email_verified=email_verified,
                    display_name=display_name,
                    avatar_url=avatar_url,
                    password_hash=password_hash,
                    oauth_providers=oauth_providers,
                    tier=tier,
                    credit_quota=quota,
                    credit_balance=quota,
                    credit_period_start=now,
                    created_at=now,
                )
                preferences: dict[str, Any] = {}
                if invite_type == "workspace" and workspace_id:
                    preferences["last_workspace_id"] = workspace_id
                if invite_after_verify_next_route:
                    if not invite_after_verify_next_route.startswith("/"):
                        raise ValueError("invite_after_verify_next_route must be an app path")
                    preferences["invite_after_verify_next_route"] = invite_after_verify_next_route
                user.preferences = preferences
                await db.execute(
                    "INSERT INTO users(id,team_id,email,email_verified,display_name,avatar_url,"
                    "password_hash,oauth_providers_json,tier,credit_balance,credit_quota,"
                    "credit_period_start,locale,timezone,preferences_json,status,created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        user.id,
                        user.team_id,
                        user.email,
                        int(user.email_verified),
                        user.display_name,
                        user.avatar_url,
                        user.password_hash,
                        json.dumps(user.oauth_providers),
                        user.tier,
                        user.credit_balance,
                        user.credit_quota,
                        user.credit_period_start,
                        user.locale,
                        user.timezone,
                        json.dumps(user.preferences),
                        user.status,
                        user.created_at,
                    ),
                )
                await db.execute(
                    "INSERT INTO team_members(id,user_id,team_id,role,status,created_at) VALUES (?,?,?,?,?,?)",
                    (
                        _id("om"),
                        user.id,
                        team_id,
                        canonical_role if invite_type == "team" else "member",
                        "active",
                        now,
                    ),
                )
                if invite_type == "team":
                    await _apply_team_invite_workspace_grants_in_tx(
                        db,
                        invite_id=invite_id,
                        team_id=team_id,
                        user_id=user.id,
                        now=now,
                    )
                if federated_provider and federated_subject:
                    await db.execute(
                        "INSERT INTO user_federated_identities(provider,subject,user_id,email_at_link,created_at,last_login_at) VALUES (?,?,?,?,?,?)",
                        (
                            federated_provider,
                            federated_subject,
                            user.id,
                            email_norm,
                            now,
                            now,
                        ),
                    )
                if invite_type == "workspace" and workspace_id:
                    await db.execute(
                        "INSERT INTO workspace_members(workspace_id,user_id,role,invited_by,"
                        "joined_at,last_active_at,status) VALUES (?,?,?,?,?,?,'active')",
                        (
                            workspace_id,
                            user.id,
                            canonical_role,
                            None,
                            now,
                            now,
                        ),
                    )
                    consume = await db.execute(
                        "UPDATE invite_tokens SET status='consumed',consumed_at=?,"
                        "consumed_by_user_id=? WHERE id=? AND status='active' "
                        "AND token_version=?",
                        (now, user.id, invite_id, token_version),
                    )
                    await db.execute(
                        "UPDATE team_invite_tokens SET status='satisfied',satisfied_at=? WHERE team_id=? AND invited_email=? AND status='active'",
                        (now, team_id, email_norm),
                    )
                else:
                    consume = await db.execute(
                        "UPDATE team_invite_tokens SET status='consumed',consumed_at=?,"
                        "consumed_by_user_id=? WHERE id=? AND status='active' "
                        "AND token_version=?",
                        (now, user.id, invite_id, token_version),
                    )
                if consume.rowcount != 1:
                    raise ValueError("invite already consumed")
                intent_consume = await db.execute(
                    "UPDATE invite_intents SET consumed_at=? WHERE secret_hash=? AND consumed_at IS NULL AND invalidated_at IS NULL",
                    (now, intent_hash),
                )
                if intent_consume.rowcount != 1:
                    raise ValueError("invite intent already consumed")
                await db.commit()
                return user
            except Exception:
                await db.rollback()
                raise

    async def create_invited_user_atomic(
        self,
        *,
        invite_type: str,
        invite_id: str,
        team_id: str,
        email: str,
        password: str,
        display_name: str | None = None,
        tier: str = "pro",
        email_verified: bool = False,
        workspace_id: str | None = None,
        workspace_role: str | None = None,
        invite_after_verify_next_route: str | None = None,
    ) -> User:
        """Create an invitee and consume the invite in one transaction.

        This is deliberately narrower than ``create_user``: it covers only the invite
        registration path, where user creation, workspace grant, invite consumption, and
        invite recovery preferences must not leave a half-finished state.
        """
        invite_type = invite_type.strip().lower()
        email_norm = email.lower().strip()
        now = _now()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute(
                    "SELECT * FROM teams WHERE id = ?",
                    (team_id,),
                ) as cur:
                    team_row = await cur.fetchone()
                team = _row_to_team(team_row) if team_row else None
                if not team:
                    raise ValueError(f"team not found: {team_id}")
                if self._seat_guard is not None:
                    active = await self.count_active_seats(team_id, conn=db)
                    await self._seat_guard.assert_can_add_seat(team_id, active_count=active)

                canonical_role = await _validate_invite_in_tx(
                    db,
                    invite_type=invite_type,
                    invite_id=invite_id,
                    team_id=team_id,
                    email_norm=email_norm,
                    now=now,
                    workspace_id=workspace_id,
                    workspace_role=workspace_role,
                )

                quota = team.tier_credits.get(tier, 0)
                user = User(
                    id=_id("usr"),
                    team_id=team_id,
                    email=email_norm,
                    email_verified=email_verified,
                    display_name=display_name,
                    password_hash=hash_password(password),
                    tier=tier,
                    credit_quota=quota,
                    credit_balance=quota,
                    credit_period_start=now,
                    created_at=now,
                )
                prefs: dict[str, Any] = {}
                if invite_type == "workspace" and workspace_id:
                    prefs["last_workspace_id"] = workspace_id
                if invite_after_verify_next_route:
                    if not invite_after_verify_next_route.startswith("/"):
                        raise ValueError("invite_after_verify_next_route must be an app path")
                    prefs["invite_after_verify_next_route"] = invite_after_verify_next_route
                user.preferences = prefs
                await db.execute(
                    "INSERT INTO users(id,team_id,email,email_verified,display_name,avatar_url,"
                    "password_hash,oauth_providers_json,tier,credit_balance,credit_quota,"
                    "credit_period_start,locale,timezone,preferences_json,status,created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        user.id,
                        user.team_id,
                        user.email,
                        int(user.email_verified),
                        user.display_name,
                        user.avatar_url,
                        user.password_hash,
                        json.dumps(user.oauth_providers),
                        user.tier,
                        user.credit_balance,
                        user.credit_quota,
                        user.credit_period_start,
                        user.locale,
                        user.timezone,
                        json.dumps(user.preferences),
                        user.status,
                        user.created_at,
                    ),
                )
                await db.execute(
                    "INSERT INTO team_members(id,user_id,team_id,role,status,created_at) VALUES (?,?,?,?,?,?)",
                    (_id("om"), user.id, team_id, canonical_role if invite_type == "team" else "member", "active", now),
                )
                if invite_type == "team":
                    await _apply_team_invite_workspace_grants_in_tx(
                        db,
                        invite_id=invite_id,
                        team_id=team_id,
                        user_id=user.id,
                        now=now,
                    )
                if invite_type == "workspace" and workspace_id:
                    await db.execute(
                        "INSERT OR REPLACE INTO workspace_members"
                        "(workspace_id,user_id,role,invited_by,joined_at,last_active_at,status) "
                        "VALUES (?,?,?,?,?,?,'active')",
                        (
                            workspace_id,
                            user.id,
                            canonical_role,
                            None,
                            now,
                            now,
                        ),
                    )
                    await db.execute(
                        "UPDATE invite_tokens SET status = 'consumed', consumed_at = ? WHERE id = ? AND status = 'active'",
                        (now, invite_id),
                    )
                else:
                    await db.execute(
                        "UPDATE team_invite_tokens SET status = 'consumed', consumed_at = ? WHERE id = ? AND status = 'active'",
                        (now, invite_id),
                    )
                await db.commit()
                return user
            except Exception:
                await db.rollback()
                raise

    async def accept_invite_for_user_atomic(
        self,
        *,
        invite_type: str,
        invite_id: str,
        user_id: str,
        team_id: str,
        email: str,
        workspace_id: str | None = None,
        workspace_role: str | None = None,
    ) -> dict[str, Any]:
        """Accept an invite for an existing user in one transaction."""
        invite_type = invite_type.strip().lower()
        email_norm = email.lower().strip()
        now = _now()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                canonical_role = await _validate_invite_in_tx(
                    db,
                    invite_type=invite_type,
                    invite_id=invite_id,
                    team_id=team_id,
                    email_norm=email_norm,
                    now=now,
                    workspace_id=workspace_id,
                    workspace_role=workspace_role,
                )
                assignment_result: dict[str, Any] = {
                    "granted_workspace_count": 0,
                    "skipped_workspace_grants": [],
                }
                if invite_type == "team":
                    await db.execute(
                        "INSERT INTO team_members(id,user_id,team_id,role,status,created_at) VALUES (?,?,?,?,?,?) "
                        "ON CONFLICT(user_id,team_id) DO UPDATE SET status='active', removed_at=NULL",
                        (_id("om"), user_id, team_id, canonical_role, "active", now),
                    )
                    assignment_result = await _apply_team_invite_workspace_grants_in_tx(
                        db,
                        invite_id=invite_id,
                        team_id=team_id,
                        user_id=user_id,
                        now=now,
                    )
                    await db.execute(
                        "UPDATE team_invite_tokens SET status = 'consumed', consumed_at = ? WHERE id = ? AND status = 'active'",
                        (now, invite_id),
                    )
                    status = "already_member"
                else:
                    async with db.execute(
                        "SELECT role FROM workspace_members WHERE workspace_id = ? AND user_id = ? AND status = 'active'",
                        (workspace_id, user_id),
                    ) as cur:
                        existing_member = await cur.fetchone()
                    if existing_member is None:
                        await db.execute(
                            "INSERT OR REPLACE INTO workspace_members"
                            "(workspace_id,user_id,role,invited_by,joined_at,last_active_at,status) "
                            "VALUES (?,?,?,?,?,?,'active')",
                            (workspace_id, user_id, canonical_role, None, now, now),
                        )
                        status = "accepted"
                    else:
                        status = "already_member"
                    await db.execute(
                        "UPDATE invite_tokens SET status = 'consumed', consumed_at = ? WHERE id = ? AND status = 'active'",
                        (now, invite_id),
                    )
                    await db.execute(
                        "UPDATE users SET preferences_json = json_set(  coalesce(preferences_json, '{}'), '$.last_workspace_id', ?) WHERE id = ?",
                        (workspace_id, user_id),
                    )
                # Invite acceptance can activate Team membership, space
                # membership, or invite-bound space grants.  Fence the whole
                # Team in the same transaction; a harmless extra bump for an
                # already-active member is safer than retaining a stale stream.
                await self._bump_team_membership_epoch_conn(db, team_id, now=now)
                await db.commit()
                return {"status": status, **assignment_result}
            except Exception:
                await db.rollback()
                raise

    async def accept_invite_from_intent_atomic(
        self,
        *,
        intent_hash: str,
        invite_type: str,
        invite_id: str,
        token_version: int,
        user_id: str,
        target_team_id: str,
        workspace_id: str | None = None,
        workspace_role: str | None = None,
        multi_team_enabled: bool = False,
        federated_provider: str | None = None,
        federated_subject: str | None = None,
    ) -> dict[str, Any]:
        """Accept an invitation for an authenticated identity in one write lock."""
        now = _now()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute(
                    "SELECT id,team_id,email,status,oauth_providers_json FROM users WHERE id = ?",
                    (user_id,),
                ) as cur:
                    user_row = await cur.fetchone()
                if user_row is None or user_row["status"] != "active":
                    raise ValueError("identity blocked")
                email_norm = str(user_row["email"]).lower().strip()

                async with db.execute(
                    "SELECT invite_type,invite_id,token_version FROM invite_intents "
                    "WHERE secret_hash=? AND consumed_at IS NULL "
                    "AND invalidated_at IS NULL AND expires_at>=?",
                    (intent_hash, now),
                ) as cur:
                    intent_row = await cur.fetchone()
                if (
                    intent_row is None
                    or intent_row["invite_type"] != invite_type
                    or intent_row["invite_id"] != invite_id
                    or intent_row["token_version"] != token_version
                ):
                    raise ValueError("invite intent invalid or expired")

                canonical_role = await _validate_invite_in_tx(
                    db,
                    invite_type=invite_type,
                    invite_id=invite_id,
                    team_id=target_team_id,
                    email_norm=email_norm,
                    now=now,
                    workspace_id=workspace_id,
                    workspace_role=workspace_role,
                    token_version=token_version,
                )
                assignment_result: dict[str, Any] = {
                    "granted_workspace_count": 0,
                    "skipped_workspace_grants": [],
                }
                async with db.execute(
                    "SELECT status FROM team_members WHERE user_id=? AND team_id=?",
                    (user_id, target_team_id),
                ) as cur:
                    membership = await cur.fetchone()
                membership_status = membership["status"] if membership else None
                if membership_status != "active" and not multi_team_enabled and user_row["team_id"] != target_team_id:
                    raise ValueError("cross-team invite not allowed")
                membership_changed = membership_status != "active"
                if membership_changed:
                    if self._seat_guard is not None:
                        active = await self.count_active_seats(target_team_id, conn=db)
                        await self._seat_guard.assert_can_add_seat(
                            target_team_id,
                            active_count=active,
                        )
                    await db.execute(
                        "INSERT INTO team_members(id,user_id,team_id,role,status,created_at) "
                        "VALUES (?,?,?,?,?,?) ON CONFLICT(user_id,team_id) DO UPDATE SET "
                        "status='active',removed_at=NULL,removed_by=NULL",
                        (
                            _id("om"),
                            user_id,
                            target_team_id,
                            canonical_role if invite_type == "team" else "member",
                            "active",
                            now,
                        ),
                    )
                if membership_status == "removed":
                    result_status = "membership_restored"
                elif membership_status is None:
                    result_status = "accepted"
                else:
                    result_status = "already_member"

                if invite_type == "workspace" and workspace_id:
                    async with db.execute(
                        "SELECT role FROM workspace_members WHERE workspace_id=? AND user_id=? AND status='active'",
                        (workspace_id, user_id),
                    ) as cur:
                        existing_space_member = await cur.fetchone()
                    if existing_space_member is None:
                        await db.execute(
                            "INSERT INTO workspace_members(workspace_id,user_id,role,"
                            "invited_by,joined_at,last_active_at,status) "
                            "VALUES (?,?,?,?,?,?,'active') ON CONFLICT(workspace_id,user_id) "
                            "DO UPDATE SET status='active'",
                            (
                                workspace_id,
                                user_id,
                                canonical_role,
                                None,
                                now,
                                now,
                            ),
                        )
                        if not membership_changed:
                            result_status = "accepted"
                    consume = await db.execute(
                        "UPDATE invite_tokens SET status='consumed',consumed_at=?,"
                        "consumed_by_user_id=? WHERE id=? AND status='active' "
                        "AND token_version=?",
                        (now, user_id, invite_id, token_version),
                    )
                    await db.execute(
                        "UPDATE users SET preferences_json=json_set(coalesce(preferences_json,'{}'),'$.last_workspace_id',?) WHERE id=?",
                        (workspace_id, user_id),
                    )
                    await db.execute(
                        "UPDATE team_invite_tokens SET status='satisfied',satisfied_at=? WHERE team_id=? AND invited_email=? AND status='active'",
                        (now, target_team_id, email_norm),
                    )
                else:
                    assignment_result = await _apply_team_invite_workspace_grants_in_tx(
                        db,
                        invite_id=invite_id,
                        team_id=target_team_id,
                        user_id=user_id,
                        now=now,
                    )
                    consume = await db.execute(
                        "UPDATE team_invite_tokens SET status='consumed',consumed_at=?,"
                        "consumed_by_user_id=? WHERE id=? AND status='active' "
                        "AND token_version=?",
                        (now, user_id, invite_id, token_version),
                    )
                if consume.rowcount != 1:
                    raise ValueError("invite already consumed")
                intent_consume = await db.execute(
                    "UPDATE invite_intents SET consumed_at=? WHERE secret_hash=? AND consumed_at IS NULL AND invalidated_at IS NULL",
                    (now, intent_hash),
                )
                if intent_consume.rowcount != 1:
                    raise ValueError("invite intent already consumed")
                if federated_provider and federated_subject:
                    async with db.execute(
                        "SELECT user_id FROM user_federated_identities WHERE provider=? AND subject=?",
                        (federated_provider, federated_subject),
                    ) as cur:
                        linked = await cur.fetchone()
                    if linked is not None and linked["user_id"] != user_id:
                        raise ValueError("federated identity already linked")
                    await db.execute(
                        "INSERT INTO user_federated_identities(provider,subject,user_id,"
                        "email_at_link,created_at,last_login_at) VALUES (?,?,?,?,?,?) "
                        "ON CONFLICT(provider,subject) DO UPDATE SET "
                        "last_login_at=excluded.last_login_at",
                        (
                            federated_provider,
                            federated_subject,
                            user_id,
                            email_norm,
                            now,
                            now,
                        ),
                    )
                    providers = _json_list(user_row["oauth_providers_json"])
                    if federated_provider not in providers:
                        providers.append(federated_provider)
                    await db.execute(
                        "UPDATE users SET oauth_providers_json=?,email_verified=1 WHERE id=?",
                        (json.dumps(providers), user_id),
                    )
                await self._bump_team_membership_epoch_conn(
                    db,
                    target_team_id,
                    now=now,
                )
                await db.commit()
                return {"status": result_status, **assignment_result}
            except Exception:
                await db.rollback()
                raise

    async def get_user(self, user_id: str) -> User | None:
        # Runs on every authenticated request: borrow a pooled reader.
        async with open_pooled_read_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM users WHERE id = ?",
                (user_id,),
            ) as cur:
                row = await cur.fetchone()
        return _row_to_user(row) if row else None

    async def get_user_by_email(self, email: str) -> User | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM users WHERE email = ?",
                (normalize_email(email),),
            ) as cur:
                row = await cur.fetchone()
        return _row_to_user(row) if row else None

    async def get_user_by_federated_identity(
        self,
        provider: str,
        subject: str,
    ) -> User | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT u.* FROM user_federated_identities f JOIN users u ON u.id=f.user_id WHERE f.provider=? AND f.subject=?",
                (provider, subject),
            ) as cur:
                row = await cur.fetchone()
        return _row_to_user(row) if row else None

    async def link_federated_identity(
        self,
        *,
        user_id: str,
        provider: str,
        subject: str,
        verified_email: str,
    ) -> None:
        now = _now()
        email_norm = verified_email.lower().strip()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute(
                    "SELECT user_id FROM user_federated_identities WHERE provider=? AND subject=?",
                    (provider, subject),
                ) as cur:
                    existing = await cur.fetchone()
                if existing is not None and existing["user_id"] != user_id:
                    raise ValueError("federated identity already linked")
                async with db.execute(
                    "SELECT email,status,oauth_providers_json FROM users WHERE id=?",
                    (user_id,),
                ) as cur:
                    user_row = await cur.fetchone()
                if user_row is None or user_row["status"] != "active" or str(user_row["email"]).lower().strip() != email_norm:
                    raise ValueError("federated identity email mismatch")
                await db.execute(
                    "INSERT INTO user_federated_identities(provider,subject,user_id,"
                    "email_at_link,created_at,last_login_at) VALUES (?,?,?,?,?,?) "
                    "ON CONFLICT(provider,subject) DO UPDATE SET last_login_at=excluded.last_login_at",
                    (provider, subject, user_id, email_norm, now, now),
                )
                providers = _json_list(user_row["oauth_providers_json"])
                if provider not in providers:
                    providers.append(provider)
                    await db.execute(
                        "UPDATE users SET oauth_providers_json=?,email_verified=1 WHERE id=?",
                        (json.dumps(providers), user_id),
                    )
                await db.commit()
            except Exception:
                await db.rollback()
                raise

    async def list_team_memberships_for_user(
        self,
        user_id: str,
    ) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT id,team_id,role,status,created_at,removed_at,removed_by FROM team_members WHERE user_id=? ORDER BY created_at",
                (user_id,),
            ) as cur:
                rows = await cur.fetchall()
        return [dict(row) for row in rows]

    async def create_credential_recovery_token(
        self,
        *,
        raw_token: str,
        user_id: str,
        team_id: str,
        created_by_user_id: str,
        expires_at: str,
    ) -> None:
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
        now = _now()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute(
                    "SELECT status FROM users WHERE id=?",
                    (user_id,),
                ) as cur:
                    user_row = await cur.fetchone()
                if user_row is None or user_row["status"] != "active":
                    raise ValueError("target identity inactive")
                async with db.execute(
                    "SELECT status FROM team_members WHERE user_id=? AND team_id=?",
                    (user_id, team_id),
                ) as cur:
                    membership = await cur.fetchone()
                if membership is None or membership["status"] != "active":
                    raise ValueError("target is not an active team member")
                await db.execute(
                    "UPDATE credential_recovery_tokens SET status='revoked',revoked_at=? WHERE user_id=? AND status='active'",
                    (now, user_id),
                )
                await db.execute(
                    "UPDATE refresh_tokens SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL",
                    (now, user_id),
                )
                await db.execute(
                    "UPDATE users SET pwd_version=pwd_version+1,session_token_version=session_token_version+1 WHERE id=?",
                    (user_id,),
                )
                await db.execute(
                    "INSERT INTO credential_recovery_tokens(token_hash,user_id,team_id,"
                    "purpose,status,created_by_user_id,created_at,expires_at) "
                    "VALUES (?,?,?,?,'active',?,?,?)",
                    (
                        token_hash,
                        user_id,
                        team_id,
                        "password_reset",  # Frozen storage value; historical setup tokens remain valid.
                        created_by_user_id,
                        now,
                        expires_at,
                    ),
                )
                await self._bump_user_account_epoch_conn(db, user_id, now=now)
                await db.commit()
            except Exception:
                await db.rollback()
                raise

    async def get_credential_recovery_token(
        self,
        raw_token: str,
    ) -> dict[str, Any] | None:
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT token_hash,user_id,team_id,purpose,status,created_by_user_id,"
                "created_at,expires_at,consumed_at,revoked_at "
                "FROM credential_recovery_tokens WHERE token_hash=?",
                (token_hash,),
            ) as cur:
                row = await cur.fetchone()
        return dict(row) if row else None

    async def complete_credential_recovery(
        self,
        *,
        raw_token: str,
        password_hash: str,
    ) -> str:
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
        now = _now()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute(
                    "SELECT user_id,status,expires_at FROM credential_recovery_tokens WHERE token_hash=?",
                    (token_hash,),
                ) as cur:
                    row = await cur.fetchone()
                if row is None or row["status"] != "active" or row["expires_at"] < now:
                    raise ValueError("credential recovery token invalid or expired")
                user_id = str(row["user_id"])
                updated = await db.execute(
                    "UPDATE users SET password_hash=?,pwd_version=pwd_version+1,"
                    "session_token_version=session_token_version+1,"
                    "last_password_change_at=? WHERE id=? AND status='active'",
                    (password_hash, now, user_id),
                )
                if updated.rowcount != 1:
                    raise ValueError("target identity inactive")
                consumed = await db.execute(
                    "UPDATE credential_recovery_tokens SET status='consumed',consumed_at=? WHERE token_hash=? AND status='active'",
                    (now, token_hash),
                )
                if consumed.rowcount != 1:
                    raise ValueError("credential recovery token replayed")
                await db.execute(
                    "UPDATE refresh_tokens SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL",
                    (now, user_id),
                )
                await self._bump_user_account_epoch_conn(db, user_id, now=now)
                await db.commit()
                return user_id
            except Exception:
                await db.rollback()
                raise

    async def revoke_credential_recovery_token(
        self,
        *,
        user_id: str,
        team_id: str,
    ) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                "UPDATE credential_recovery_tokens SET status='revoked',revoked_at=? WHERE user_id=? AND team_id=? AND status='active'",
                (_now(), user_id, team_id),
            )
            await db.commit()
            return cur.rowcount

    async def get_users_by_ids(self, user_ids: list[str]) -> dict[str, str]:
        """Batch-resolve user ids → display name (best-effort), avoiding N+1.
        Returns ``id → (display_name or email-local or id)``; ids absent from the
        table are simply omitted (callers fall back to the raw id). Used to turn
        raw ``usr_*`` ids into人名 in shared-report lists + share rosters (0708 ⑤)."""
        ids = [u for u in dict.fromkeys(user_ids) if u]  # dedup, drop empties, keep order
        if not ids:
            return {}
        placeholders = ",".join("?" for _ in ids)
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                f"SELECT id, display_name, email FROM users WHERE id IN ({placeholders})",  # noqa: S608 — placeholders only, values bound
                ids,
            ) as cur:
                rows = await cur.fetchall()
        out: dict[str, str] = {}
        for r in rows:
            local = (r["email"] or "").partition("@")[0]
            out[r["id"]] = r["display_name"] or local or r["id"]
        return out

    async def get_user_display_names_by_ids(
        self,
        user_ids: list[str],
    ) -> dict[str, str]:
        """Resolve explicit display names without deriving identity from email.

        Security summaries use this stricter variant because an email local-part
        is not an approved fallback for catalog disclosure.
        """
        ids = [user_id for user_id in dict.fromkeys(user_ids) if user_id]
        if not ids:
            return {}
        placeholders = ",".join("?" for _ in ids)
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                f"SELECT id, display_name FROM users WHERE id IN ({placeholders})",  # noqa: S608 — placeholders only, values bound
                ids,
            ) as cur:
                rows = await cur.fetchall()
        return {str(row["id"]): str(row["display_name"]) for row in rows if row["display_name"]}

    async def get_team_member_labels(self, team_id: str, user_ids: list[str]) -> dict[str, str]:
        """同团队成员的界面名称：显示名，缺省用邮箱 @ 前部分（与 ``list_team_directory`` 同规则）。

        只解析该团队的成员（含已离开者，他们上传的文件仍在），不在团队内的 id 不返回——
        调用方据此不会把其他团队成员的名字带进本团队的界面。
        """
        ids = [user_id for user_id in dict.fromkeys(user_ids) if user_id]
        if not team_id or not ids:
            return {}
        placeholders = ",".join("?" for _ in ids)
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT DISTINCT u.id, u.display_name, u.email FROM users AS u "
                "JOIN team_members AS tm ON tm.user_id=u.id "
                f"WHERE tm.team_id = ? AND u.id IN ({placeholders})",  # noqa: S608 — placeholders only, values bound
                [team_id, *ids],
            ) as cur:
                rows = await cur.fetchall()
        labels: dict[str, str] = {}
        for row in rows:
            label = row["display_name"] or (row["email"] or "").partition("@")[0]
            if label:
                labels[str(row["id"])] = str(label)
        return labels

    async def update_last_active(self, user_id: str) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE users SET last_active_at = ? WHERE id = ?",
                (_now(), user_id),
            )
            await db.commit()

    # ─── workspace_members (P4.5.6) ─────────────────────

    async def add_member(
        self,
        *,
        workspace_id: str,
        user_id: str,
        role: str = "member",
        invited_by: str | None = None,
    ) -> None:
        canonical_role = _canonical_workspace_member_role(role) or "member"
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            async with db.execute(
                "SELECT w.team_id AS workspace_team_id, w.status AS workspace_status, "
                "u.team_id AS legacy_team_id, "
                "EXISTS(SELECT 1 FROM team_members om WHERE om.user_id = ? AND om.team_id = w.team_id AND om.status = 'active') AS is_team_member "
                "FROM workspaces w JOIN users u ON u.id = ? WHERE w.id = ?",
                (user_id, user_id, workspace_id),
            ) as cur:
                row = await cur.fetchone()
            if row and row["workspace_status"] != "active":
                raise SpaceGovernanceError("workspace_archived")
            if row and not row["is_team_member"] and row["workspace_team_id"] != row["legacy_team_id"]:
                raise WorkspaceTeamMismatchError
            await db.execute(
                "INSERT OR REPLACE INTO workspace_members(workspace_id,user_id,role,invited_by,joined_at,status) VALUES (?,?,?,?,?,'active')",
                (workspace_id, user_id, canonical_role, invited_by, _now()),
            )
            if row is None:
                await db.rollback()
                raise ValueError("workspace or user is missing")
            await self._bump_team_membership_epoch_conn(
                db,
                str(row["workspace_team_id"]),
            )
            await db.commit()

    async def remove_member(self, workspace_id: str, user_id: str) -> bool:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            team_id = await self._workspace_team_id_conn(db, workspace_id)
            cur = await db.execute(
                "UPDATE workspace_members SET status = 'removed' WHERE workspace_id = ? AND user_id = ? AND status = 'active'",
                (workspace_id, user_id),
            )
            if cur.rowcount > 0:
                await self._bump_team_membership_epoch_conn(db, team_id)
            await db.commit()
            return cur.rowcount > 0

    async def get_member_role(
        self,
        workspace_id: str,
        user_id: str,
    ) -> str | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT role FROM workspace_members WHERE workspace_id = ? AND user_id = ? AND status = 'active'",
                (workspace_id, user_id),
            ) as cur:
                row = await cur.fetchone()
        return _canonical_workspace_member_role(row["role"]) if row else None

    async def has_member_record(self, workspace_id: str, user_id: str) -> bool:
        """Return whether any active or removed membership row exists.

        Unlike ``get_member_role``, this preserves removal history so first-send
        enrollment cannot silently restore a deliberately removed member.
        """
        async with (
            aiosqlite.connect(self.db_path) as db,
            db.execute(
                "SELECT 1 FROM workspace_members WHERE workspace_id = ? AND user_id = ?",
                (workspace_id, user_id),
            ) as cur,
        ):
            row = await cur.fetchone()
        return row is not None

    async def active_member_roles_for_user(self, user_id: str) -> dict[str, str | None]:
        """`get_member_role` 的批量版：该用户全部 active 成员行 → ``{workspace_id: 规范化角色}``。

        与单条版同一个 WHERE、同一个规范化；不在字典里的空间，单条版返回 None。给「一次请求要判很多空间」
        的调用方（全局搜索）用，免得每个空间各开一次库。
        """
        async with (
            aiosqlite.connect(self.db_path) as db,
            db.execute(
                "SELECT workspace_id, role FROM workspace_members WHERE user_id = ? AND status = 'active'",
                (user_id,),
            ) as cur,
        ):
            rows = await cur.fetchall()
        return {str(workspace_id): _canonical_workspace_member_role(role) for workspace_id, role in rows}

    async def workspaces_for_team(self, team_id: str) -> dict[str, dict[str, Any]]:
        """`get_workspace` 的批量版：某团队全部空间行（列与 `get_workspace` 相同）→ ``{workspace_id: 行}``。"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT id, team_id, name, name_key, description, status, revision, "
                "datarc_workspace_id, created_at, updated_at FROM workspaces WHERE team_id = ?",
                (team_id,),
            ) as cur:
                rows = await cur.fetchall()
        return {str(row["id"]): dict(row) for row in rows}

    async def get_workspace_team_id(self, workspace_id: str) -> str | None:
        """ws-datarc-decouple [review P1-c]: the owning team of a Bowhead workspace, for the
        cross-team authz leg. ``get_member_role`` keys only on (workspace_id, user_id) and does
        NOT verify the workspace belongs to the caller's team — so authorize_ws_access needs the
        real ws_team_id to reject a stale/cross-team member row under enforce. Returns None when no
        ``workspaces`` row exists (legacy datarc id / pre-migration) → caller passes ws_team_id=None,
        which skips ONLY the team-equality leg (membership is still required)."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT team_id FROM workspaces WHERE id = ?",
                (workspace_id,),
            ) as cur:
                row = await cur.fetchone()
        return row["team_id"] if row else None

    async def list_team_directory(
        self,
        team_id: str,
        *,
        q: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Content sharing 选人器（tech-design §5）：同 team active 用户的最小目录。
        只返 id / display_name / email 掩码（首字符+***+@domain — 企业内部目录,
        同 team 成员互见姓名是产品语义）;分页;q 对 display_name/email 前缀过滤。"""
        where = [
            "tm.team_id = ?",
            "tm.status = 'active'",
            "u.status = 'active'",
        ]
        params: list[Any] = [team_id]
        if q:
            where.append("(u.display_name LIKE ? OR u.email LIKE ?)")
            params.extend([f"%{q}%", f"{q}%"])
        params.extend([max(1, min(200, limit)), max(0, offset)])
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT u.id, u.display_name, u.email "
                "FROM users AS u "
                "JOIN team_members AS tm ON tm.user_id=u.id "
                f"WHERE {' AND '.join(where)} "  # noqa: S608 — clauses fixed, values bound
                "ORDER BY u.display_name ASC, u.email ASC LIMIT ? OFFSET ?",
                params,
            ) as cur:
                rows = await cur.fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            email = r["email"] or ""
            local, _, domain = email.partition("@")
            masked = f"{local[:1]}***@{domain}" if domain else ""
            out.append(
                {
                    "id": r["id"],
                    "display_name": r["display_name"] or local or r["id"],
                    "email_masked": masked,
                }
            )
        return out

    async def team_member_emails(self, team_id: str) -> set[str]:
        """Lower-cased raw emails of the team's active members. Used as the delivery allowlist for
        scheduled-routine email recipients (block exfil of a workspace's analysis to arbitrary external
        addresses). Raw (not masked like list_team_directory) because it's an exact-match allowlist."""
        if not team_id:
            return set()
        async with (
            aiosqlite.connect(self.db_path) as db,
            db.execute(
                "SELECT email FROM users WHERE team_id = ? AND status = 'active' AND email IS NOT NULL",
                (team_id,),
            ) as cur,
        ):
            rows = await cur.fetchall()
        return {str(r[0]).strip().lower() for r in rows if r[0]}

    async def workspace_in_team(
        self,
        team_id: str,
        workspace_ref: str,
        *,
        active_only: bool = False,
    ) -> bool:
        """Content sharing (report ownership adapter, tech-design §3): does this
        workspace reference belong to ``team_id``? Accepts BOTH identities of the
        dual-id model — the Bowhead ``ws_*`` id and the legacy datarc numeric id
        (canvases created pre-migration may carry either in ``workspace_id``)."""
        if not team_id or not workspace_ref:
            return False
        query = "SELECT 1 FROM workspaces WHERE team_id = ? AND (id = ? OR datarc_workspace_id = ?)"
        if active_only:
            query += " AND status = 'active'"
        query += " LIMIT 1"
        async with aiosqlite.connect(self.db_path) as db, db.execute(query, (team_id, workspace_ref, workspace_ref)) as cur:
            row = await cur.fetchone()
        return row is not None

    async def resolve_workspace_id_for_team(
        self,
        team_id: str,
        workspace_ref: str,
    ) -> str | None:
        """Resolve a canonical Bowhead Space id from either frozen identifier."""

        if not team_id or not workspace_ref:
            return None
        async with (
            aiosqlite.connect(self.db_path) as db,
            db.execute(
                "SELECT id FROM workspaces WHERE team_id = ? AND (id = ? OR datarc_workspace_id = ?) LIMIT 1",
                (team_id, workspace_ref, workspace_ref),
            ) as cur,
        ):
            row = await cur.fetchone()
        return str(row[0]) if row else None

    async def list_members(self, workspace_id: str) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT m.user_id, m.role, m.joined_at, u.email, u.display_name "
                "FROM workspace_members m JOIN users u ON u.id = m.user_id "
                "WHERE m.workspace_id = ? AND m.status = 'active'",
                (workspace_id,),
            ) as cur:
                rows = await cur.fetchall()
        return [
            {
                "user_id": r["user_id"],
                "role": _canonical_workspace_member_role(r["role"]),
                "email": r["email"],
                "display_name": r["display_name"],
                "joined_at": r["joined_at"],
            }
            for r in rows
        ]

    async def list_workspace_member_candidates(self, team_id: str, workspace_id: str) -> list[dict[str, Any]]:
        """Return active Team members that do not already belong to the Space."""

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT u.id AS user_id, u.email, u.display_name "
                "FROM team_members tm JOIN users u ON u.id = tm.user_id "
                "WHERE tm.team_id = ? AND tm.status = 'active' AND u.status = 'active' "
                "AND NOT EXISTS (SELECT 1 FROM workspace_members wm "
                "WHERE wm.workspace_id = ? AND wm.user_id = u.id AND wm.status = 'active') "
                "ORDER BY COALESCE(u.display_name, ''), u.email",
                (team_id, workspace_id),
            ) as cur:
                rows = await cur.fetchall()
        return [dict(row) for row in rows]

    async def list_active_team_members_for_governance(
        self,
        team_id: str,
    ) -> list[dict[str, Any]]:
        """Return the active Team directory for Team-level Space governance.

        This deliberately lives beside the Space membership helpers: Team
        administrators may assign a Space administrator, but that does not
        grant the Team administrator content access to the Space.
        """
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT u.id AS user_id, u.email, u.display_name "
                "FROM team_members tm JOIN users u ON u.id = tm.user_id "
                "WHERE tm.team_id = ? AND tm.status = 'active' AND u.status = 'active' "
                "ORDER BY COALESCE(u.display_name, ''), u.email",
                (team_id,),
            ) as cur:
                rows = await cur.fetchall()
        return [dict(row) for row in rows]

    async def list_member_workspaces(self, user_id: str) -> list[dict[str, Any]]:
        """P4.5.23: list workspaces this user is active member of."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT workspace_id, role, joined_at FROM workspace_members WHERE user_id = ? AND status = 'active' ORDER BY joined_at",
                (user_id,),
            ) as cur:
                rows = await cur.fetchall()
        return [{**dict(r), "role": _canonical_workspace_member_role(r["role"])} for r in rows]

    async def update_member_role(
        self,
        workspace_id: str,
        user_id: str,
        new_role: str,
    ) -> bool:
        canonical_role = _canonical_workspace_member_role(new_role)
        if canonical_role not in ("admin", "member", "viewer"):
            return False
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            team_id = await self._workspace_team_id_conn(db, workspace_id)
            cur = await db.execute(
                "UPDATE workspace_members SET role = ? WHERE workspace_id = ? AND user_id = ? AND status = 'active'",
                (canonical_role, workspace_id, user_id),
            )
            if cur.rowcount > 0:
                await self._bump_team_membership_epoch_conn(db, team_id)
            await db.commit()
            return cur.rowcount > 0

    async def count_active_workspace_admins(self, workspace_id: str) -> int:
        """followup-context-deck #2: 数 workspace 里 active 的 admin 数.

        只数 active user + active workspace_members, 用于 update_member_role /
        remove_member 的 last-admin 保护: 不让最后一个 admin
        被降级/移除, 否则 workspace 0 admin → 谁都过不了 _require_workspace_admin,
        再没人能管理成员 (mirror team 级 is_only_remaining_team_admin)."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT COUNT(*) AS n "
                "FROM workspace_members wm "
                "JOIN users u ON u.id = wm.user_id AND u.status = 'active' "
                "WHERE wm.workspace_id = ? "
                "AND wm.role IN ('admin', 'ws_admin', 'owner', 'workspace_admin') "
                "AND wm.status = 'active'",
                (workspace_id,),
            ) as cur:
                row = await cur.fetchone()
        return int(row["n"]) if row else 0

    # ─── team admin (P4.5.20) ───────────────────────────

    async def set_team_creator(self, team_id: str, user_id: str) -> bool:
        """Mark user as team creator. Atomic idempotent: only sets if NULL.

        Sprint α: returns True if THIS call actually set creator,
        False if creator was already set (race lost). PRD r4 G6.
        """
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                cur = await db.execute(
                    "UPDATE teams SET created_by_user_id = ? WHERE id = ? AND created_by_user_id IS NULL",
                    (user_id, team_id),
                )
                changed = cur.rowcount > 0
                if changed:
                    await self._bump_team_membership_epoch_conn(db, team_id)
                await db.commit()
                return changed
            except Exception:
                await db.rollback()
                raise

    async def is_team_admin(self, user_id: str, team_id: str) -> bool:
        """True if user is the owner or an active team_admin in the given Team.

        SP-AUTH-3 (概念术语审计 P0-2): ``team_id`` 必传. 旧的 ``team_id=None``
        分支按 ``users.team_id``(主队) 判角色, 在 FEATURE_MULTI_TEAM_AUTHZ 下会把
        主队管理员身份带进当前团队 (跨团队提权). 调用方必须传解析后的当前团队;
        授权体系.md §8.1: 禁止直接以 users.team_id 推断当前 Team.
        """
        if not team_id:
            return False
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            query = (
                "SELECT o.created_by_user_id, om.role AS team_member_role FROM teams o "
                "LEFT JOIN team_members om ON om.team_id = o.id AND om.user_id = ? AND om.status = 'active' "
                "WHERE o.id = ?"
            )
            async with db.execute(query, (user_id, team_id)) as cur:
                row = await cur.fetchone()
        return bool(row and (row["created_by_user_id"] == user_id or row["team_member_role"] == "team_admin"))

    async def find_default_team_for_domain(self, email: str) -> Team | None:
        """Sprint α G6: 白名单 domain → 已 provisioned team. None 时 caller 不该 auto-create.

        Returns the OLDEST matching active team for the given email's domain.
        """
        if "@" not in email:
            return None
        domain = email.rsplit("@", 1)[-1].lower().strip()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM teams WHERE LOWER(domain) = ? AND status = 'active' ORDER BY created_at ASC LIMIT 1",
                (domain,),
            ) as cur:
                row = await cur.fetchone()
        if not row:
            return None
        return _row_to_team(row)

    async def get_user_team_id(self, user_id: str) -> str | None:
        """Sprint α G9 Sec P0-S-6: re-SELECT user.team_id from DB (不信 JWT stale claim)."""
        async with (
            aiosqlite.connect(self.db_path) as db,
            db.execute(
                "SELECT team_id FROM users WHERE id = ? AND status = 'active'",
                (user_id,),
            ) as cur,
        ):
            row = await cur.fetchone()
        return row[0] if row else None

    async def list_active_teams_for_user(self, user_id: str) -> list[dict[str, str]]:
        """Return the active Team memberships for a global User identity."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT o.id, o.name, CASE WHEN o.created_by_user_id = ? THEN 'owner' ELSE om.role END AS role "
                "FROM team_members om JOIN teams o ON o.id = om.team_id "
                "JOIN users u ON u.id = om.user_id "
                "WHERE om.user_id = ? AND om.status = 'active' AND u.status = 'active' AND o.status = 'active' "
                "ORDER BY o.created_at, o.id",
                (user_id, user_id),
            ) as cur:
                rows = await cur.fetchall()
        return [{"id": str(row["id"]), "name": str(row["name"]), "role": str(row["role"])} for row in rows]

    async def user_has_active_team_membership(self, user_id: str, team_id: str) -> bool:
        async with (
            aiosqlite.connect(self.db_path) as db,
            db.execute(
                "SELECT 1 FROM team_members WHERE user_id = ? AND team_id = ? AND status = 'active'",
                (user_id, team_id),
            ) as cur,
        ):
            return await cur.fetchone() is not None

    async def set_active_team(self, user_id: str, team_id: str) -> bool:
        if not await self.user_has_active_team_membership(user_id, team_id):
            return False
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute("UPDATE users SET active_team_id = ? WHERE id = ?", (team_id, user_id))
            await db.commit()
        return cur.rowcount == 1

    async def get_team_creator_id(self, team_id: str) -> str | None:
        """Sprint α G6: peek team's created_by_user_id. None if unset or team missing.

        Used by register flow bootstrap detection — separated for testability +
        ensures fresh DB read (not cached).
        """
        async with (
            aiosqlite.connect(self.db_path) as db,
            db.execute(
                "SELECT created_by_user_id FROM teams WHERE id = ?",
                (team_id,),
            ) as cur,
        ):
            row = await cur.fetchone()
        return row[0] if row else None

    async def transfer_team_owner(self, team_id: str, from_user_id: str, to_user_id: str) -> None:
        """Atomically transfer the single team owner to an active member.

        SP-AUTH-6 (概念术语审计 P1-1): 目标校验按本团队 active team_members 行判,
        不再按 users.team_id(主队) — 多团队下 owner 可转给「主队在别处」的成员。
        """
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute(
                    "SELECT 1 FROM team_members om JOIN users u ON u.id = om.user_id "
                    "WHERE om.user_id = ? AND om.team_id = ? AND om.status = 'active' AND u.status = 'active'",
                    (to_user_id, team_id),
                ) as cur:
                    if await cur.fetchone() is None:
                        raise TeamOwnerTransferError("owner_transfer_target_not_active")
                cur = await db.execute(
                    "UPDATE teams SET created_by_user_id = ? WHERE id = ? AND created_by_user_id = ?", (to_user_id, team_id, from_user_id)
                )
                if cur.rowcount != 1:
                    raise TeamOwnerTransferError("owner_transfer_conflict")
                await db.execute(
                    "INSERT INTO team_members(id,user_id,team_id,role,status,created_at) VALUES (?,?,?,?,?,?) ON CONFLICT(user_id,team_id) DO UPDATE SET role='team_admin', status='active'",
                    (_id("om"), from_user_id, team_id, "team_admin", "active", _now()),
                )
                await self._bump_team_membership_epoch_conn(db, team_id)
                await db.commit()
            except Exception:
                await db.rollback()
                raise

    # ─── Sprint α G3: email verify jti ──────────────────

    async def update_verify_token_jti(self, user_id: str, jti: str | None) -> bool:
        """Set verify_token_jti. Pass None to clear after consume.

        Used by G3 resend (set new jti, supersede old) + consume (clear after success).
        """
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                "UPDATE users SET verify_token_jti = ? WHERE id = ?",
                (jti, user_id),
            )
            await db.commit()
            return cur.rowcount > 0

    async def get_verify_token_jti(self, user_id: str) -> str | None:
        async with (
            aiosqlite.connect(self.db_path) as db,
            db.execute(
                "SELECT verify_token_jti FROM users WHERE id = ?",
                (user_id,),
            ) as cur,
        ):
            row = await cur.fetchone()
        return row[0] if row else None

    async def mark_email_verified_atomic(
        self,
        user_id: str,
        jti: str | None,
    ) -> bool:
        """Atomic verify. Returns True iff this call flipped 0→1.

        Backward compat (Q8 locked): jti=None → match any current jti (old users
        without jti can still verify; sprint β strict).

        After success, jti is cleared (set to NULL).
        """
        async with aiosqlite.connect(self.db_path) as db:
            if jti is None:
                # Old user compat — verify token without jti claim
                cur = await db.execute(
                    "UPDATE users SET email_verified = 1, verify_token_jti = NULL WHERE id = ? AND email_verified = 0",
                    (user_id,),
                )
            else:
                cur = await db.execute(
                    "UPDATE users SET email_verified = 1, verify_token_jti = NULL "
                    "WHERE id = ? AND email_verified = 0 AND "
                    "(verify_token_jti = ? OR verify_token_jti IS NULL)",
                    (user_id, jti),
                )
            await db.commit()
            return cur.rowcount > 0

    # ─── Sprint α G4: datarc member_uid ──────────────────

    async def update_user_datarc_member_uid(
        self,
        user_id: str,
        member_uid: str,
    ) -> bool:
        """G4: write datarc member_uid to users column after lookup success."""
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                "UPDATE users SET datarc_member_uid = ? WHERE id = ?",
                (member_uid, user_id),
            )
            await db.commit()
            return cur.rowcount > 0

    # ─── Sprint α G3: SMTP outbox ──────────────────────────

    async def enqueue_smtp(
        self,
        *,
        to_email: str,
        subject: str,
        body: str,
        purpose: str,
        team_id: str | None = None,
        html_body: str | None = None,
        public_entry_id: str | None = None,
        brand_revision_id: str | None = None,
        branding_snapshot: str | None = None,
    ) -> str:
        """G3 Ops P0-O-3: queue email for outbox worker (NOT direct send).

        email-config sprint: team_id 透传 → worker 按行解析 team-level SMTP(NULL=env 兜底)。
        Returns outbox_id.
        """
        oid = _id("smtp")
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO smtp_outbox "
                "(id, to_email, subject, body, purpose, status, "
                " attempts, created_at, next_attempt_at, team_id, html_body, "
                " public_entry_id, brand_revision_id, branding_snapshot) "
                "VALUES (?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?, ?, ?, ?, ?)",
                (
                    oid,
                    to_email,
                    subject,
                    body,
                    purpose,
                    _now(),
                    _now(),
                    team_id,
                    html_body,
                    public_entry_id,
                    brand_revision_id,
                    branding_snapshot,
                ),
            )
            await db.commit()
        return oid

    async def requeue_dead_smtp(self, team_id: str | None) -> int:
        """email-config sprint: 把该 team 的 dead 行复活成 failed_retry(重新进 claim 队列)。

        场景:team 配错 SMTP → 5 次重试后死信;admin 改好配置/测试成功后调本方法补发滞留的
        验证/邀请/通知邮件(tech-design §5 / security #9 — 修 worker docstring 与现实矛盾)。
        team_id None → 只复活 NULL-team(env 兜底)行。返回复活行数。

        review-fix(P2):只复活近 7 天的死信 —— 验证/邀请邮件内嵌 7d JWT 链接,更老的行
        即使重发也是死链,徒增困惑(配置坏 2+ 周后才修复的场景)。
        """
        from datetime import datetime, timedelta

        cutoff = (datetime.now(UTC) - timedelta(days=7)).isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            if team_id is None:
                cur = await db.execute(
                    "UPDATE smtp_outbox SET status = 'failed_retry', attempts = 0, "
                    "next_attempt_at = ? WHERE status = 'dead' AND team_id IS NULL "
                    "AND created_at >= ?",
                    (_now(), cutoff),
                )
            else:
                cur = await db.execute(
                    "UPDATE smtp_outbox SET status = 'failed_retry', attempts = 0, "
                    "next_attempt_at = ? WHERE status = 'dead' AND team_id = ? "
                    "AND created_at >= ?",
                    (_now(), team_id, cutoff),
                )
            await db.commit()
            return cur.rowcount

    async def claim_pending_smtp(self, batch_size: int = 10) -> list[dict[str, Any]]:
        """Atomic claim: SELECT pending+failed_retry → mark 'sending' → return.

        Worker loop pattern. next_attempt_at filters backoff retries.

        CTO P1-T-1 fix: BEGIN IMMEDIATE acquires write-lock before SELECT to
        prevent double-claim if sprint β ever spawns multiple workers.
        Currently single-worker invariant per Q3 — defense in depth.
        """
        now = _now()
        rows: list[dict[str, Any]] = []
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute(
                    "SELECT * FROM smtp_outbox "
                    "WHERE status IN ('pending', 'failed_retry') "
                    "AND (next_attempt_at IS NULL OR next_attempt_at <= ?) "
                    "ORDER BY created_at ASC LIMIT ?",
                    (now, batch_size),
                ) as cur:
                    async for row in cur:
                        rows.append(dict(row))
                for r in rows:
                    await db.execute(
                        "UPDATE smtp_outbox SET status = 'sending', attempts = attempts + 1 WHERE id = ?",
                        (r["id"],),
                    )
                await db.commit()
            except Exception:
                await db.rollback()
                raise
        return rows

    async def mark_smtp_sent(self, outbox_id: str) -> bool:
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                "UPDATE smtp_outbox SET status = 'sent', sent_at = ?, last_error = NULL WHERE id = ?",
                (_now(), outbox_id),
            )
            await db.commit()
            return cur.rowcount > 0

    async def mark_smtp_failed(
        self,
        outbox_id: str,
        error: str,
        next_attempt_at: str | None,
    ) -> bool:
        """Mark failed_retry (if next_attempt_at given) or dead (None)."""
        new_status = "failed_retry" if next_attempt_at else "dead"
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                "UPDATE smtp_outbox SET status = ?, last_error = ?, next_attempt_at = ? WHERE id = ?",
                (new_status, error, next_attempt_at, outbox_id),
            )
            await db.commit()
            return cur.rowcount > 0

    # ─── Sprint α G9: SQLite-backed rate limit ─────────────

    async def rate_limit_check(
        self,
        bucket_key: str,
        *,
        limit: int,
        window_s: int,
    ) -> bool:
        """Atomic check-and-increment via BEGIN IMMEDIATE. Returns True if allowed.

        Uses time-bucket strategy: window_start = current_time // window_s * window_s.
        BEGIN IMMEDIATE acquires write-lock immediately → serializes concurrent calls
        on same SQLite db file (sprint α single-worker is safe; sprint β multi-worker
        would also be safe).

        Per CTO P0-S-2 fix: previous version did SELECT then UPDATE in separate
        statements → race window let N concurrent callers all read count=N and all
        increment, effectively N×concurrency leak.
        """
        import time

        now = int(time.time())
        window_start = (now // window_s) * window_s
        async with aiosqlite.connect(self.db_path) as db:
            # BEGIN IMMEDIATE — acquire write-lock before SELECT to prevent race
            await db.execute("BEGIN IMMEDIATE")
            try:
                db.row_factory = aiosqlite.Row
                async with db.execute(
                    "SELECT count FROM rate_limit_buckets WHERE bucket_key = ? AND window_start = ?",
                    (bucket_key, window_start),
                ) as cur:
                    row = await cur.fetchone()
                current = row["count"] if row else 0
                if current >= limit:
                    await db.rollback()
                    return False
                if row:
                    await db.execute(
                        "UPDATE rate_limit_buckets SET count = count + 1 WHERE bucket_key = ? AND window_start = ?",
                        (bucket_key, window_start),
                    )
                else:
                    await db.execute(
                        "INSERT INTO rate_limit_buckets (bucket_key, window_start, count) VALUES (?, ?, 1)",
                        (bucket_key, window_start),
                    )
                # opportunistic cleanup of old buckets (1% of calls)
                import random

                if random.random() < 0.01:
                    await db.execute(
                        "DELETE FROM rate_limit_buckets WHERE window_start < ?",
                        (now - 86400,),
                    )
                await db.commit()
                return True
            except Exception:
                await db.rollback()
                raise

    # ─── Sprint β G8 — admin_audit_log + D-3=B tiered atomicity ─────

    async def write_admin_audit_log(
        self,
        *,
        actor_user_id: str,
        team_id: str,
        action: str,
        target_kind: str | None = None,
        target_id: str | None = None,
        target_team_id: str | None = None,
        scope: str,
        payload: dict[str, Any] | None = None,
        ip: str | None = None,
        user_agent: str | None = None,
        conn: Any | None = None,
        audit_id: str | None = None,
    ) -> bool:
        """G8 audit log write.

        Two modes:
        - `conn=None` (default, best-effort): opens own connection, catches all
          exceptions → log warning + fallback file. Returns True on success.
        - `conn=<aiosqlite.Connection>` (atomic mode): writes via caller's tx;
          caller is responsible for commit/rollback.

        P2-S-4: payload >5KB truncated by replacing largest string value with
        placeholder; `_truncated: True` flag added.
        """
        payload_dict = dict(payload or {})
        payload_str = json.dumps(payload_dict, ensure_ascii=False)
        if len(payload_str) > 5000:
            # Find largest string value and truncate
            largest_key = None
            largest_len = 0
            for k, v in payload_dict.items():
                if isinstance(v, str) and len(v) > largest_len:
                    largest_key = k
                    largest_len = len(v)
            if largest_key is not None:
                payload_dict[largest_key] = payload_dict[largest_key][:500] + "...[truncated]"
            payload_dict["_truncated"] = True
            payload_str = json.dumps(payload_dict, ensure_ascii=False)

        if audit_id is not None and (not isinstance(audit_id, str) or not audit_id):
            raise ValueError("admin audit identity is invalid")
        if audit_id is not None and conn is None:
            raise ValueError("stable admin audit identity requires a transaction")
        row_id = audit_id or _id("aud")
        sql = (
            "INSERT INTO admin_audit_log "
            "(id, actor_user_id, team_id, action, target_kind, target_id, target_team_id, "
            " scope, payload_json, ts, ip, user_agent) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        )
        params = (
            row_id,
            actor_user_id,
            team_id,
            action,
            target_kind,
            target_id,
            target_team_id,
            scope,
            payload_str,
            _now(),
            ip,
            user_agent,
        )

        if conn is not None:
            # Atomic mode — caller tx
            if audit_id is not None:
                async with conn.execute(
                    "SELECT * FROM admin_audit_log WHERE id=?",
                    (audit_id,),
                ) as cursor:
                    existing = await cursor.fetchone()
                if existing is not None:
                    expected = {
                        "actor_user_id": actor_user_id,
                        "team_id": team_id,
                        "action": action,
                        "target_kind": target_kind,
                        "target_id": target_id,
                        "target_team_id": target_team_id,
                        "scope": scope,
                        "ip": ip,
                        "user_agent": user_agent,
                    }
                    if any(existing[key] != value for key, value in expected.items()):
                        raise ValueError("admin audit identity conflict")
                    try:
                        existing_payload = json.loads(existing["payload_json"])
                    except (TypeError, json.JSONDecodeError) as exc:
                        raise ValueError("admin audit payload is malformed") from exc
                    if existing_payload != payload_dict:
                        raise ValueError("admin audit identity conflict")
                    return True
            await conn.execute(sql, params)
            return True

        # Best-effort mode
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(sql, params)
                await db.commit()
            return True
        except Exception as e:
            import logging as _logging

            _log = _logging.getLogger(__name__)
            _log.error("admin_audit_log write failed action=%s err=%s", action, str(e)[:200])
            # P2-S-9 fallback file
            try:
                from pathlib import Path as _Path

                fallback = _Path("/var/log/bowhead/audit-fallback.jsonl")
                fallback.parent.mkdir(parents=True, exist_ok=True)
                with fallback.open("a") as f:
                    f.write(
                        json.dumps(
                            {
                                "ts": _now(),
                                "action": action,
                                "actor_user_id": actor_user_id,
                                "team_id": team_id,
                                "payload": payload_dict,
                                "error": str(e)[:200],
                            },
                            ensure_ascii=False,
                        )
                        + "\n"
                    )
            except Exception:  # noqa: S110
                pass
            return False

    async def audit_critical_action(
        self,
        *,
        action: str,
        actor_user_id: str,
        team_id: str,
        scope: str,
        payload: dict[str, Any],
        main_op_sql: str,
        main_op_params: tuple,
        target_kind: str | None = None,
        target_id: str | None = None,
        target_team_id: str | None = None,
        ip: str | None = None,
        user_agent: str | None = None,
    ) -> int:
        """D-3=B critical action wrapper: main op + audit log in single BEGIN IMMEDIATE tx.

        Audit failure (e.g., trigger ABORT) rolls back the main op. Use ONLY for
        actions in CRITICAL_ACTIONS (raise ValueError otherwise).

        Returns rowcount of main_op_sql execute.
        """
        from bowhead_core.audit_actions import is_critical

        if not is_critical(action):
            raise ValueError(f"audit_critical_action requires CRITICAL_ACTIONS member; got {action!r}")

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                cur = await db.execute(main_op_sql, main_op_params)
                rowcount = cur.rowcount
                await self.write_admin_audit_log(
                    actor_user_id=actor_user_id,
                    team_id=team_id,
                    action=action,
                    target_kind=target_kind,
                    target_id=target_id,
                    target_team_id=target_team_id,
                    scope=scope,
                    payload=payload,
                    ip=ip,
                    user_agent=user_agent,
                    conn=db,
                )
                await db.commit()
                return rowcount
            except Exception:
                await db.rollback()
                raise

    # ─── Sprint β G5 — team_members + multi-admin helpers ──────────

    async def get_team_role(self, user_id: str, team_id: str) -> str | None:
        """Returns 'owner' / 'team_admin' / 'audit_admin' / 'member' / None.

        The team owner wins over team_members.role.
        """
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            return await _read_team_role_in_tx(
                db,
                user_id=user_id,
                team_id=team_id,
            )

    async def list_team_users_by_roles(
        self,
        team_id: str,
        roles: set[str],
        *,
        include_creator: bool = True,
    ) -> list[str]:
        """Return active user ids in an team with one of the requested team roles."""
        if not roles:
            return []
        role_values = {r for r in roles if r != "owner"}
        out: list[str] = []
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            if include_creator and "owner" in roles:
                async with db.execute(
                    "SELECT u.id FROM teams o JOIN users u ON u.id = o.created_by_user_id WHERE o.id = ? AND u.status = 'active'",
                    (team_id,),
                ) as cur:
                    row = await cur.fetchone()
                    if row:
                        out.append(row["id"])
            if role_values:
                placeholders = ",".join(["?"] * len(role_values))
                async with db.execute(
                    "SELECT DISTINCT u.id FROM team_members om "
                    "JOIN users u ON u.id = om.user_id "
                    f"WHERE om.team_id = ? AND om.role IN ({placeholders}) "
                    "AND om.status = 'active' AND u.status = 'active' "
                    "ORDER BY u.id",
                    (team_id, *sorted(role_values)),
                ) as cur:
                    rows = await cur.fetchall()
                for row in rows:
                    if row["id"] not in out:
                        out.append(row["id"])
        return out

    async def is_only_remaining_team_admin(
        self,
        user_id: str,
        team_id: str,
    ) -> bool:
        """True if user is the only ACTIVE 'team_admin' role in team (excluding owner).

        Used by self-demote block. P1-O-5 race fix: caller wraps in BEGIN IMMEDIATE
        (this method is non-atomic — caller decides isolation).
        """
        async with (
            aiosqlite.connect(self.db_path) as db,
            db.execute(
                "SELECT COUNT(*) "
                "FROM team_members om "
                "JOIN users u ON u.id = om.user_id AND u.status = 'active' "
                "WHERE om.team_id = ? AND om.role = 'team_admin' AND om.status = 'active' "
                "AND om.user_id != ?",
                (team_id, user_id),
            ) as cur,
        ):
            row = await cur.fetchone()
        return row[0] == 0

    async def promote_team_role(
        self,
        *,
        user_id: str,
        team_id: str,
        new_role: str,
        promoted_by: str,
    ) -> tuple[bool, str | None]:
        """G5 atomic promote/demote via BEGIN IMMEDIATE.

        Returns (changed: bool, old_role: str | None). Idempotent if new_role==old_role.
        """
        if new_role not in {"team_admin", "audit_admin", "member"}:
            raise ValueError("invalid team role")
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                db.row_factory = aiosqlite.Row
                async with db.execute(
                    "SELECT role FROM team_members WHERE user_id = ? AND team_id = ?",
                    (user_id, team_id),
                ) as cur:
                    row = await cur.fetchone()
                old_role = row["role"] if row else None

                if old_role == new_role:
                    await db.rollback()
                    return (False, old_role)

                await db.execute(
                    "INSERT INTO team_members(id, user_id, team_id, role, "
                    "promoted_by, promoted_at, status, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, 'active', ?) "
                    "ON CONFLICT(user_id, team_id) DO UPDATE SET "
                    "  role = excluded.role, "
                    "  promoted_by = excluded.promoted_by, "
                    "  promoted_at = excluded.promoted_at, "
                    "  status = 'active'",
                    (_id("om"), user_id, team_id, new_role, promoted_by, _now(), _now()),
                )
                await self._bump_team_membership_epoch_conn(db, team_id)
                await db.commit()
                return (True, old_role)
            except Exception:
                await db.rollback()
                raise

    async def _migrate_init_team_members_from_creators(self) -> int:
        """G5 grandfather migration. Backfills `teams.created_by_user_id`
        as 'team_admin' row in team_members. Idempotent via WHERE NOT EXISTS.

        P1-O-2 fix: dropped COUNT(*) > 0 short-circuit (re-runs need check).
        Uses Python utc_now_iso() for consistency.
        """
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                "INSERT INTO team_members(id, user_id, team_id, role, "
                "promoted_by, promoted_at, status, created_at) "
                "SELECT 'om_' || lower(hex(randomblob(6))), "
                "       o.created_by_user_id, o.id, 'team_admin', "
                "       o.created_by_user_id, ?, 'active', ? "
                "FROM teams o "
                "WHERE o.created_by_user_id IS NOT NULL "
                "  AND NOT EXISTS ("
                "    SELECT 1 FROM team_members om "
                "    WHERE om.user_id = o.created_by_user_id AND om.team_id = o.id"
                "  )",
                (_now(), _now()),
            )
            await db.commit()
            return cur.rowcount

    # ─── Sprint β G2 — workspaces + ws_members extension (D-1=B) ──

    async def sync_workspace_from_datarc(
        self,
        *,
        team_id: str,
        datarc_workspace_id: str,
        name: str,
        description: str | None = None,
    ) -> str:
        """D-1=B: upsert Bowhead workspaces from datarc. Returns Bowhead ws_id."""
        display_name, name_key = normalize_space_name(name)
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT id FROM workspaces WHERE datarc_workspace_id = ? AND team_id = ?",
                (datarc_workspace_id, team_id),
            ) as cur:
                row = await cur.fetchone()
            if row:
                await db.execute(
                    "UPDATE workspaces SET name = ?, name_key = ?, description = ?, "
                    "updated_at = ?, last_synced_at = ?, revision = revision + 1 "
                    "WHERE id = ?",
                    (display_name, name_key, description, _now(), _now(), row["id"]),
                )
                await db.commit()
                return row["id"]
            ws_id = _id("ws")
            await db.execute(
                "INSERT INTO workspaces(id, team_id, name, name_key, datarc_workspace_id, "
                "description, status, revision, created_at, updated_at, last_synced_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 'active', 1, ?, ?, ?)",
                (
                    ws_id,
                    team_id,
                    display_name,
                    name_key,
                    datarc_workspace_id,
                    description,
                    _now(),
                    _now(),
                    _now(),
                ),
            )
            await db.commit()
            return ws_id

    async def get_workspace(
        self,
        workspace_id: str,
    ) -> dict[str, Any] | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT id, team_id, name, name_key, description, status, revision, "
                "datarc_workspace_id, created_at, updated_at FROM workspaces WHERE id = ?",
                (workspace_id,),
            ) as cur:
                row = await cur.fetchone()
        return dict(row) if row else None

    async def list_workspaces_for_user(
        self,
        user_id: str,
        *,
        team_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """G2 /me/workspaces backend. Bowhead canonical (D-1=B).

        Returns list of {workspace_id, name, description, role, last_active_at, team_id,
        datarc_workspace_id}.
        """
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            query = (
                "SELECT w.id AS workspace_id, w.name, w.description, w.datarc_workspace_id, "
                "       wm.role, wm.last_active_at, w.team_id "
                "FROM workspace_members wm "
                "JOIN workspaces w ON w.id = wm.workspace_id "
                "WHERE wm.user_id = ? AND wm.status = 'active' AND w.status = 'active'"
            )
            params: tuple[str, ...] = (user_id,)
            if team_id is not None:
                query += " AND w.team_id = ?"
                params = (user_id, team_id)
            query += " ORDER BY wm.last_active_at DESC NULLS LAST"
            async with db.execute(query, params) as cur:
                rows = await cur.fetchall()
        return [{**dict(r), "role": _canonical_workspace_member_role(r["role"])} for r in rows]

    async def count_active_workspaces(self, team_id: str) -> int:
        """Count active Bowhead spaces for one Team without loading memberships."""
        async with (
            aiosqlite.connect(self.db_path) as db,
            db.execute(
                "SELECT COUNT(*) FROM workspaces WHERE team_id = ? AND status = 'active'",
                (team_id,),
            ) as cur,
        ):
            row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def list_active_workspace_ids(self, team_id: str) -> list[str]:
        """List active Space ids for internal team-scoped aggregate queries."""
        async with (
            aiosqlite.connect(self.db_path) as db,
            db.execute(
                "SELECT id FROM workspaces WHERE team_id = ? AND status = 'active' ORDER BY id",
                (team_id,),
            ) as cur,
        ):
            rows = await cur.fetchall()
        return [str(row[0]) for row in rows]

    @staticmethod
    async def _team_governance_role_in_tx(
        db: aiosqlite.Connection,
        *,
        user_id: str,
        team_id: str,
    ) -> str | None:
        async with db.execute(
            "SELECT t.created_by_user_id, tm.role "
            "FROM teams t "
            "LEFT JOIN team_members tm ON tm.team_id=t.id AND tm.user_id=? "
            "AND tm.status='active' "
            "WHERE t.id=? AND t.status='active'",
            (user_id, team_id),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        if row["created_by_user_id"] == user_id:
            return "owner"
        role = str(row["role"] or "")
        return role if role in {"team_admin", "audit_admin", "member"} else None

    @staticmethod
    async def _assert_workspace_name_unique_in_tx(
        db: aiosqlite.Connection,
        *,
        team_id: str,
        name_key: str,
        exclude_workspace_id: str | None = None,
    ) -> None:
        query = "SELECT id,name,name_key FROM workspaces WHERE team_id=?"
        params: list[Any] = [team_id]
        if exclude_workspace_id is not None:
            query += " AND id<>?"
            params.append(exclude_workspace_id)
        async with db.execute(query, params) as cur:
            rows = await cur.fetchall()
        for row in rows:
            existing_key = row["name_key"]
            if not existing_key:
                _, existing_key = normalize_space_name(str(row["name"]))
            if existing_key == name_key:
                raise SpaceGovernanceError("duplicate_workspace_name")

    @staticmethod
    def _space_governance_item(row: Any) -> dict[str, Any]:
        return {
            "workspace_id": row["id"],
            "name": row["name"],
            "description": row["description"],
            "status": row["status"],
            "member_count": int(row["member_count"]),
            "admin_count": int(row["admin_count"]),
            "caller_membership": normalize_space_role(row["caller_membership"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "revision": int(row["revision"] or 1),
        }

    async def _get_workspace_governance_record(
        self,
        *,
        team_id: str,
        caller_user_id: str,
        workspace_id: str,
        require_team_governance: bool,
    ) -> dict[str, Any]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            if require_team_governance:
                role = await self._team_governance_role_in_tx(
                    db,
                    user_id=caller_user_id,
                    team_id=team_id,
                )
                if role not in {"owner", "team_admin"}:
                    raise SpaceGovernanceError("not_team_admin")
            async with db.execute(
                "SELECT w.id,w.name,w.description,w.status,w.created_at,w.updated_at,"
                "w.revision,"
                "(SELECT COUNT(*) FROM workspace_members wm "
                " JOIN users u ON u.id=wm.user_id AND u.status='active' "
                " JOIN team_members tm ON tm.user_id=wm.user_id AND tm.team_id=w.team_id "
                "  AND tm.status='active' "
                " WHERE wm.workspace_id=w.id AND wm.status='active') AS member_count,"
                "(SELECT COUNT(*) FROM workspace_members wm "
                " JOIN users u ON u.id=wm.user_id AND u.status='active' "
                " JOIN team_members tm ON tm.user_id=wm.user_id AND tm.team_id=w.team_id "
                "  AND tm.status='active' "
                " WHERE wm.workspace_id=w.id AND wm.status='active' "
                " AND wm.role IN ('admin','ws_admin','owner','workspace_admin')) AS admin_count,"
                "(SELECT wm.role FROM workspace_members wm "
                " WHERE wm.workspace_id=w.id AND wm.user_id=? AND wm.status='active') "
                " AS caller_membership "
                "FROM workspaces w WHERE w.id=? AND w.team_id=?",
                (caller_user_id, workspace_id, team_id),
            ) as cur:
                row = await cur.fetchone()
        if row is None:
            raise SpaceGovernanceError("workspace_not_found")
        return self._space_governance_item(row)

    async def list_team_workspaces_governance(
        self,
        *,
        team_id: str,
        caller_user_id: str,
        status: str = "active",
        q: str | None = None,
        page_size: int = 50,
        after_name_key: str | None = None,
        after_workspace_id: str | None = None,
    ) -> tuple[list[dict[str, Any]], bool]:
        """List Team Space governance metadata without granting content access."""

        if status not in {"active", "archived", "all"}:
            raise SpaceGovernanceError("invalid_workspace_status")
        limit = max(1, min(100, page_size))
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            role = await self._team_governance_role_in_tx(
                db,
                user_id=caller_user_id,
                team_id=team_id,
            )
            if role not in {"owner", "team_admin"}:
                raise SpaceGovernanceError("not_team_admin")
            where = ["w.team_id=?"]
            params: list[Any] = [team_id]
            if status != "all":
                where.append("w.status=?")
                params.append(status)
            if q:
                query_key = normalize_space_name(q)[1]
                where.append("(COALESCE(w.name_key, lower(trim(w.name))) LIKE ? OR w.id LIKE ?)")
                params.extend([f"%{query_key}%", f"%{q.strip()}%"])
            if after_name_key is not None and after_workspace_id is not None:
                where.append("(COALESCE(w.name_key, lower(trim(w.name)))>? OR (COALESCE(w.name_key, lower(trim(w.name)))=? AND w.id>?))")
                params.extend(
                    [
                        after_name_key,
                        after_name_key,
                        after_workspace_id,
                    ]
                )
            query_params = [caller_user_id, *params, limit + 1]
            sql = (
                "SELECT w.id,w.name,w.description,w.status,w.created_at,w.updated_at,"
                "w.revision,COALESCE(w.name_key,lower(trim(w.name))) AS effective_name_key,"
                "(SELECT COUNT(*) FROM workspace_members wm "
                " JOIN users u ON u.id=wm.user_id AND u.status='active' "
                " JOIN team_members tm ON tm.user_id=wm.user_id AND tm.team_id=w.team_id "
                "  AND tm.status='active' "
                " WHERE wm.workspace_id=w.id AND wm.status='active') AS member_count,"
                "(SELECT COUNT(*) FROM workspace_members wm "
                " JOIN users u ON u.id=wm.user_id AND u.status='active' "
                " JOIN team_members tm ON tm.user_id=wm.user_id AND tm.team_id=w.team_id "
                "  AND tm.status='active' "
                " WHERE wm.workspace_id=w.id AND wm.status='active' "
                " AND wm.role IN ('admin','ws_admin','owner','workspace_admin')) AS admin_count,"
                "(SELECT wm.role FROM workspace_members wm "
                " WHERE wm.workspace_id=w.id AND wm.user_id=? AND wm.status='active') "
                " AS caller_membership "
                f"FROM workspaces w WHERE {' AND '.join(where)} "
                "ORDER BY effective_name_key ASC,w.id ASC LIMIT ?"
            )
            async with db.execute(sql, query_params) as cur:
                rows = await cur.fetchall()
        has_more = len(rows) > limit
        return [self._space_governance_item(row) for row in rows[:limit]], has_more

    async def get_team_workspace_governance(
        self,
        *,
        team_id: str,
        caller_user_id: str,
        workspace_id: str,
    ) -> dict[str, Any]:
        """Return one Space governance record for the caller's verified Team."""

        return await self._get_workspace_governance_record(
            team_id=team_id,
            caller_user_id=caller_user_id,
            workspace_id=workspace_id,
            require_team_governance=True,
        )

    async def create_governed_workspace(
        self,
        *,
        team_id: str,
        actor_user_id: str,
        name: str,
        description: str | None = None,
        set_as_current: bool = False,
    ) -> dict[str, Any]:
        """Atomically create a Space, its first admin, optional preference and audit."""

        display_name, name_key = normalize_space_name(name)
        if len(display_name) > 120:
            raise SpaceGovernanceError("workspace_name_too_long")
        workspace_id = _id("ws")
        now = _now()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                role = await self._team_governance_role_in_tx(
                    db,
                    user_id=actor_user_id,
                    team_id=team_id,
                )
                if role not in {"owner", "team_admin"}:
                    raise SpaceGovernanceError("not_team_admin")
                await self._assert_workspace_name_unique_in_tx(
                    db,
                    team_id=team_id,
                    name_key=name_key,
                )
                await db.execute(
                    "INSERT INTO workspaces(id,team_id,name,name_key,datarc_workspace_id,"
                    "description,status,revision,created_at,updated_at) "
                    "VALUES (?,?,?,?,NULL,?,'active',1,?,?)",
                    (
                        workspace_id,
                        team_id,
                        display_name,
                        name_key,
                        description,
                        now,
                        now,
                    ),
                )
                await db.execute(
                    "INSERT INTO workspace_members(workspace_id,user_id,role,invited_by,"
                    "joined_at,last_active_at,status) VALUES (?,?,'admin',?,?,?,'active')",
                    (
                        workspace_id,
                        actor_user_id,
                        actor_user_id,
                        now,
                        now,
                    ),
                )
                if set_as_current:
                    await db.execute(
                        "UPDATE users SET preferences_json=json_set(coalesce(preferences_json,'{}'),'$.last_workspace_id',?) WHERE id=?",
                        (workspace_id, actor_user_id),
                    )
                await self.write_admin_audit_log(
                    actor_user_id=actor_user_id,
                    team_id=team_id,
                    action="workspace_created",
                    target_kind="workspace",
                    target_id=workspace_id,
                    scope="team",
                    payload={
                        "set_as_current": set_as_current,
                        "source": "space_governance",
                    },
                    conn=db,
                )
                await db.commit()
            except Exception:
                await db.rollback()
                raise
        return await self._get_workspace_governance_record(
            team_id=team_id,
            caller_user_id=actor_user_id,
            workspace_id=workspace_id,
            require_team_governance=False,
        )

    async def update_governed_workspace(
        self,
        *,
        team_id: str,
        actor_user_id: str,
        workspace_id: str,
        expected_revision: int | None,
        name: str | None = None,
        description: str | None | object = _UNSET,
    ) -> dict[str, Any]:
        """Update Space metadata with OCC and lifecycle-aware governance."""

        display_name: str | None = None
        name_key: str | None = None
        if name is not None:
            display_name, name_key = normalize_space_name(name)
            if len(display_name) > 120:
                raise SpaceGovernanceError("workspace_name_too_long")
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute(
                    "SELECT id,team_id,status,revision,name,name_key,description FROM workspaces WHERE id=? AND team_id=?",
                    (workspace_id, team_id),
                ) as cur:
                    workspace = await cur.fetchone()
                if workspace is None:
                    raise SpaceGovernanceError("workspace_not_found")
                team_role = await self._team_governance_role_in_tx(
                    db,
                    user_id=actor_user_id,
                    team_id=team_id,
                )
                async with db.execute(
                    "SELECT role FROM workspace_members WHERE workspace_id=? AND user_id=? AND status='active'",
                    (workspace_id, actor_user_id),
                ) as cur:
                    member = await cur.fetchone()
                member_role = normalize_space_role(member["role"]) if member else None
                is_team_admin = team_role in {"owner", "team_admin"}
                if workspace["status"] == "archived":
                    if not is_team_admin:
                        raise SpaceGovernanceError("not_team_admin")
                elif not is_team_admin and member_role != "admin":
                    raise SpaceGovernanceError("not_workspace_admin")
                current_revision = int(workspace["revision"] or 1)
                if expected_revision is not None and expected_revision != current_revision:
                    raise SpaceGovernanceError(
                        "stale_workspace_update",
                        {"current_revision": current_revision},
                    )
                if name_key is not None:
                    await self._assert_workspace_name_unique_in_tx(
                        db,
                        team_id=team_id,
                        name_key=name_key,
                        exclude_workspace_id=workspace_id,
                    )
                target_description = workspace["description"] if description is _UNSET else description
                target_name = display_name or workspace["name"]
                target_name_key = name_key or workspace["name_key"]
                if not target_name_key:
                    _, target_name_key = normalize_space_name(str(target_name))
                await db.execute(
                    "UPDATE workspaces SET name=?,name_key=?,description=?,updated_at=?,revision=revision+1 WHERE id=? AND team_id=?",
                    (
                        target_name,
                        target_name_key,
                        target_description,
                        _now(),
                        workspace_id,
                        team_id,
                    ),
                )
                await self.write_admin_audit_log(
                    actor_user_id=actor_user_id,
                    team_id=team_id,
                    action="workspace_metadata_updated",
                    target_kind="workspace",
                    target_id=workspace_id,
                    scope="team",
                    payload={"previous_revision": current_revision},
                    conn=db,
                )
                await db.commit()
            except Exception:
                await db.rollback()
                raise
        return await self._get_workspace_governance_record(
            team_id=team_id,
            caller_user_id=actor_user_id,
            workspace_id=workspace_id,
            require_team_governance=False,
        )

    async def archive_governed_workspace(
        self,
        *,
        team_id: str,
        actor_user_id: str,
        workspace_id: str,
        expected_revision: int,
    ) -> dict[str, Any]:
        """Archive a Space while serializing the last-active-Space invariant."""

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                role = await self._team_governance_role_in_tx(
                    db,
                    user_id=actor_user_id,
                    team_id=team_id,
                )
                if role not in {"owner", "team_admin"}:
                    raise SpaceGovernanceError("not_team_admin")
                async with db.execute(
                    "SELECT status,revision FROM workspaces WHERE id=? AND team_id=?",
                    (workspace_id, team_id),
                ) as cur:
                    workspace = await cur.fetchone()
                if workspace is None:
                    raise SpaceGovernanceError("workspace_not_found")
                if workspace["status"] != "active":
                    raise SpaceGovernanceError("workspace_archived")
                current_revision = int(workspace["revision"] or 1)
                if expected_revision != current_revision:
                    raise SpaceGovernanceError(
                        "stale_workspace_update",
                        {"current_revision": current_revision},
                    )
                async with db.execute(
                    "SELECT COUNT(*) FROM workspaces WHERE team_id=? AND status='active' AND id<>?",
                    (team_id, workspace_id),
                ) as cur:
                    remaining = await cur.fetchone()
                if remaining is None or int(remaining[0]) < 1:
                    raise SpaceGovernanceError("last_active_space")
                await db.execute(
                    "UPDATE workspaces SET status='archived',updated_at=?,revision=revision+1 WHERE id=? AND team_id=?",
                    (_now(), workspace_id, team_id),
                )
                await db.execute(
                    "UPDATE users SET preferences_json=json_remove("
                    "coalesce(preferences_json,'{}'),'$.last_workspace_id') "
                    "WHERE id IN (SELECT user_id FROM workspace_members "
                    "WHERE workspace_id=?) "
                    "AND json_extract(coalesce(preferences_json,'{}'),"
                    "'$.last_workspace_id')=?",
                    (workspace_id, workspace_id),
                )
                await self._bump_team_membership_epoch_conn(db, team_id)
                await self.write_admin_audit_log(
                    actor_user_id=actor_user_id,
                    team_id=team_id,
                    action="workspace_archived",
                    target_kind="workspace",
                    target_id=workspace_id,
                    scope="team",
                    payload={"previous_revision": current_revision},
                    conn=db,
                )
                await db.commit()
            except Exception:
                await db.rollback()
                raise
        return await self._get_workspace_governance_record(
            team_id=team_id,
            caller_user_id=actor_user_id,
            workspace_id=workspace_id,
            require_team_governance=False,
        )

    async def restore_governed_workspace(
        self,
        *,
        team_id: str,
        actor_user_id: str,
        workspace_id: str,
        expected_revision: int,
        recovery_admin_user_id: str | None = None,
    ) -> dict[str, Any]:
        """Restore a Space and repair a missing administrator atomically."""

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                role = await self._team_governance_role_in_tx(
                    db,
                    user_id=actor_user_id,
                    team_id=team_id,
                )
                if role not in {"owner", "team_admin"}:
                    raise SpaceGovernanceError("not_team_admin")
                async with db.execute(
                    "SELECT status,revision,name,name_key FROM workspaces WHERE id=? AND team_id=?",
                    (workspace_id, team_id),
                ) as cur:
                    workspace = await cur.fetchone()
                if workspace is None:
                    raise SpaceGovernanceError("workspace_not_found")
                if workspace["status"] != "archived":
                    raise SpaceGovernanceError("workspace_not_archived")
                current_revision = int(workspace["revision"] or 1)
                if expected_revision != current_revision:
                    raise SpaceGovernanceError(
                        "stale_workspace_update",
                        {"current_revision": current_revision},
                    )
                _, name_key = normalize_space_name(str(workspace["name"]))
                await self._assert_workspace_name_unique_in_tx(
                    db,
                    team_id=team_id,
                    name_key=name_key,
                    exclude_workspace_id=workspace_id,
                )
                async with db.execute(
                    "SELECT COUNT(*) FROM workspace_members wm "
                    "JOIN users u ON u.id=wm.user_id AND u.status='active' "
                    "JOIN team_members tm ON tm.user_id=wm.user_id "
                    "AND tm.team_id=? AND tm.status='active' "
                    "WHERE wm.workspace_id=? AND wm.status='active' "
                    "AND wm.role IN ('admin','ws_admin','owner','workspace_admin')",
                    (team_id, workspace_id),
                ) as cur:
                    admin_row = await cur.fetchone()
                needs_admin = admin_row is None or int(admin_row[0]) == 0
                if needs_admin:
                    if not recovery_admin_user_id:
                        raise SpaceGovernanceError("restore_admin_required")
                    async with db.execute(
                        "SELECT 1 FROM users u JOIN team_members tm ON tm.user_id=u.id "
                        "WHERE u.id=? AND u.status='active' AND tm.team_id=? "
                        "AND tm.status='active'",
                        (recovery_admin_user_id, team_id),
                    ) as cur:
                        recovery_member = await cur.fetchone()
                    if recovery_member is None:
                        raise SpaceGovernanceError("recovery_admin_not_active")
                    now = _now()
                    await db.execute(
                        "INSERT INTO workspace_members(workspace_id,user_id,role,"
                        "invited_by,joined_at,status) VALUES (?,?,'admin',?,?,"
                        "'active') ON CONFLICT(workspace_id,user_id) DO UPDATE SET "
                        "role='admin',status='active'",
                        (
                            workspace_id,
                            recovery_admin_user_id,
                            actor_user_id,
                            now,
                        ),
                    )
                    await self.write_admin_audit_log(
                        actor_user_id=actor_user_id,
                        team_id=team_id,
                        action="workspace_member_added",
                        target_kind="user",
                        target_id=recovery_admin_user_id,
                        scope="workspace",
                        payload={
                            "workspace_id": workspace_id,
                            "role": "admin",
                            "source": "restore_recovery",
                        },
                        conn=db,
                    )
                await db.execute(
                    "UPDATE workspaces SET status='active',name_key=?,updated_at=?,revision=revision+1 WHERE id=? AND team_id=?",
                    (name_key, _now(), workspace_id, team_id),
                )
                await self._bump_team_membership_epoch_conn(db, team_id)
                await self.write_admin_audit_log(
                    actor_user_id=actor_user_id,
                    team_id=team_id,
                    action="workspace_restored",
                    target_kind="workspace",
                    target_id=workspace_id,
                    scope="team",
                    payload={
                        "previous_revision": current_revision,
                        "recovery_admin_added": needs_admin,
                    },
                    conn=db,
                )
                await db.commit()
            except Exception:
                await db.rollback()
                raise
        return await self._get_workspace_governance_record(
            team_id=team_id,
            caller_user_id=actor_user_id,
            workspace_id=workspace_id,
            require_team_governance=False,
        )

    async def restore_openapi_workspace(
        self,
        *,
        team_id: str,
        workspace_id: str,
        recovery_admin_user_id: str | None = None,
    ) -> bool:
        """Restore an archived Space for an authorized external synchronization key.

        The OpenAPI credential is the audited actor.  The restore itself still
        preserves the Space invariant that an active Space has an active admin.
        """

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute(
                    "SELECT status,name FROM workspaces WHERE id=? AND team_id=?",
                    (workspace_id, team_id),
                ) as cur:
                    workspace = await cur.fetchone()
                if workspace is None:
                    raise SpaceGovernanceError("workspace_not_found")
                if workspace["status"] != "archived":
                    raise SpaceGovernanceError("workspace_not_archived")
                _, name_key = normalize_space_name(str(workspace["name"]))
                await self._assert_workspace_name_unique_in_tx(
                    db,
                    team_id=team_id,
                    name_key=name_key,
                    exclude_workspace_id=workspace_id,
                )
                async with db.execute(
                    "SELECT COUNT(*) FROM workspace_members wm "
                    "JOIN users u ON u.id=wm.user_id AND u.status='active' "
                    "JOIN team_members tm ON tm.user_id=u.id "
                    "AND tm.team_id=? AND tm.status='active' "
                    "WHERE wm.workspace_id=? AND wm.status='active' "
                    "AND wm.role IN ('admin','ws_admin','owner','workspace_admin')",
                    (team_id, workspace_id),
                ) as cur:
                    admin_row = await cur.fetchone()
                needs_admin = admin_row is None or int(admin_row[0]) == 0
                if needs_admin:
                    if not recovery_admin_user_id:
                        raise SpaceGovernanceError("restore_admin_required")
                    async with db.execute(
                        "SELECT 1 FROM users u JOIN team_members tm ON tm.user_id=u.id "
                        "WHERE u.id=? AND u.status='active' AND tm.team_id=? "
                        "AND tm.status='active'",
                        (recovery_admin_user_id, team_id),
                    ) as cur:
                        recovery_member = await cur.fetchone()
                    if recovery_member is None:
                        raise SpaceGovernanceError("recovery_admin_not_active")
                    now = _now()
                    await db.execute(
                        "INSERT INTO workspace_members(workspace_id,user_id,role,"
                        "invited_by,joined_at,status) VALUES (?,?,'admin',NULL,?,'active') "
                        "ON CONFLICT(workspace_id,user_id) DO UPDATE SET "
                        "role='admin',status='active',joined_at=excluded.joined_at",
                        (workspace_id, recovery_admin_user_id, now),
                    )
                await db.execute(
                    "UPDATE workspaces SET status='active',name_key=?,updated_at=?,revision=revision+1 WHERE id=? AND team_id=?",
                    (name_key, _now(), workspace_id, team_id),
                )
                await self._bump_team_membership_epoch_conn(db, team_id)
                await db.commit()
            except Exception:
                await db.rollback()
                raise
        return needs_admin

    async def create_local_workspace(
        self,
        *,
        team_id: str,
        name: str,
        creator_user_id: str,
        description: str | None = None,
    ) -> str:
        """ws-datarc-decouple ② (R2.2 / #110): create a Bowhead-LOCAL workspace — NO datarc
        backend (``datarc_workspace_id`` NULL). BI is team-connection-gated at runtime (P3), so a
        local workspace simply has no BI until the team has a connection; KB/docs/chat work
        regardless. The creator is enrolled as ``admin``. Returns the new ``ws_*`` id."""
        display_name, name_key = normalize_space_name(name)
        ws_id = _id("ws")
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO workspaces(id, team_id, name, name_key, datarc_workspace_id, "
                "description, status, revision, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, NULL, ?, 'active', 1, ?, ?)",
                (ws_id, team_id, display_name, name_key, description, _now(), _now()),
            )
            await db.execute(
                "INSERT OR REPLACE INTO workspace_members"
                "(workspace_id, user_id, role, invited_by, joined_at, status) "
                "VALUES (?, ?, 'admin', ?, ?, 'active')",
                (ws_id, creator_user_id, creator_user_id, _now()),
            )
            await db.commit()
        return ws_id

    async def rename_workspace(
        self,
        *,
        workspace_id: str,
        team_id: str,
        name: str | None = None,
        description: str | None = None,
    ) -> bool:
        """#110: rename / re-describe a workspace — Bowhead-LOCAL, NEVER touches datarc.
        team-bound (wrong team → no rows). COALESCE keeps fields the caller leaves None."""
        display_name: str | None = None
        name_key: str | None = None
        if name is not None:
            display_name, name_key = normalize_space_name(name)
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                "UPDATE workspaces SET name = COALESCE(?, name), "
                "name_key = COALESCE(?, name_key), "
                "description = COALESCE(?, description), updated_at = ?, "
                "revision = revision + 1 WHERE id = ? AND team_id = ?",
                (
                    display_name,
                    name_key,
                    description,
                    _now(),
                    workspace_id,
                    team_id,
                ),
            )
            await db.commit()
            return cur.rowcount > 0

    async def set_workspace_status(
        self,
        *,
        workspace_id: str,
        team_id: str,
        status: str,
    ) -> bool:
        """Archive / restore a workspace (status ∈ active|archived) — Bowhead-LOCAL, team-bound.
        Archived workspaces drop out of ``list_workspaces_for_user`` (which filters active)."""
        if status not in ("active", "archived"):
            raise ValueError(f"invalid status: {status!r}")
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                now = _now()
                cur = await db.execute(
                    "UPDATE workspaces SET status = ?, updated_at = ?, revision = revision + 1 WHERE id = ? AND team_id = ?",
                    (status, now, workspace_id, team_id),
                )
                changed = cur.rowcount > 0
                if changed:
                    await self._bump_team_membership_epoch_conn(db, team_id, now=now)
                await db.commit()
                return changed
            except Exception:
                await db.rollback()
                raise

    async def backfill_legacy_datarc_workspaces(
        self,
        *,
        team_id: str | None = None,
        legacy_workspace_ids: list[str] | None = None,
        apply: bool = False,
    ) -> dict[str, Any]:
        """Backfill canonical Bowhead workspaces from legacy Datarc-scoped members.

        Older deployments stored Datarc project ids directly in
        `workspace_members.workspace_id` and had no matching `workspaces` rows.
        The new Bowhead/Datarc split requires a canonical Bowhead `ws_*` row with
        `workspaces.datarc_workspace_id` pointing at the historical project id.

        This helper is intentionally explicit and non-destructive. It copies active
        legacy memberships to canonical workspaces and leaves the legacy rows in place.
        Feishu message handling must not call this automatically.
        """
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            wm_cols = await _table_columns(db, "workspace_members")
            optional_selects = [col for col in ("last_active_at", "cached_name") if col in wm_cols]
            optional_sql = "".join(f", wm.{col}" for col in optional_selects)
            params: list[Any] = []
            team_filter = ""
            if team_id:
                team_filter = " AND u.team_id = ?"
                params.append(team_id)
            legacy_filter = ""
            legacy_ids = [str(item).strip() for item in (legacy_workspace_ids or []) if str(item).strip()]
            if legacy_ids:
                placeholders = ", ".join("?" for _ in legacy_ids)
                legacy_filter = f" AND wm.workspace_id IN ({placeholders})"
                params.extend(legacy_ids)
            async with db.execute(
                "SELECT wm.workspace_id AS legacy_workspace_id, wm.user_id, "
                "       wm.role, wm.invited_by, wm.invited_at, wm.joined_at, "
                "       wm.status, u.team_id "
                f"{optional_sql} "
                "FROM workspace_members wm "
                "JOIN users u ON u.id = wm.user_id "
                "LEFT JOIN workspaces w ON w.id = wm.workspace_id "
                "WHERE w.id IS NULL "
                "  AND wm.status = 'active' "
                "  AND wm.workspace_id NOT LIKE 'ws%' "
                f"{team_filter} "
                f"{legacy_filter} "
                "ORDER BY u.team_id, wm.workspace_id, wm.user_id",
                params,
            ) as cur:
                legacy_rows = [dict(row) for row in await cur.fetchall()]

            mappings: dict[tuple[str, str], dict[str, Any]] = {}
            for row in legacy_rows:
                key = (str(row["team_id"]), str(row["legacy_workspace_id"]))
                if key in mappings:
                    continue
                existing = await _find_workspace_by_datarc_id(
                    db,
                    team_id=key[0],
                    datarc_workspace_id=key[1],
                )
                if existing is not None:
                    mappings[key] = {
                        "team_id": key[0],
                        "legacy_workspace_id": key[1],
                        "workspace_id": existing,
                        "created": False,
                    }
                    continue
                workspace_id = _id("ws")
                mappings[key] = {
                    "team_id": key[0],
                    "legacy_workspace_id": key[1],
                    "workspace_id": workspace_id,
                    "created": True,
                }
                if apply:
                    now = _now()
                    await db.execute(
                        "INSERT INTO workspaces(id, team_id, name, datarc_workspace_id, "
                        "description, status, created_at, updated_at, last_synced_at) "
                        "VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?)",
                        (
                            workspace_id,
                            key[0],
                            f"Datarc project {key[1]}",
                            key[1],
                            (f"Backfilled from legacy workspace_members.workspace_id {key[1]}"),
                            now,
                            now,
                            now,
                        ),
                    )

            members_copied = 0
            if apply:
                for row in legacy_rows:
                    key = (str(row["team_id"]), str(row["legacy_workspace_id"]))
                    workspace_id = mappings[key]["workspace_id"]
                    async with db.execute(
                        "SELECT 1 FROM workspace_members WHERE workspace_id = ? AND user_id = ?",
                        (workspace_id, row["user_id"]),
                    ) as cur:
                        if await cur.fetchone():
                            continue
                    insert_cols = [
                        "workspace_id",
                        "user_id",
                        "role",
                        "invited_by",
                        "invited_at",
                        "joined_at",
                        "status",
                    ]
                    insert_vals: list[Any] = [
                        workspace_id,
                        row["user_id"],
                        row["role"],
                        row["invited_by"],
                        row["invited_at"],
                        row["joined_at"],
                        row["status"],
                    ]
                    if "last_active_at" in wm_cols:
                        insert_cols.append("last_active_at")
                        insert_vals.append(row.get("last_active_at") or row["joined_at"])
                    if "cached_name" in wm_cols:
                        insert_cols.append("cached_name")
                        insert_vals.append(row.get("cached_name") or f"Datarc project {row['legacy_workspace_id']}")
                    placeholders = ", ".join("?" for _ in insert_cols)
                    await db.execute(
                        f"INSERT INTO workspace_members({', '.join(insert_cols)}) VALUES ({placeholders})",
                        insert_vals,
                    )
                    members_copied += 1
                await db.commit()

        workspace_mappings = list(mappings.values())
        return {
            "dry_run": not apply,
            "legacy_workspace_ids_filter": legacy_ids,
            "legacy_memberships": len(legacy_rows),
            "workspace_mappings": workspace_mappings,
            "workspaces_created": sum(1 for item in workspace_mappings if item["created"]),
            "members_copied": members_copied,
        }

    async def touch_workspace_member_active(
        self,
        workspace_id: str,
        user_id: str,
    ) -> bool:
        """Update wm.last_active_at to now. Called from /me/last-workspace + ws-scoped ops."""
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                "UPDATE workspace_members SET last_active_at = ? WHERE workspace_id = ? AND user_id = ? AND status = 'active'",
                (_now(), workspace_id, user_id),
            )
            await db.commit()
            return cur.rowcount > 0

    async def set_user_preference_key(
        self,
        user_id: str,
        key: str,
        value: str,
    ) -> bool:
        """P1-S-3 atomic preferences_json update via SQLite json_set.

        Replaces read-modify-write of preferences_json (loses concurrent writes).
        `key` is JSON path leaf (e.g., 'last_workspace_id' → '$.last_workspace_id').
        `value` is wrapped as JSON string.
        """
        # Sanitize key (alphanumeric + underscore only — defense against path injection)
        if not key.replace("_", "").isalnum():
            raise ValueError(f"invalid preference key: {key!r}")
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                "UPDATE users SET preferences_json = json_set(  coalesce(preferences_json, '{}'), ?, ?) WHERE id = ?",
                (f"$.{key}", value, user_id),
            )
            await db.commit()
            return cur.rowcount > 0

    # ─── Sprint β G10 — password change support ─────────────────────

    async def try_acquire_pwd_change_lock(
        self,
        user_id: str,
        *,
        ttl_s: int = 30,
    ) -> bool:
        """P2-S-5: 30s atomic lock prevents 2-tab concurrent change-pwd attack.

        Returns True if acquired. Caller MUST call release_pwd_change_lock in finally.
        """
        import time as _time

        deadline = int(_time.time()) + ttl_s
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                db.row_factory = aiosqlite.Row
                async with db.execute(
                    "SELECT password_change_in_progress FROM users WHERE id = ?",
                    (user_id,),
                ) as cur:
                    row = await cur.fetchone()
                if not row:
                    await db.rollback()
                    return False
                existing = row["password_change_in_progress"]
                # If existing timestamp not expired → reject
                if existing:
                    try:
                        existing_ts = int(existing)
                    except (ValueError, TypeError):
                        existing_ts = 0
                    if existing_ts > int(_time.time()):
                        await db.rollback()
                        return False
                # Acquire lock
                await db.execute(
                    "UPDATE users SET password_change_in_progress = ? WHERE id = ?",
                    (str(deadline), user_id),
                )
                await db.commit()
                return True
            except Exception:
                await db.rollback()
                raise

    async def release_pwd_change_lock(self, user_id: str) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE users SET password_change_in_progress = NULL WHERE id = ?",
                (user_id,),
            )
            await db.commit()

    async def update_user_password(
        self,
        user_id: str,
        new_hash: str,
    ) -> bool:
        """G10 P0-S-3 atomic password update with pwd_version + session_token_version bump."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                now = _now()
                cur = await db.execute(
                    "UPDATE users SET password_hash = ?, "
                    "pwd_version = pwd_version + 1, "
                    "session_token_version = session_token_version + 1, "
                    "last_password_change_at = ? "
                    "WHERE id = ? AND status = 'active'",
                    (new_hash, now, user_id),
                )
                changed = cur.rowcount > 0
                if changed:
                    await self._bump_user_account_epoch_conn(db, user_id, now=now)
                await db.commit()
                return changed
            except Exception:
                await db.rollback()
                raise

    async def get_pwd_version(self, user_id: str) -> int:
        async with (
            aiosqlite.connect(self.db_path) as db,
            db.execute(
                "SELECT pwd_version FROM users WHERE id = ?",
                (user_id,),
            ) as cur,
        ):
            row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def get_session_token_version(self, user_id: str) -> int:
        async with (
            aiosqlite.connect(self.db_path) as db,
            db.execute(
                "SELECT session_token_version FROM users WHERE id = ?",
                (user_id,),
            ) as cur,
        ):
            row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def list_refresh_tokens(
        self,
        user_id: str,
        *,
        active_only: bool = True,
    ) -> list[dict[str, Any]]:
        """G10 active sessions list."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            sql = "SELECT id, user_id, expires_at, created_at, last_used_at, revoked_at, user_agent, ip FROM refresh_tokens WHERE user_id = ?"
            params: list = [user_id]
            if active_only:
                sql += " AND revoked_at IS NULL AND expires_at > ?"
                params.append(_now())
            sql += " ORDER BY last_used_at DESC NULLS LAST, created_at DESC"
            async with db.execute(sql, params) as cur:
                rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def revoke_refresh_token_by_id(
        self,
        session_id: str,
        user_id: str,
    ) -> bool:
        """G10 IDOR-safe revoke: only if session belongs to caller.

        Returns False (404 semantics) if session doesn't exist OR belongs to other user.
        """
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                "UPDATE refresh_tokens SET revoked_at = ? WHERE id = ? AND user_id = ? AND revoked_at IS NULL",
                (_now(), session_id, user_id),
            )
            await db.commit()
            return cur.rowcount > 0

    # ─── refresh_tokens (P4.5.27) ──────────────────────

    async def record_refresh_token(
        self,
        *,
        user_id: str,
        raw_token: str,
        expires_at: str,
        user_agent: str | None = None,
        ip: str | None = None,
    ) -> str:
        """存 refresh token hash. 返 row id (不是 raw token)."""
        tid = _id("rfh")
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO refresh_tokens(id,user_id,token_hash,expires_at,created_at,user_agent,ip) VALUES (?,?,?,?,?,?,?)",
                (tid, user_id, token_hash, expires_at, _now(), (user_agent or "")[:200], ip),
            )
            await db.commit()
        return tid

    async def verify_refresh_token(self, raw_token: str) -> str | None:
        """active + not expired → return user_id; 否则 None."""
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
        now = _now()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT user_id, expires_at, revoked_at FROM refresh_tokens WHERE token_hash = ?",
                (token_hash,),
            ) as cur:
                row = await cur.fetchone()
        if not row:
            return None
        if row["revoked_at"] is not None:
            return None
        if row["expires_at"] < now:
            return None
        return str(row["user_id"])

    async def rotate_refresh_token(
        self,
        *,
        old_raw: str,
        new_raw: str,
        user_id: str,
        expires_at: str,
        user_agent: str | None = None,
        ip: str | None = None,
    ) -> bool:
        """One-shot rotate (atomic in single tx). 返 True if old was active.

        防 replay: 同一 raw 用过即 revoked, 不能再换.
        """
        old_hash = hashlib.sha256(old_raw.encode()).hexdigest()
        new_hash = hashlib.sha256(new_raw.encode()).hexdigest()
        now = _now()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                cur = await db.execute(
                    "UPDATE refresh_tokens SET revoked_at = ?, last_used_at = ? "
                    "WHERE token_hash = ? AND revoked_at IS NULL "
                    "AND expires_at >= ? AND user_id = ?",
                    (now, now, old_hash, now, user_id),
                )
                if cur.rowcount == 0:
                    await db.rollback()
                    return False
                tid = _id("rfh")
                await db.execute(
                    "INSERT INTO refresh_tokens(id,user_id,token_hash,expires_at,created_at,user_agent,ip) VALUES (?,?,?,?,?,?,?)",
                    (tid, user_id, new_hash, expires_at, now, (user_agent or "")[:200], ip),
                )
                await db.commit()
                return True
            except Exception:
                await db.rollback()
                raise

    async def refresh_token_rotated_within(self, raw_token: str, *, seconds: float) -> bool:
        """Return whether this refresh token was consumed by rotation in the last ``seconds``.

        Only ``rotate_refresh_token`` writes ``last_used_at``, and it does so in the
        same statement that sets ``revoked_at``. A non-null ``last_used_at`` therefore
        identifies a rotated predecessor, as opposed to a token revoked by logout,
        session management or a compromise response.
        """
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
        async with (
            aiosqlite.connect(self.db_path) as db,
            db.execute(
                "SELECT last_used_at, revoked_at FROM refresh_tokens WHERE token_hash = ?",
                (token_hash,),
            ) as cur,
        ):
            row = await cur.fetchone()
        if not row or row[0] is None or row[1] is None or row[0] != row[1]:
            return False
        try:
            rotated_at = datetime.fromisoformat(str(row[0]))
        except ValueError:
            return False
        if rotated_at.tzinfo is None:
            rotated_at = rotated_at.replace(tzinfo=UTC)
        age = (datetime.now(UTC) - rotated_at).total_seconds()
        # Tolerate sub-second clock differences between gateway workers.
        return -1.0 <= age <= seconds

    async def revoke_refresh_token(self, raw_token: str) -> bool:
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                "UPDATE refresh_tokens SET revoked_at = ? WHERE token_hash = ? AND revoked_at IS NULL",
                (_now(), token_hash),
            )
            await db.commit()
            return cur.rowcount > 0

    async def revoke_refresh_token_and_get_user(self, raw_token: str) -> str | None:
        """Atomically revoke one active refresh credential and return its owner."""
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute(
                    "SELECT user_id FROM refresh_tokens WHERE token_hash=? AND revoked_at IS NULL",
                    (token_hash,),
                ) as cursor:
                    row = await cursor.fetchone()
                if row is None:
                    await db.rollback()
                    return None
                updated = await db.execute(
                    "UPDATE refresh_tokens SET revoked_at=? WHERE token_hash=? AND revoked_at IS NULL",
                    (_now(), token_hash),
                )
                if updated.rowcount != 1:
                    await db.rollback()
                    return None
                await db.commit()
                return str(row["user_id"])
            except BaseException:
                await db.rollback()
                raise

    async def revoke_all_refresh_for_user(self, user_id: str) -> int:
        """logout-everywhere / 改密码后用. 返 revoked 数."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                now = _now()
                cur = await db.execute(
                    "UPDATE refresh_tokens SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL",
                    (now, user_id),
                )
                # logout-everywhere is an account-wide authorization revoke,
                # even when no refresh row remains (an access-token SSE may
                # still be live).  Always advance the durable account fence.
                await self._bump_user_account_epoch_conn(db, user_id, now=now)
                await db.commit()
                return cur.rowcount
            except Exception:
                await db.rollback()
                raise

    # ─── invite_tokens (P4.5.18) ────────────────────────

    async def record_invite(
        self,
        *,
        workspace_id: str,
        team_id: str | None,
        role: str,
        invited_email: str,
        raw_token: str,
        expires_at: str,
        invited_by: str | None = None,
        invite_id: str | None = None,
        token_version: int = 1,
    ) -> str:
        """Persist invite token hash for revocation. 返 invite_id."""
        invite_id = invite_id or _id("inv")
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                now = _now()
                email_norm = invited_email.lower().strip()
                await db.execute(
                    "UPDATE invite_tokens SET status='expired' WHERE workspace_id=? AND invited_email=? AND status='active' AND expires_at < ?",
                    (workspace_id, email_norm, now),
                )
                async with db.execute(
                    "SELECT 1 FROM invite_tokens WHERE workspace_id=? AND invited_email=? AND status='active'",
                    (workspace_id, email_norm),
                ) as cur:
                    if await cur.fetchone() is not None:
                        raise ValueError("active workspace invite already exists")
                await db.execute(
                    "INSERT INTO invite_tokens(id,workspace_id,team_id,role,invited_email,"
                    "token_hash,status,invited_by,created_at,expires_at,token_version) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        invite_id,
                        workspace_id,
                        team_id,
                        role,
                        email_norm,
                        token_hash,
                        "active",
                        invited_by,
                        now,
                        expires_at,
                        token_version,
                    ),
                )
                await db.commit()
            except Exception:
                await db.rollback()
                raise
        return invite_id

    async def get_workspace_invite_by_id(
        self,
        invite_id: str,
    ) -> dict[str, Any] | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT id,workspace_id,team_id,role,invited_email,status,invited_by,"
                "created_at,expires_at,consumed_at,revoked_at,token_version,"
                "consumed_by_user_id,satisfied_at FROM invite_tokens "
                "WHERE id = ?",
                (invite_id,),
            ) as cur:
                row = await cur.fetchone()
        return dict(row) if row else None

    async def get_workspace_invite_by_token_hash(
        self,
        raw_token: str,
    ) -> dict[str, Any] | None:
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT id,workspace_id,team_id,role,invited_email,status,invited_by,"
                "created_at,expires_at,consumed_at,revoked_at,token_version,"
                "consumed_by_user_id,satisfied_at FROM invite_tokens "
                "WHERE token_hash = ?",
                (token_hash,),
            ) as cur:
                row = await cur.fetchone()
        return dict(row) if row else None

    async def is_invite_active(self, raw_token: str) -> bool:
        """Verify token still active (not revoked / consumed / expired)."""
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
        now = _now()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT status, expires_at FROM invite_tokens WHERE token_hash = ?",
                (token_hash,),
            ) as cur:
                row = await cur.fetchone()
        if not row:
            return False
        if row["status"] != "active":
            return False
        return not row["expires_at"] < now

    async def consume_invite(self, raw_token: str) -> bool:
        """Mark invite as consumed (one-shot). 返 True 仅当原本 active."""
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
        now = _now()
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                "UPDATE invite_tokens SET status = 'consumed', consumed_at = ? WHERE token_hash = ? AND status = 'active' AND expires_at >= ?",
                (now, token_hash, now),
            )
            await db.commit()
            return cur.rowcount > 0

    async def get_active_workspace_invite_by_email(
        self,
        workspace_id: str,
        invited_email: str,
    ) -> dict[str, Any] | None:
        """Fix A: match the ACTIVE workspace invite by (workspace_id, email) rather than by
        exact token_hash. Resend/reminder rotates the token (mints a new one, can't recover
        the old raw token from a hash) — hash-match would silently invalidate a link already
        in the invitee's inbox. The JWT is signature+exp verified upstream (_decode_jwt_multi);
        this row is the revoke/consume/expiry gate. The partial unique index guarantees one
        active row per (workspace,email)."""
        now = _now()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT id,workspace_id,team_id,role,invited_email,status,invited_by,"
                "created_at,expires_at,consumed_at,revoked_at,token_version,"
                "consumed_by_user_id,satisfied_at FROM invite_tokens "
                "WHERE workspace_id = ? AND invited_email = ? "
                "AND status = 'active' AND expires_at >= ? "
                "ORDER BY created_at DESC LIMIT 1",
                (workspace_id, invited_email.lower().strip(), now),
            ) as cur:
                row = await cur.fetchone()
        return dict(row) if row else None

    async def list_active_invites_for_email(self, email: str) -> list[dict[str, Any]]:
        """批2/c12 (PRD §2.3): per-user 邀请查询——零空间引导页「加入空间」数据源。
        现状 list_invites 只有 per-ws admin 视图（review P1：invitee 视角端点不存在）。
        带 workspace 名（引导页展示）；只返 active+未过期。"""
        now = _now()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT it.id, it.workspace_id, it.team_id, it.role, it.invited_by, "
                "it.created_at, it.expires_at, w.name AS workspace_name "
                "FROM invite_tokens it LEFT JOIN workspaces w ON w.id = it.workspace_id "
                "WHERE it.invited_email = ? AND it.status = 'active' AND it.expires_at >= ? "
                "ORDER BY it.created_at DESC",
                (email.lower().strip(), now),
            ) as cur:
                rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def consume_invite_by_id(self, invite_id: str) -> bool:
        """Consume a workspace invite by its id (Fix A — validation now matches by email,
        not by the rotated token hash, so consume must key on the resolved row's id)."""
        now = _now()
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                "UPDATE invite_tokens SET status = 'consumed', consumed_at = ? WHERE id = ? AND status = 'active'",
                (now, invite_id),
            )
            await db.commit()
            return cur.rowcount > 0

    async def revoke_invite(self, invite_id: str) -> bool:
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                "UPDATE invite_tokens SET status = 'revoked', revoked_at = ? WHERE id = ? AND status = 'active'",
                (_now(), invite_id),
            )
            await db.commit()
            return cur.rowcount > 0

    async def reissue_invite_token(
        self,
        invite_id: str,
        team_id: str | None,
        new_raw_token: str,
        expires_at: str,
        *,
        expected_token_version: int | None = None,
    ) -> bool:
        """email-config sprint(PRD P0-1): 重发邀请 = 重铸 token。

        替换 token_hash + 刷新 expires_at(旧 token 因 hash 替换自然失效,is_invite_active
        按 hash 查表)。WHERE status='active'(expired 是惰性状态、无人写 status='expired',
        故 active 已含过期行 → 重发刷新有效期, 语义正确);consumed/revoked 拒绝。
        team_id 入 WHERE 防跨 team reissue;legacy NULL-team 行(record_invite 接受 None)放行。
        返 True 仅当命中一行 active。
        """
        new_hash = hashlib.sha256(new_raw_token.encode()).hexdigest()
        async with aiosqlite.connect(self.db_path) as db:
            version_clause = ""
            args: tuple[Any, ...]
            if expected_token_version is not None:
                version_clause = " AND token_version = ?"
            if team_id is None:
                args = (new_hash, expires_at, invite_id)
                if expected_token_version is not None:
                    args += (expected_token_version,)
                cur = await db.execute(
                    "UPDATE invite_tokens SET token_hash = ?, expires_at = ?, "
                    "token_version = token_version + 1 WHERE id = ? AND status = 'active'" + version_clause,
                    args,
                )
            else:
                args = (new_hash, expires_at, invite_id, team_id)
                if expected_token_version is not None:
                    args += (expected_token_version,)
                cur = await db.execute(
                    "UPDATE invite_tokens SET token_hash = ?, expires_at = ?, "
                    "token_version = token_version + 1 WHERE id = ? AND status = 'active' "
                    "AND (team_id = ? OR team_id IS NULL)" + version_clause,
                    args,
                )
            await db.commit()
            return cur.rowcount > 0

    async def list_invites_for_workspace(
        self,
        workspace_id: str,
        *,
        include_inactive: bool = False,
    ) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            sql = (
                "SELECT id,workspace_id,team_id,role,invited_email,status,invited_by,"
                "created_at,expires_at,consumed_at,revoked_at,token_version,"
                "consumed_by_user_id,satisfied_at FROM invite_tokens "
                "WHERE workspace_id = ?"
            )
            args: tuple[Any, ...] = (workspace_id,)
            if not include_inactive:
                sql += " AND status = 'active'"
            sql += " ORDER BY created_at DESC"
            async with db.execute(sql, args) as cur:
                rows = await cur.fetchall()
        return [dict(r) for r in rows]

    # ─── team_invite_tokens — team-only People invitations ──────────

    async def create_or_merge_team_invite(
        self,
        *,
        team_id: str,
        invited_email: str,
        raw_token: str,
        expires_at: str,
        assignment_mode: str,
        initial_workspace_grants: list[dict[str, str]],
        invited_by: str,
        invite_id: str | None = None,
        team_role: str = "member",
        token_version: int = 1,
        public_entry_id: str | None = None,
    ) -> dict[str, Any]:
        """Create or merge one Team invitation and its initial Space grants."""

        if team_role not in {"team_admin", "audit_admin", "member"}:
            raise ValueError("invalid team role")

        if assignment_mode not in {"spaces", "team_only"}:
            raise SpaceGovernanceError("invalid_invite_assignment_mode")
        if assignment_mode == "spaces" and not (1 <= len(initial_workspace_grants) <= 20):
            raise SpaceGovernanceError("invalid_initial_workspace_grants")
        if assignment_mode == "team_only" and initial_workspace_grants:
            raise SpaceGovernanceError("invalid_initial_workspace_grants")
        normalized_grants: dict[str, str] = {}
        for grant in initial_workspace_grants:
            workspace_id = str(grant.get("workspace_id") or "").strip()
            role = normalize_space_role(grant.get("role"))
            if not workspace_id or role not in {"admin", "member", "viewer"}:
                raise SpaceGovernanceError("invalid_initial_workspace_grants")
            normalized_grants[workspace_id] = role
        if len(normalized_grants) != len(initial_workspace_grants):
            raise SpaceGovernanceError("duplicate_initial_workspace_grant")

        invite_id = invite_id or _id("oinv")
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
        email_norm = invited_email.lower().strip()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                now = _now()
                await db.execute(
                    "UPDATE team_invite_tokens SET status='expired' WHERE team_id=? AND invited_email=? AND status='active' AND expires_at<?",
                    (team_id, email_norm, now),
                )
                if self._seat_guard is not None:
                    active = await self.count_active_seats(team_id, conn=db)
                    await self._seat_guard.assert_can_add_seat(
                        team_id,
                        active_count=active,
                    )
                if normalized_grants:
                    placeholders = ",".join("?" for _ in normalized_grants)
                    async with db.execute(
                        f"SELECT id,team_id,status FROM workspaces WHERE id IN ({placeholders})",
                        list(normalized_grants),
                    ) as cur:
                        workspace_rows = {row["id"]: row for row in await cur.fetchall()}
                    for workspace_id in normalized_grants:
                        workspace = workspace_rows.get(workspace_id)
                        if workspace is None or workspace["team_id"] != team_id or workspace["status"] != "active":
                            raise SpaceGovernanceError(
                                "invalid_initial_workspace_grant",
                                {"workspace_id": workspace_id},
                            )
                async with db.execute(
                    "SELECT id,assignment_mode,assignment_revision FROM team_invite_tokens WHERE team_id=? AND invited_email=? AND status='active'",
                    (team_id, email_norm),
                ) as cur:
                    existing = await cur.fetchone()
                method = "created"
                if existing is None:
                    await db.execute(
                        "INSERT INTO team_invite_tokens("
                        "id,team_id,team_role,invited_email,token_hash,status,"
                        "invited_by,created_at,expires_at,token_version,"
                        "assignment_mode,assignment_revision,public_entry_id"
                        ") VALUES (?,?,?,?,?,'active',?,?,?,?,?,1,?)",
                        (
                            invite_id,
                            team_id,
                            team_role,
                            email_norm,
                            token_hash,
                            invited_by,
                            now,
                            expires_at,
                            token_version,
                            assignment_mode,
                            public_entry_id,
                        ),
                    )
                    for workspace_id, role in normalized_grants.items():
                        await db.execute(
                            "INSERT INTO team_invite_workspace_grants(invite_id,workspace_id,role,created_at) VALUES (?,?,?,?)",
                            (invite_id, workspace_id, role, now),
                        )
                else:
                    invite_id = str(existing["id"])
                    async with db.execute(
                        "SELECT workspace_id,role FROM team_invite_workspace_grants WHERE invite_id=?",
                        (invite_id,),
                    ) as cur:
                        current_grants = {str(row["workspace_id"]): str(row["role"]) for row in await cur.fetchall()}
                    if assignment_mode == "team_only" and current_grants:
                        raise SpaceGovernanceError("invite_has_workspace_grants")
                    merged = dict(current_grants)
                    merged.update(normalized_grants)
                    if len(merged) > 20:
                        raise SpaceGovernanceError("too_many_initial_workspace_grants")
                    changed = False
                    for workspace_id, role in merged.items():
                        if current_grants.get(workspace_id) == role:
                            continue
                        await db.execute(
                            "INSERT INTO team_invite_workspace_grants("
                            "invite_id,workspace_id,role,created_at) "
                            "VALUES (?,?,?,?) ON CONFLICT(invite_id,workspace_id) "
                            "DO UPDATE SET role=excluded.role",
                            (invite_id, workspace_id, role, now),
                        )
                        changed = True
                    target_mode = "spaces" if merged else assignment_mode
                    if target_mode != existing["assignment_mode"]:
                        changed = True
                    if changed:
                        await db.execute(
                            "UPDATE team_invite_tokens SET assignment_mode=?,assignment_revision=assignment_revision+1 WHERE id=?",
                            (target_mode, invite_id),
                        )
                        method = "grant_merged"
                    else:
                        method = "already_present"
                await self.write_admin_audit_log(
                    actor_user_id=invited_by,
                    team_id=team_id,
                    action="team_invite_assignment_created",
                    target_kind="invite",
                    target_id=invite_id,
                    scope="team",
                    payload={
                        "assignment_mode": assignment_mode,
                        "grant_count": len(normalized_grants),
                        "method": method,
                    },
                    conn=db,
                )
                await db.commit()
            except Exception:
                await db.rollback()
                raise
        invite = await self.get_team_invite_by_id(invite_id)
        if invite is None:
            raise RuntimeError("created Team invitation could not be read")
        return {**invite, "method": method}

    async def replace_team_invite_assignment(
        self,
        *,
        invite_id: str,
        team_id: str,
        actor_user_id: str,
        assignment_mode: str,
        initial_workspace_grants: list[dict[str, str]],
        expected_assignment_revision: int,
    ) -> dict[str, Any]:
        """Replace a pending invitation assignment set using optimistic concurrency."""

        if assignment_mode not in {"spaces", "team_only"}:
            raise SpaceGovernanceError("invalid_invite_assignment_mode")
        normalized: dict[str, str] = {}
        for grant in initial_workspace_grants:
            workspace_id = str(grant.get("workspace_id") or "").strip()
            role = normalize_space_role(grant.get("role"))
            if not workspace_id or role not in {"admin", "member", "viewer"} or workspace_id in normalized:
                raise SpaceGovernanceError("invalid_initial_workspace_grants")
            normalized[workspace_id] = role
        if (assignment_mode == "spaces" and not 1 <= len(normalized) <= 20) or (assignment_mode == "team_only" and normalized):
            raise SpaceGovernanceError("invalid_initial_workspace_grants")
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute(
                    "SELECT status,assignment_revision FROM team_invite_tokens WHERE id=? AND team_id=?",
                    (invite_id, team_id),
                ) as cur:
                    invite = await cur.fetchone()
                if invite is None or invite["status"] != "active":
                    raise SpaceGovernanceError("invite_not_found")
                current_revision = int(invite["assignment_revision"] or 1)
                if current_revision != expected_assignment_revision:
                    raise SpaceGovernanceError(
                        "stale_invite_assignment",
                        {"current_assignment_revision": current_revision},
                    )
                if normalized:
                    placeholders = ",".join("?" for _ in normalized)
                    async with db.execute(
                        f"SELECT id FROM workspaces WHERE team_id=? AND status='active' AND id IN ({placeholders})",
                        (team_id, *normalized),
                    ) as cur:
                        valid_ids = {str(row["id"]) for row in await cur.fetchall()}
                    if valid_ids != set(normalized):
                        raise SpaceGovernanceError("invalid_initial_workspace_grant")
                await db.execute(
                    "DELETE FROM team_invite_workspace_grants WHERE invite_id=?",
                    (invite_id,),
                )
                now = _now()
                for workspace_id, role in normalized.items():
                    await db.execute(
                        "INSERT INTO team_invite_workspace_grants(invite_id,workspace_id,role,created_at) VALUES (?,?,?,?)",
                        (invite_id, workspace_id, role, now),
                    )
                await db.execute(
                    "UPDATE team_invite_tokens SET assignment_mode=?,assignment_revision=assignment_revision+1 WHERE id=?",
                    (assignment_mode, invite_id),
                )
                await self.write_admin_audit_log(
                    actor_user_id=actor_user_id,
                    team_id=team_id,
                    action="team_invite_assignment_updated",
                    target_kind="invite",
                    target_id=invite_id,
                    scope="team",
                    payload={
                        "assignment_mode": assignment_mode,
                        "grant_count": len(normalized),
                        "previous_revision": current_revision,
                    },
                    conn=db,
                )
                await db.commit()
            except Exception:
                await db.rollback()
                raise
        invite = await self.get_team_invite_by_id(invite_id)
        if invite is None:
            raise RuntimeError("updated Team invitation could not be read")
        return invite

    async def record_team_invite(
        self,
        *,
        team_id: str,
        team_role: str,
        invited_email: str,
        raw_token: str,
        expires_at: str,
        invited_by: str | None = None,
        invite_id: str | None = None,
        token_version: int = 1,
    ) -> str:
        if team_role not in {"team_admin", "audit_admin", "member"}:
            raise ValueError("invalid team role")
        invite_id = invite_id or _id("oinv")
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                now = _now()
                email_norm = invited_email.lower().strip()
                await db.execute(
                    "UPDATE team_invite_tokens SET status='expired' WHERE team_id=? AND invited_email=? AND status='active' AND expires_at < ?",
                    (team_id, email_norm, now),
                )
                async with db.execute(
                    "SELECT 1 FROM team_invite_tokens WHERE team_id=? AND invited_email=? AND status='active'",
                    (team_id, email_norm),
                ) as cur:
                    if await cur.fetchone() is not None:
                        raise ValueError("active team invite already exists")
                await db.execute(
                    "INSERT INTO team_invite_tokens(id,team_id,team_role,invited_email,"
                    "token_hash,status,invited_by,created_at,expires_at,token_version) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        invite_id,
                        team_id,
                        team_role,
                        email_norm,
                        token_hash,
                        "active",
                        invited_by,
                        now,
                        expires_at,
                        token_version,
                    ),
                )
                await db.commit()
            except Exception:
                await db.rollback()
                raise
        return invite_id

    async def get_team_invite_by_id(
        self,
        invite_id: str,
    ) -> dict[str, Any] | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT id,team_id,team_role,invited_email,status,invited_by,"
                "created_at,expires_at,consumed_at,revoked_at,token_version,"
                "consumed_by_user_id,satisfied_at,assignment_mode,"
                "assignment_revision,public_entry_id "
                "FROM team_invite_tokens WHERE id = ?",
                (invite_id,),
            ) as cur:
                row = await cur.fetchone()
        if row is None:
            return None
        invite = dict(row)
        invite["initial_workspace_grants"] = await self.list_team_invite_workspace_grants(invite_id)
        return invite

    async def get_team_invite_by_token_hash(
        self,
        raw_token: str,
    ) -> dict[str, Any] | None:
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT id,team_id,team_role,invited_email,status,invited_by,"
                "created_at,expires_at,consumed_at,revoked_at,token_version,"
                "consumed_by_user_id,satisfied_at,assignment_mode,"
                "assignment_revision,public_entry_id "
                "FROM team_invite_tokens WHERE token_hash = ?",
                (token_hash,),
            ) as cur:
                row = await cur.fetchone()
        return dict(row) if row else None

    async def get_active_team_invite_by_token(
        self,
        raw_token: str,
    ) -> dict[str, Any] | None:
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
        now = _now()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT id,team_id,team_role,invited_email,status,invited_by,"
                "created_at,expires_at,consumed_at,revoked_at,token_version,"
                "consumed_by_user_id,satisfied_at,assignment_mode,"
                "assignment_revision,public_entry_id "
                "FROM team_invite_tokens WHERE token_hash = ?",
                (token_hash,),
            ) as cur:
                row = await cur.fetchone()
        if not row or row["status"] != "active" or row["expires_at"] < now:
            return None
        return dict(row)

    async def get_active_team_invite_by_email(
        self,
        team_id: str,
        invited_email: str,
    ) -> dict[str, Any] | None:
        """Fix A: match the ACTIVE team invite by (team_id, email) rather than by exact
        token_hash — so resend/reminder (which rotates the token) doesn't invalidate a link
        already in the invitee's inbox. The JWT is signature+exp verified upstream
        (_decode_jwt_multi); this row is the revoke/consume/expiry gate. At most one active
        invite per (team,email) — invite_team_member short-circuits 'already_invited'."""
        now = _now()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT id,team_id,team_role,invited_email,status,invited_by,"
                "created_at,expires_at,consumed_at,revoked_at,token_version,"
                "consumed_by_user_id,satisfied_at,assignment_mode,"
                "assignment_revision,public_entry_id FROM team_invite_tokens "
                "WHERE team_id = ? AND invited_email = ? "
                "AND status = 'active' AND expires_at >= ? "
                "ORDER BY created_at DESC LIMIT 1",
                (team_id, invited_email.lower().strip(), now),
            ) as cur:
                row = await cur.fetchone()
        return dict(row) if row else None

    async def consume_team_invite_by_id(self, invite_id: str) -> bool:
        """Consume an team invite by its id (Fix A — validation matches by email, not by the
        rotated token hash, so consume keys on the resolved row's id)."""
        now = _now()
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                "UPDATE team_invite_tokens SET status = 'consumed', consumed_at = ? WHERE id = ? AND status = 'active'",
                (now, invite_id),
            )
            await db.commit()
            return cur.rowcount > 0

    async def list_team_invites(
        self,
        team_id: str,
        *,
        include_inactive: bool = False,
        page: int | None = None,
        size: int = 25,
        q: str = "",
    ) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            sql = (
                "SELECT id,team_id,team_role,invited_email,status,invited_by,"
                "created_at,expires_at,consumed_at,revoked_at,token_version,"
                "consumed_by_user_id,satisfied_at,assignment_mode,"
                "assignment_revision,public_entry_id "
                "FROM team_invite_tokens WHERE team_id = ?"
            )
            args: list[Any] = [team_id]
            if not include_inactive:
                sql += " AND status = 'active' AND expires_at >= ?"
                args.append(_now())
            if q:
                escaped = q.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                sql += " AND LOWER(invited_email) LIKE ? ESCAPE '\\'"
                args.append(f"%{escaped}%")
            sql += " ORDER BY created_at DESC, id DESC"
            if page is not None:
                sql += " LIMIT ? OFFSET ?"
                args.extend([size, (page - 1) * size])
            async with db.execute(sql, args) as cur:
                rows = await cur.fetchall()
        invites = [dict(r) for r in rows]
        if not invites:
            return []
        invite_ids = [str(invite["id"]) for invite in invites]
        placeholders = ",".join("?" for _ in invite_ids)
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT invite_id,workspace_id,role,created_at "
                "FROM team_invite_workspace_grants "
                f"WHERE invite_id IN ({placeholders}) "
                "ORDER BY invite_id,workspace_id",
                invite_ids,
            ) as cur:
                grant_rows = await cur.fetchall()
        grants_by_invite: dict[str, list[dict[str, Any]]] = {invite_id: [] for invite_id in invite_ids}
        for row in grant_rows:
            grants_by_invite[str(row["invite_id"])].append(
                {
                    "workspace_id": row["workspace_id"],
                    "role": row["role"],
                    "created_at": row["created_at"],
                }
            )
        for invite in invites:
            invite["initial_workspace_grants"] = grants_by_invite[str(invite["id"])]
        return invites

    async def count_team_invites(
        self,
        team_id: str,
        *,
        include_inactive: bool = False,
        q: str = "",
    ) -> int:
        sql = "SELECT COUNT(*) FROM team_invite_tokens WHERE team_id = ?"
        args: list[Any] = [team_id]
        if not include_inactive:
            sql += " AND status = 'active' AND expires_at >= ?"
            args.append(_now())
        if q:
            escaped = q.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            sql += " AND LOWER(invited_email) LIKE ? ESCAPE '\\'"
            args.append(f"%{escaped}%")
        async with aiosqlite.connect(self.db_path) as db, db.execute(sql, args) as cur:
            return int((await cur.fetchone())[0])

    async def list_team_invite_workspace_grants(
        self,
        invite_id: str,
    ) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT workspace_id,role,created_at FROM team_invite_workspace_grants WHERE invite_id=? ORDER BY workspace_id",
                (invite_id,),
            ) as cur:
                rows = await cur.fetchall()
        return [dict(row) for row in rows]

    async def revoke_team_invite(self, invite_id: str, team_id: str) -> bool:
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                "UPDATE team_invite_tokens SET status = 'revoked', revoked_at = ? WHERE id = ? AND team_id = ? AND status = 'active'",
                (_now(), invite_id, team_id),
            )
            await db.commit()
            return cur.rowcount > 0

    async def reissue_team_invite_token(
        self,
        invite_id: str,
        team_id: str,
        new_raw_token: str,
        expires_at: str,
        *,
        expected_token_version: int | None = None,
        public_entry_id: str | None = None,
    ) -> bool:
        new_hash = hashlib.sha256(new_raw_token.encode()).hexdigest()
        async with aiosqlite.connect(self.db_path) as db:
            version_clause = ""
            args: tuple[Any, ...] = (
                new_hash,
                expires_at,
                public_entry_id,
                invite_id,
                team_id,
            )
            if expected_token_version is not None:
                version_clause = " AND token_version = ?"
                args += (expected_token_version,)
            cur = await db.execute(
                "UPDATE team_invite_tokens SET token_hash = ?, expires_at = ?, "
                "public_entry_id = COALESCE(public_entry_id, ?), "
                "token_version = token_version + 1 WHERE id = ? AND team_id = ? "
                "AND status = 'active'" + version_clause,
                args,
            )
            await db.commit()
            return cur.rowcount > 0

    async def list_team_workspace_grants_for_user(
        self,
        *,
        team_id: str,
        user_id: str,
    ) -> list[dict[str, Any]]:
        """List all Team Spaces with the target user's active membership role, if any."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT w.id AS workspace_id, w.name, w.description, w.status, "
                "       wm.role, wm.joined_at "
                "FROM workspaces w "
                "LEFT JOIN workspace_members wm ON wm.workspace_id = w.id "
                "  AND wm.user_id = ? AND wm.status = 'active' "
                "WHERE w.team_id = ? "
                "ORDER BY w.name COLLATE NOCASE, w.created_at",
                (user_id, team_id),
            ) as cur:
                rows = await cur.fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            item = dict(r)
            if item.get("role"):
                item["role"] = _canonical_workspace_member_role(item["role"])
            out.append(item)
        return out

    # ─── api_tokens (P4.5.5) ────────────────────────────

    async def issue_api_token(
        self,
        *,
        user_id: str,
        name: str,
        role_snapshot: str,
        workspace_id: str | None = None,
        expires_at: str | None = None,
    ) -> tuple[str, str]:
        """Issue raw token (only returned ONCE) + persist hash.
        返 (raw_token, token_id). role_snapshot 来自 user role at issue time (12.9 fix).
        """
        raw = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(raw.encode()).hexdigest()
        tid = _id("tok")
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO api_tokens(id,user_id,name,hash,workspace_id,role_snapshot,expires_at,created_at) VALUES (?,?,?,?,?,?,?,?)",
                (tid, user_id, name, token_hash, workspace_id, role_snapshot, expires_at, _now()),
            )
            await db.commit()
        return raw, tid

    async def list_api_tokens(self, user_id: str) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT id,name,workspace_id,role_snapshot,expires_at,created_at,last_used_at,revoked_at FROM api_tokens WHERE user_id = ?",
                (user_id,),
            ) as cur:
                rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def revoke_api_token(self, token_id: str, user_id: str) -> bool:
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                "UPDATE api_tokens SET revoked_at = ? WHERE id = ? AND user_id = ?",
                (_now(), token_id, user_id),
            )
            await db.commit()
            return cur.rowcount > 0

    async def lookup_api_token(self, raw_token: str) -> dict[str, Any] | None:
        """按原始令牌查有效 api_token 行 (撤销与过期均视为无效).

        SP-AUTH-7 (概念术语审计 P1-2): 网关鉴权尚未消费本方法 (api_token 目前只有
        签发/撤销面)。将来接线时, ``role_snapshot`` 只能与持有人当前角色取**交集**
        (快照只降不升), 禁止直接采信快照 — 否则角色降级后旧 token 永久保留高权限。
        """
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM api_tokens WHERE hash = ? AND revoked_at IS NULL AND (expires_at IS NULL OR expires_at > ?)",
                (token_hash, _now()),
            ) as cur:
                row = await cur.fetchone()
        return dict(row) if row else None

    # ─── GDPR (P4.5.5 12.10) ────────────────────────────

    async def soft_delete_user(self, user_id: str) -> bool:
        """Retire the identity; enrolled deletion jobs always keep their deadline."""
        from bowhead_core.account_deletion import AccountDeletionError, request_account_deletion

        if self.account_deletion_recorder is None:
            raise AccountDeletionError("account_deletion_not_ready")
        await request_account_deletion(self, user_id, record_intent=self.account_deletion_recorder)
        return True

    async def admin_soft_delete_member(
        self,
        *,
        target_user_id: str,
        team_id: str,
        actor_user_id: str,
        ip: str | None = None,
        user_agent: str | None = None,
    ) -> tuple[bool, str | None]:
        """Compatibility wrapper returning the historical two-item tuple."""
        result = await self.admin_soft_delete_member_detailed(
            target_user_id=target_user_id,
            team_id=team_id,
            actor_user_id=actor_user_id,
            ip=ip,
            user_agent=user_agent,
        )
        return (result.changed, result.error_code)

    async def admin_soft_delete_member_detailed(
        self,
        *,
        target_user_id: str,
        team_id: str,
        actor_user_id: str,
        ip: str | None = None,
        user_agent: str | None = None,
        delete_identity: bool = False,
    ) -> MemberDeactivationResult:
        """Remove ANOTHER member from one team and free that team's seat.

        By default identity lifecycle remains independent: the transaction changes only the
        target ``team_members`` row and workspace grants owned by that team.  When
        ``delete_identity`` is requested, it first verifies this database has exactly one active
        team, then atomically soft-deletes the User and revokes their refresh sessions too.

        The same transaction blocks enabled shared Subagents and quarantines
        private Subagents before removing membership.
        """
        from bowhead_core._email_utils import mask_email
        from bowhead_core.audit_actions import AuditAction
        from bowhead_core.team_subagent_store import TeamSubagentStore

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute(
                    "SELECT u.status AS identity_status,u.email,tm.status AS membership_status "
                    "FROM users u JOIN team_members tm ON tm.user_id=u.id "
                    "WHERE u.id=? AND tm.team_id=?",
                    (target_user_id, team_id),
                ) as cur:
                    urow = await cur.fetchone()
                if not urow:
                    await db.rollback()
                    return MemberDeactivationResult(False, "target_not_found")
                # Self-deactivate is the account-delete flow, not seat mgmt — block at the data layer too
                # (symmetric with the owner/last-admin guards; the endpoint also pre-blocks for a 422).
                if target_user_id == actor_user_id:
                    await db.rollback()
                    return MemberDeactivationResult(
                        False,
                        "cannot_self_deactivate",
                    )
                async with db.execute(
                    "SELECT created_by_user_id FROM teams WHERE id = ?",
                    (team_id,),
                ) as cur:
                    orow = await cur.fetchone()
                if orow and orow["created_by_user_id"] == target_user_id:
                    await db.rollback()
                    return MemberDeactivationResult(
                        False,
                        "owner_immutable",
                    )  # ownership changes through transfer
                if urow["membership_status"] != "active":
                    await db.rollback()
                    return MemberDeactivationResult(
                        False,
                        "already_inactive",
                    )  # idempotent — seat already freed
                if delete_identity:
                    async with db.execute(
                        "SELECT COUNT(*) AS n FROM teams WHERE status = 'active'",
                    ) as cur:
                        active_team_count = await cur.fetchone()
                    if active_team_count is None or int(active_team_count["n"]) != 1:
                        await db.rollback()
                        return MemberDeactivationResult(
                            False,
                            "local_team_invariant_failed",
                        )
                # Last-admin guard (CMP-6): block if target is an active team_admin with no OTHER active team_admin.
                async with db.execute(
                    "SELECT role FROM team_members WHERE user_id = ? AND team_id = ? AND status = 'active'",
                    (target_user_id, team_id),
                ) as cur:
                    mrow = await cur.fetchone()
                if mrow and mrow["role"] == "team_admin":
                    async with db.execute(
                        "SELECT COUNT(*) "
                        "FROM team_members om "
                        "JOIN users u ON u.id = om.user_id AND u.status = 'active' "
                        "WHERE om.team_id = ? AND om.role = 'team_admin' "
                        "AND om.status = 'active' AND om.user_id != ?",
                        (team_id, target_user_id),
                    ) as cur:
                        crow = await cur.fetchone()
                    if crow is None or crow[0] == 0:  # no OTHER active team_admin → fail-closed, block
                        await db.rollback()
                        return MemberDeactivationResult(
                            False,
                            "last_team_admin",
                        )
                # Workspace last-admin guard: deactivation also removes the member from all team workspaces.
                # If the target is the sole active admin in any canonical workspace, block before mutating.
                async with db.execute(
                    "SELECT wm.workspace_id "
                    "FROM workspace_members wm "
                    "JOIN workspaces w ON w.id = wm.workspace_id "
                    "WHERE wm.user_id = ? AND w.team_id = ? AND wm.status = 'active' "
                    "AND wm.role IN ('admin', 'ws_admin', 'owner', 'workspace_admin')",
                    (target_user_id, team_id),
                ) as cur:
                    admin_ws_rows = await cur.fetchall()
                for ws_row in admin_ws_rows:
                    workspace_id = ws_row["workspace_id"]
                    async with db.execute(
                        "SELECT COUNT(*) AS n "
                        "FROM workspace_members wm "
                        "JOIN users u ON u.id = wm.user_id AND u.status = 'active' "
                        "WHERE wm.workspace_id = ? AND wm.status = 'active' "
                        "AND wm.role IN ('admin', 'ws_admin', 'owner', 'workspace_admin') "
                        "AND wm.user_id != ?",
                        (workspace_id, target_user_id),
                    ) as cur:
                        ws_count_row = await cur.fetchone()
                    if ws_count_row is None or int(ws_count_row["n"]) == 0:
                        await db.rollback()
                        return MemberDeactivationResult(
                            False,
                            "last_workspace_admin",
                        )
                subagent_result = await TeamSubagentStore(
                    self.db_path,
                ).prepare_member_deactivation_conn(
                    db,
                    team_id=team_id,
                    owner_user_id=target_user_id,
                    actor_user_id=actor_user_id,
                )
                if subagent_result.enabled_shared:
                    await db.rollback()
                    return MemberDeactivationResult(
                        False,
                        "subagent_lifecycle_action_required",
                        {
                            "enabled_shared": (subagent_result.enabled_shared),
                            "governance_path": ("/team/settings/subagents/"),
                        },
                    )
                now = _now()
                await db.execute(
                    "UPDATE team_members SET status = 'removed', removed_at = ?, removed_by = ? "
                    "WHERE user_id = ? AND team_id = ? AND status = 'active'",
                    (now, actor_user_id, target_user_id, team_id),
                )
                await db.execute(
                    "UPDATE workspace_members SET status = 'removed' "
                    "WHERE user_id = ? AND status = 'active' "
                    "AND workspace_id IN (SELECT id FROM workspaces WHERE team_id = ?)",
                    (target_user_id, team_id),
                )
                await self._bump_team_membership_epoch_conn(db, team_id)
                if delete_identity:
                    from bowhead_core.account_deletion import retire_account_conn

                    if self.account_deletion_recorder is None:
                        await db.rollback()
                        return MemberDeactivationResult(False, "account_deletion_not_ready")
                    retirement = await retire_account_conn(db, self, target_user_id, requested_at=datetime.fromisoformat(now))
                    await self.account_deletion_recorder(retirement)
                await self.write_admin_audit_log(
                    actor_user_id=actor_user_id,
                    team_id=team_id,
                    action=AuditAction.TEAM_MEMBER_DEACTIVATED,
                    target_kind="user",
                    target_id=target_user_id,
                    target_team_id=team_id,
                    scope="team",
                    payload={
                        "target_email_mask": mask_email(urow["email"]),
                        "reason": ("admin_deactivate_local_single_team" if delete_identity else "admin_deactivate"),
                        "identity_deleted": delete_identity,
                        "private_subagents_quarantined": (subagent_result.quarantined_private),
                        "private_allow_grants_revoked": (subagent_result.revoked_private_allow_grants),
                    },
                    ip=ip,
                    user_agent=user_agent,
                    conn=db,  # atomic — audit in the same tx
                )
                await db.commit()
                return MemberDeactivationResult(True, identity_deleted=delete_identity)
            except Exception:
                await db.rollback()
                raise

    async def export_user_data(self, user_id: str) -> dict[str, Any]:
        """GDPR data export — return all user-owned rows as dict."""
        export: dict[str, Any] = {"user_id": user_id, "exported_at": _now()}
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            for tbl in ("users", "api_tokens", "credit_ledger", "user_audit_log", "workspace_members"):
                async with db.execute(f"PRAGMA table_info({tbl})") as cur:
                    cols = [r["name"] async for r in cur]
                if "user_id" not in cols:
                    continue
                async with db.execute(
                    f"SELECT * FROM {tbl} WHERE user_id = ?",
                    (user_id,),
                ) as cur:
                    rows = await cur.fetchall()
                export[tbl] = [dict(r) for r in rows]
        return export

    # ─── credit double-deduct (atomic, 12.4 fix) ────────

    async def record_credit_exhaustion_event(
        self,
        *,
        team_id: str,
        user_id: str,
        workspace_id: str | None = None,
        code: str,
        credit_balance: int | None = None,
        credit_quota: int | None = None,
        team_credit_balance: int | None = None,
        team_credit_quota: int | None = None,
        turn_id: str | None = None,
        created_at: str | None = None,
    ) -> dict[str, Any]:
        """Persist a normalized credit hard-block or deduct-failure event."""

        if code not in {"user_credit_exhausted", "team_pool_exhausted", "credit_deduct_failed"}:
            raise ValueError(f"invalid credit exhaustion code: {code}")
        event_id = _id("cex")
        ts = created_at or _now()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO credit_exhaustion_events("
                "id, team_id, user_id, workspace_id, code, credit_balance, "
                "credit_quota, team_credit_balance, team_credit_quota, turn_id, created_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    event_id,
                    team_id,
                    user_id,
                    workspace_id,
                    code,
                    credit_balance,
                    credit_quota,
                    team_credit_balance,
                    team_credit_quota,
                    turn_id,
                    ts,
                ),
            )
            await db.commit()
        return {
            "id": event_id,
            "team_id": team_id,
            "user_id": user_id,
            "workspace_id": workspace_id,
            "code": code,
            "credit_balance": credit_balance,
            "credit_quota": credit_quota,
            "team_credit_balance": team_credit_balance,
            "team_credit_quota": team_credit_quota,
            "turn_id": turn_id,
            "created_at": ts,
        }

    async def record_llm_usage_events(self, rows: list[dict[str, Any]]) -> int:
        """批量落 `llm_usage_events`（credit-billing gap 7 计量，非计费）。

        批量而非逐条：Router 的计量 hook 跑在每次 LLM 调用之后，逐条写会把 SQLite 的单写者
        争用直接搬到 chat 热路径上——这正是 §9.1 里反对 per-call 扣费的同一个理由。调用方
        （`llm_usage_recorder`）在内存里攒一批，由后台 flush 一次性写。

        每行需要 `reason` / `agent_type`，其余键缺省即空。返回真正写入的行数。
        """
        if not rows:
            return 0
        now = _now()
        payload = [
            (
                _id("lu"),
                r.get("team_id"),
                r.get("user_id"),
                r.get("workspace_id"),
                r.get("turn_id"),
                str(r.get("reason") or "unattributed"),
                str(r.get("agent_type") or "unknown"),
                r.get("provider"),
                r.get("model"),
                int(r.get("tokens_input") or 0),
                int(r.get("tokens_output") or 0),
                int(r.get("tokens_cached") or 0),
                1 if r.get("streamed") else 0,
                str(r.get("created_at") or now),
            )
            for r in rows
        ]
        async with aiosqlite.connect(self.db_path) as db:
            await db.executemany(
                "INSERT INTO llm_usage_events(id,team_id,user_id,workspace_id,turn_id,reason,"
                "agent_type,provider,model,tokens_input,tokens_output,tokens_cached,streamed,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                payload,
            )
            await db.commit()
        return len(payload)

    async def rollup_llm_usage_daily(self, *, days: tuple[str, ...] | None = None, before: str | None = None) -> int:
        """把 `llm_usage_events` 按天聚合进 `llm_usage_daily`。返回写入的汇总行数。

        **幂等**：用 `INSERT ... ON CONFLICT DO UPDATE SET <col> = excluded.<col>`——即整段
        **覆盖**而非累加。所以同一天重跑多少次结果都一样；这也是它能安全重跑补历史的原因。
        （若写成 `col = col + excluded.col`，重跑会把当天用量翻倍，是这类汇总最常见的坑。）

        `days` 显式指定要汇总哪几天（`YYYY-MM-DD`）；`before` 则汇总该时间点之前的所有天。
        两者都不给时汇总**全部**已有原始数据（首次接线/补历史用）。
        """
        where = ""
        args: tuple[Any, ...] = ()
        if days:
            where = f" WHERE substr(created_at,1,10) IN ({','.join('?' for _ in days)})"
            args = tuple(days)
        elif before:
            where = " WHERE created_at < ?"
            args = (before,)
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                "INSERT INTO llm_usage_daily(day,team_id,user_id,workspace_id,reason,"
                "calls,tokens_input,tokens_output,tokens_cached,updated_at) "
                "SELECT substr(created_at,1,10) AS day, COALESCE(team_id,''), COALESCE(user_id,''), "
                "COALESCE(workspace_id,''), reason, COUNT(*), COALESCE(SUM(tokens_input),0), "
                "COALESCE(SUM(tokens_output),0), COALESCE(SUM(tokens_cached),0), ? "
                "FROM llm_usage_events" + where + " "
                "GROUP BY day, COALESCE(team_id,''), COALESCE(user_id,''), COALESCE(workspace_id,''), reason "
                "ON CONFLICT(day,team_id,user_id,workspace_id,reason) DO UPDATE SET "
                "calls=excluded.calls, tokens_input=excluded.tokens_input, "
                "tokens_output=excluded.tokens_output, tokens_cached=excluded.tokens_cached, "
                "updated_at=excluded.updated_at",
                (_now(), *args),
            )
            await db.commit()
            return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0

    async def prune_llm_usage_events(self, *, older_than: str, require_rolled_up: bool = True) -> int:
        """裁掉 `created_at < older_than` 的原始用量行，返回删除行数。

        `require_rolled_up=True`（默认）时**只删已进汇总的天**：逐天核对 `llm_usage_daily`
        里有该天的行才删。这样即使调用方忘了先 rollup，也不会把还没汇总的数据直接删掉——
        裁剪与汇总的顺序错误是这类清理任务最容易造成不可逆数据丢失的地方。
        """
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT DISTINCT substr(created_at,1,10) AS day FROM llm_usage_events WHERE created_at < ?",
                (older_than,),
            ) as cur:
                candidate_days = [r["day"] for r in await cur.fetchall()]
            if not candidate_days:
                return 0
            deletable = candidate_days
            if require_rolled_up:
                async with db.execute(
                    f"SELECT DISTINCT day FROM llm_usage_daily WHERE day IN ({','.join('?' for _ in candidate_days)})",
                    tuple(candidate_days),
                ) as cur:
                    rolled = {r["day"] for r in await cur.fetchall()}
                deletable = [d for d in candidate_days if d in rolled]
                skipped = [d for d in candidate_days if d not in rolled]
                if skipped:
                    logger.warning(
                        "llm_usage.prune_skipped_unrolled days=%s -- 这些天还没进日级汇总, 不删",
                        sorted(skipped)[:10],
                    )
            if not deletable:
                return 0
            # Batch dispatch: `deletable` length equals days to expire; years of backlog (legacy db without cron) exceed
            # SQLITE_MAX_VARIABLE_NUMBER in one IN clause. Chunk by days to avoid it.
            deleted = 0
            for i in range(0, len(deletable), 200):
                part = deletable[i : i + 200]
                placeholders = ",".join("?" for _ in part)
                cur = await db.execute(
                    f"DELETE FROM llm_usage_events WHERE created_at < ? AND substr(created_at,1,10) IN ({placeholders})",
                    (older_than, *part),
                )
                deleted += cur.rowcount or 0
            await db.commit()
        return deleted

    async def llm_usage_daily_by_reason(self, *, since_day: str | None = None) -> list[dict[str, Any]]:
        """从**日级汇总**按 reason 汇总 —— 超出原始保留窗口的历史只能从这里查。"""
        sql = (
            "SELECT reason, SUM(calls) AS calls, SUM(tokens_input) AS tokens_input, "
            "SUM(tokens_output) AS tokens_output, SUM(tokens_cached) AS tokens_cached "
            "FROM llm_usage_daily"
        )
        args: tuple[Any, ...] = ()
        if since_day:
            sql += " WHERE day >= ?"
            args = (since_day,)
        sql += " GROUP BY reason ORDER BY (SUM(tokens_input) + SUM(tokens_output)) DESC"
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, args) as cur:
                return [dict(r) for r in await cur.fetchall()]

    async def llm_usage_by_reason(self, *, since: str | None = None) -> list[dict[str, Any]]:
        """按 `reason` 汇总用量 —— 回答「后台调用到底烧了多少」这个决策问题。

        这是 gap 7 的产出口：拿到真实占比后，才谈「系统自发的记忆蒸馏是否该由用户付费」。
        """
        sql = (
            "SELECT reason, COUNT(*) AS calls, SUM(tokens_input) AS tokens_input, "
            "SUM(tokens_output) AS tokens_output, SUM(tokens_cached) AS tokens_cached "
            "FROM llm_usage_events"
        )
        args: tuple[Any, ...] = ()
        if since:
            sql += " WHERE created_at >= ?"
            args = (since,)
        sql += " GROUP BY reason ORDER BY (SUM(tokens_input) + SUM(tokens_output)) DESC"
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, args) as cur:
                return [dict(r) for r in await cur.fetchall()]

    async def try_deduct_credit(
        self,
        *,
        user_id: str,
        team_id: str,
        cost_credits: int,
        turn_id: str | None = None,
        workspace_id: str | None = None,
        tokens_input: int = 0,
        tokens_output: int = 0,
        reason: str = "turn",
        model_usage: tuple[ModelUsageLine, ...] = (),
        rate_card_version: str = "v1",
    ) -> tuple[bool, str]:
        """双层 atomic: user 跟 team 同时扣. 余额不足照实扣到负数（结算不是准入, 见
        `_deduct_credit_with_stable_id_in_tx`）; 仅 user/team 行不存在时不扣.
        返 (success, error_reason).

        reason (kb-matrix M2 §8④): ledger 行的 reason 标签. 默认 "turn" =
        chat/scheduled 调用点零改; 矩阵 run 传 "kb_matrix_run" (turn_id 列锚
        mxrun_ 前缀 run_id, 对账反查键).
        """
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                outcome, error_reason = await _deduct_credit_with_stable_id_in_tx(
                    db,
                    ledger_id=_id("led"),
                    user_id=user_id,
                    team_id=team_id,
                    cost_credits=cost_credits,
                    turn_id=turn_id,
                    workspace_id=workspace_id,
                    tokens_input=tokens_input,
                    tokens_output=tokens_output,
                    reason=reason,
                    rate_card_version=rate_card_version,
                    model_usage=model_usage,
                )
                if outcome == "confirmed_not_applied":
                    await db.rollback()
                    return False, error_reason
                await db.commit()
                return True, ""
            except Exception:
                await db.execute("ROLLBACK")
                raise

    async def admin_grant(
        self,
        *,
        user_id: str,
        team_id: str,
        amount: int,
        by_user_id: str,
        reason_note: str | None = None,
    ) -> int:
        """Admin 给 user 加 credit (从 team pool 调拨, 双层 atomic).

        语义跟 try_deduct_credit 对称: 加 user.balance + 减 team.balance + 写 ledger
        + audit log. team pool 不足返 ValueError (caller 转 4xx).

        返新的 user.credit_balance.
        """
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                # 1. user.balance += amount
                cur = await db.execute(
                    "UPDATE users SET credit_balance = credit_balance + ? WHERE id = ? AND team_id = ? AND status = 'active'",
                    (amount, user_id, team_id),
                )
                if cur.rowcount == 0:
                    raise ValueError("user_not_active")
                # 2. team.balance -= amount (sufficient check)
                cur = await db.execute(
                    "UPDATE teams SET team_credit_balance = team_credit_balance - ? WHERE id = ? AND team_credit_balance >= ?",
                    (amount, team_id, amount),
                )
                if cur.rowcount == 0:
                    raise ValueError("team_pool_insufficient")
                # 3. ledger
                await db.execute(
                    "INSERT INTO credit_ledger(id,user_id,team_id,workspace_id,turn_id,"
                    "delta,reason,tokens_input,tokens_output,created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (_id("led"), user_id, team_id, None, None, amount, f"admin_grant:{by_user_id}", 0, 0, _now()),
                )
                # 4. audit log
                await db.execute(
                    "INSERT INTO user_audit_log(id,user_id,team_id,event_type,ip,user_agent,payload_json,created_at) VALUES (?,?,?,?,?,?,?,?)",
                    (
                        _id("aud"),
                        by_user_id,
                        team_id,
                        "credit_admin_grant",
                        None,
                        None,
                        json.dumps(
                            {
                                "target_user_id": user_id,
                                "amount": amount,
                                "reason": reason_note,
                            },
                            ensure_ascii=False,
                        ),
                        _now(),
                    ),
                )
                # 5. fetch new balance
                async with db.execute(
                    "SELECT credit_balance FROM users WHERE id = ?",
                    (user_id,),
                ) as bcur:
                    brow = await bcur.fetchone()
                new_balance = int(brow[0]) if brow else 0
                await db.commit()
                return new_balance
            except Exception:
                await db.execute("ROLLBACK")
                raise

    # ─── admin credits write (V3) ────────────────────────

    async def admin_set_user_credit(
        self,
        *,
        user_id: str,
        team_id: str,
        by_user_id: str,
        tier: str | None = None,
        credit_quota: int | None = None,
        credit_balance: int | None = None,
        reset_quota_with_tier: bool = True,
        pull_balance_to_quota: bool = False,
    ) -> dict[str, Any]:
        """改 user tier / quota / balance, 含联动 + cap + audit.

        语义:
          - 改 tier: 若 reset_quota_with_tier=True, 同时 quota = tier_credits[new_tier]
          - 改 quota: 若 pull_balance_to_quota=True, 同时 balance = new_quota
          - 改 balance: 写 ledger reason='admin_set_balance:{by_user_id}'
                     (delta = new_balance - old_balance, 可正可负)

        返新 user state dict (含 tier/credit_quota/credit_balance/...).
        失败:
          - user 不存在 / team 跨 → ValueError("user_not_found_or_cross_team")
          - user.status='removed' → ValueError("user_removed_cannot_edit")
        """
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                # 1. Fetch before snapshot + validate team
                async with db.execute(
                    "SELECT id, team_id, tier, credit_balance, credit_quota, credit_period_start, status, email, display_name FROM users WHERE id = ?",
                    (user_id,),
                ) as cur:
                    before = await cur.fetchone()
                if not before or before["team_id"] != team_id:
                    raise ValueError("user_not_found_or_cross_team")
                if before["status"] != "active":
                    raise ValueError("user_removed_cannot_edit")

                # 2. fetch tier_credits (for tier change linkage)
                tier_credits: dict[str, int] = {}
                if tier is not None and reset_quota_with_tier:
                    async with db.execute(
                        "SELECT tier_credits_json FROM teams WHERE id = ?",
                        (team_id,),
                    ) as ocur:
                        team_row = await ocur.fetchone()
                    if team_row:
                        tier_credits = json.loads(
                            team_row["tier_credits_json"] or "{}",
                        )

                # 3. compute after
                new_tier = tier if tier is not None else before["tier"]
                new_quota = credit_quota if credit_quota is not None else int(before["credit_quota"])
                new_balance = credit_balance if credit_balance is not None else int(before["credit_balance"])

                # Tier updates quota.
                if tier is not None and reset_quota_with_tier and credit_quota is None and new_tier in tier_credits:
                    new_quota = int(tier_credits[new_tier])

                # Quota updates balance.
                if credit_quota is not None and pull_balance_to_quota and credit_balance is None:
                    new_balance = new_quota

                # 4. apply user
                await db.execute(
                    "UPDATE users SET tier=?, credit_quota=?, credit_balance=? WHERE id=? AND team_id=?",
                    (new_tier, new_quota, new_balance, user_id, team_id),
                )

                # 5. ledger (only if balance changed)
                balance_delta = new_balance - int(before["credit_balance"])
                if balance_delta != 0:
                    await db.execute(
                        "INSERT INTO credit_ledger(id,user_id,team_id,workspace_id,turn_id,"
                        "delta,reason,tokens_input,tokens_output,created_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (_id("led"), user_id, team_id, None, None, balance_delta, f"admin_set_balance:{by_user_id}", 0, 0, _now()),
                    )

                # 6. audit
                after_dict = {
                    "tier": new_tier,
                    "quota": new_quota,
                    "balance": new_balance,
                }
                before_dict = {
                    "tier": before["tier"],
                    "quota": int(before["credit_quota"]),
                    "balance": int(before["credit_balance"]),
                }
                await db.execute(
                    "INSERT INTO user_audit_log(id,user_id,team_id,event_type,ip,user_agent,payload_json,created_at) VALUES (?,?,?,?,?,?,?,?)",
                    (
                        _id("aud"),
                        by_user_id,
                        team_id,
                        "credit_admin_set_user",
                        None,
                        None,
                        json.dumps(
                            {
                                "target_user_id": user_id,
                                "before": before_dict,
                                "after": after_dict,
                                "options": {
                                    "reset_quota_with_tier": reset_quota_with_tier,
                                    "pull_balance_to_quota": pull_balance_to_quota,
                                },
                            },
                            ensure_ascii=False,
                        ),
                        _now(),
                    ),
                )
                await db.commit()
                return {
                    "user_id": user_id,
                    "email": before["email"],
                    "display_name": before["display_name"],
                    "tier": new_tier,
                    "credit_balance": new_balance,
                    "credit_quota": new_quota,
                    "credit_period_start": before["credit_period_start"],
                    "status": before["status"],
                }
            except Exception:
                await db.execute("ROLLBACK")
                raise

    async def admin_period_reset(
        self,
        *,
        team_id: str,
        by_user_id: str,
        mode: str,  # "balance_only" | "balance_and_quota"
        confirm: bool = False,
    ) -> dict[str, Any]:
        """周期 reset. confirm=False → preview only; True → 真做 + audit.

        Cooldown: 距上次同 team reset < 24h → 抛 RuntimeError("cooldown:NNNN")
        其中 NNNN 是 retry_after_seconds.

        balance_only:
          - 每个 active user.credit_balance ← user.credit_quota
          - team_credit_balance ← team_credit_quota
          - 写 audit + ledger reason='period_reset' (per user one row, delta=balance change)

        balance_and_quota:
          - 每个 active user 按 tier_credits 重写 quota + balance
          - team_credit_balance ← team_credit_quota
        """
        if mode not in ("balance_only", "balance_and_quota"):
            raise ValueError(f"invalid mode: {mode}")

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row

            # cooldown check
            async with db.execute(
                "SELECT created_at FROM user_audit_log "
                "WHERE team_id = ? AND event_type = 'credit_admin_period_reset' "
                "ORDER BY created_at DESC LIMIT 1",
                (team_id,),
            ) as cur:
                last_row = await cur.fetchone()
            if last_row:
                last_at = datetime.fromisoformat(last_row["created_at"])
                age = datetime.now(UTC) - last_at
                if age.total_seconds() < 24 * 3600:
                    retry = int(24 * 3600 - age.total_seconds())
                    raise RuntimeError(f"cooldown:{retry}")

            # fetch active users + team
            async with db.execute(
                "SELECT id, email, tier, credit_balance, credit_quota FROM users WHERE team_id = ? AND status = 'active'",
                (team_id,),
            ) as cur:
                user_rows = await cur.fetchall()
            async with db.execute(
                "SELECT team_credit_balance, team_credit_quota, tier_credits_json FROM teams WHERE id = ?",
                (team_id,),
            ) as cur:
                team_row = await cur.fetchone()
            if not team_row:
                raise ValueError("team_not_found")
            tier_credits = json.loads(team_row["tier_credits_json"] or "{}")

            # build plan
            plan: list[dict[str, Any]] = []
            custom_overrides: list[dict[str, Any]] = []
            for u in user_rows:
                before = {
                    "tier": u["tier"],
                    "quota": int(u["credit_quota"]),
                    "balance": int(u["credit_balance"]),
                }
                if mode == "balance_only":
                    after = dict(before)
                    after["balance"] = before["quota"]
                else:  # balance_and_quota
                    new_q = int(tier_credits.get(u["tier"], before["quota"]))
                    after = {"tier": before["tier"], "quota": new_q, "balance": new_q}
                    if before["quota"] != new_q:
                        custom_overrides.append(
                            {
                                "user_id": u["id"],
                                "email": u["email"],
                                "current_quota": before["quota"],
                                "new_quota": new_q,
                            }
                        )
                plan.append(
                    {
                        "user_id": u["id"],
                        "email": u["email"],
                        "before": before,
                        "after": after,
                    }
                )

            team_balance_before = int(team_row["team_credit_balance"])
            team_balance_after = int(team_row["team_credit_quota"])

            preview = {
                "users": plan,
                "custom_quota_overrides": custom_overrides,
                "team_balance_before": team_balance_before,
                "team_balance_after": team_balance_after,
                "mode": mode,
            }

            if not confirm:
                return {"ok": False, "preview": preview, "affected_users": len(plan)}

            # really do it
            await db.execute("BEGIN IMMEDIATE")
            try:
                now_iso = _now()
                for p in plan:
                    await db.execute(
                        "UPDATE users SET tier=?, credit_quota=?, credit_balance=?, credit_period_start=? WHERE id=? AND team_id=?",
                        (p["after"]["tier"], p["after"]["quota"], p["after"]["balance"], now_iso, p["user_id"], team_id),
                    )
                    delta = p["after"]["balance"] - p["before"]["balance"]
                    if delta != 0:
                        await db.execute(
                            "INSERT INTO credit_ledger(id,user_id,team_id,workspace_id,"
                            "turn_id,delta,reason,tokens_input,tokens_output,created_at) "
                            "VALUES (?,?,?,?,?,?,?,?,?,?)",
                            (_id("led"), p["user_id"], team_id, None, None, delta, "period_reset", 0, 0, now_iso),
                        )
                # team reset
                await db.execute(
                    "UPDATE teams SET team_credit_balance=team_credit_quota, team_credit_period_start=? WHERE id=?",
                    (now_iso, team_id),
                )
                # Audit (complete snapshot permits manual rollback after mistakes)
                await db.execute(
                    "INSERT INTO user_audit_log(id,user_id,team_id,event_type,ip,user_agent,payload_json,created_at) VALUES (?,?,?,?,?,?,?,?)",
                    (
                        _id("aud"),
                        by_user_id,
                        team_id,
                        "credit_admin_period_reset",
                        None,
                        None,
                        json.dumps(
                            {"mode": mode, "snapshot": plan, "team_balance_before": team_balance_before, "team_balance_after": team_balance_after},
                            ensure_ascii=False,
                        ),
                        now_iso,
                    ),
                )
                await db.commit()
                return {
                    "ok": True,
                    "affected_users": len(plan),
                    "mode": mode,
                }
            except Exception:
                await db.execute("ROLLBACK")
                raise

    async def credits_user_ledger(
        self,
        user_id: str,
        *,
        team_id: str,
        page: int = 1,
        size: int = 50,
        from_ts: str | None = None,
        to_ts: str | None = None,
        type_filter: str | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        """单用户 ledger 明细 (drill-down). 跨 team 过滤."""
        where = ["user_id = ?", "team_id = ?"]
        params: list[Any] = [user_id, team_id]
        if from_ts:
            where.append("created_at >= ?")
            params.append(from_ts)
        if to_ts:
            where.append("created_at <= ?")
            params.append(to_ts)
        if type_filter:
            # type_filter may be 'turn' / 'admin_grant' / 'admin_set_balance' / 'period_reset'
            where.append("(reason = ? OR reason LIKE ?)")
            params.extend([type_filter, f"{type_filter}:%"])
        where_sql = " AND ".join(where)

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                f"SELECT COUNT(*) AS n FROM credit_ledger WHERE {where_sql}",
                params,
            ) as cur:
                count_row = await cur.fetchone()
            async with db.execute(
                f"SELECT id, workspace_id, turn_id, delta, reason, "
                f"tokens_input, tokens_output, created_at FROM credit_ledger "
                f"WHERE {where_sql} ORDER BY created_at DESC LIMIT ? OFFSET ?",
                [*params, size, max(0, (page - 1) * size)],
            ) as cur:
                rows = await cur.fetchall()
            ledger_ids = [str(row["id"]) for row in rows]
            model_rows: list[aiosqlite.Row] = []
            if ledger_ids:
                async with db.execute(
                    f"SELECT ledger_id,provider,model,tokens_input,tokens_cached,tokens_output,rate_input,rate_cached,rate_output,weighted_tokens "
                    f"FROM credit_model_usage WHERE ledger_id IN ({','.join('?' for _ in ledger_ids)}) ORDER BY ledger_id,line_no",
                    ledger_ids,
                ) as cur:
                    model_rows = list(await cur.fetchall())

        model_usage_by_ledger: dict[str, list[dict[str, Any]]] = {}
        for model_row in model_rows:
            model_usage_by_ledger.setdefault(str(model_row["ledger_id"]), []).append(
                {
                    "provider": model_row["provider"],
                    "model": model_row["model"],
                    "tokens_input": int(model_row["tokens_input"]),
                    "tokens_cached": int(model_row["tokens_cached"]),
                    "tokens_output": int(model_row["tokens_output"]),
                    "rate_input": model_row["rate_input"],
                    "rate_cached": model_row["rate_cached"],
                    "rate_output": model_row["rate_output"],
                    "weighted_tokens": model_row["weighted_tokens"],
                }
            )

        items = [
            {
                "id": r["id"],
                "type": (r["reason"] or "").split(":")[0],
                "workspace_id": r["workspace_id"],
                "turn_id_short": r["turn_id"][:8] if r["turn_id"] else None,
                "tokens_input": int(r["tokens_input"] or 0),
                "tokens_output": int(r["tokens_output"] or 0),
                "model_usage": model_usage_by_ledger.get(str(r["id"]), []),
                "delta": int(r["delta"]),
                "created_at": r["created_at"],
            }
            for r in rows
        ]
        total = int(count_row["n"]) if count_row else 0
        return items, total

    async def credits_audit_log(
        self,
        team_id: str,
        *,
        page: int = 1,
        size: int = 50,
        from_ts: str | None = None,
        to_ts: str | None = None,
        event_type: str | None = None,
        actor_user_id: str | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        """审计 log (credit_admin_* 事件). team-scoped."""
        where = ["team_id = ?", "event_type LIKE 'credit_admin_%'"]
        params: list[Any] = [team_id]
        if event_type:
            where.append("event_type = ?")
            params.append(event_type)
        if actor_user_id:
            where.append("user_id = ?")
            params.append(actor_user_id)
        if from_ts:
            where.append("created_at >= ?")
            params.append(from_ts)
        if to_ts:
            where.append("created_at <= ?")
            params.append(to_ts)
        where_sql = " AND ".join(where)

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                f"SELECT COUNT(*) AS n FROM user_audit_log WHERE {where_sql}",
                params,
            ) as cur:
                count_row = await cur.fetchone()
            async with db.execute(
                f"SELECT id, user_id AS actor_user_id, event_type, payload_json, created_at "
                f"FROM user_audit_log WHERE {where_sql} "
                f"ORDER BY created_at DESC LIMIT ? OFFSET ?",
                [*params, size, max(0, (page - 1) * size)],
            ) as cur:
                rows = await cur.fetchall()

        items = []
        for r in rows:
            try:
                payload = json.loads(r["payload_json"] or "{}")
            except (json.JSONDecodeError, TypeError):
                payload = {}
            items.append(
                {
                    "id": r["id"],
                    "event_type": r["event_type"],
                    "actor_user_id": r["actor_user_id"],
                    "target_user_id": payload.get("target_user_id"),
                    "payload": payload,
                    "created_at": r["created_at"],
                }
            )
        total = int(count_row["n"]) if count_row else 0
        return items, total

    # ─── admin credits read (V3) ────────────────────────

    async def materialize_credit_attention(
        self,
        team_id: str,
        *,
        limit: int = 20,
        now: str | None = None,
        metrics_log: Any | None = None,
    ) -> list[dict[str, Any]]:
        """Evaluate and persist current credit activation attention items."""

        from bowhead_core.credit_activation import (
            CreditTeamContext,
            CreditUsageCandidate,
            evaluate_credit_attention,
        )

        now_ts = now or _now()
        capped_limit = max(1, min(int(limit), 100))
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT team_credit_balance, team_credit_quota FROM teams WHERE id = ?",
                (team_id,),
            ) as cur:
                team_row = await cur.fetchone()
            if not team_row:
                return []

            async with db.execute(
                """
                WITH user_30d AS (
                  SELECT user_id, SUM(-delta) AS spend_30d,
                         COUNT(DISTINCT CASE
                           WHEN tokens_input + tokens_output >= 500 THEN turn_id
                         END) AS turns_30d,
                         MAX(created_at) AS last_activity
                  FROM credit_ledger
                  WHERE team_id = ? AND datetime(created_at) >= datetime('now', '-30 days')
                    AND delta < 0 AND reason = 'turn'
                  GROUP BY user_id
                ), user_7d AS (
                  SELECT user_id, SUM(-delta) AS spend_7d,
                         COUNT(DISTINCT CASE
                           WHEN tokens_input + tokens_output >= 500 THEN turn_id
                         END) AS turns_7d
                  FROM credit_ledger
                  WHERE team_id = ? AND datetime(created_at) >= datetime('now', '-7 days')
                    AND delta < 0 AND reason = 'turn'
                  GROUP BY user_id
                ), prior_30d AS (
                  SELECT user_id, SUM(-delta) AS prior_30d_spend
                  FROM credit_ledger
                  WHERE team_id = ?
                    AND datetime(created_at) >= datetime('now', '-60 days')
                    AND datetime(created_at) < datetime('now', '-30 days')
                    AND delta < 0 AND reason = 'turn'
                  GROUP BY user_id
                ), user_workspace_30d AS (
                  SELECT user_id, workspace_id, SUM(-delta) AS workspace_spend_30d
                  FROM credit_ledger
                  WHERE team_id = ? AND datetime(created_at) >= datetime('now', '-30 days')
                    AND delta < 0 AND reason = 'turn' AND workspace_id IS NOT NULL
                  GROUP BY user_id, workspace_id
                ), top_workspace AS (
                  SELECT user_id, workspace_id AS top_workspace_id, workspace_spend_30d
                  FROM (
                    SELECT user_id, workspace_id, workspace_spend_30d,
                           ROW_NUMBER() OVER (
                             PARTITION BY user_id
                             ORDER BY workspace_spend_30d DESC, workspace_id ASC
                           ) AS rn
                    FROM user_workspace_30d
                  )
                  WHERE rn = 1
                ), exhausted_7d AS (
                  SELECT user_id, COUNT(*) AS exhausted_blocks_7d
                  FROM credit_exhaustion_events
                  WHERE team_id = ? AND datetime(created_at) >= datetime('now', '-7 days')
                  GROUP BY user_id
                ), first_activity AS (
                  SELECT user_id, MIN(created_at) AS first_activity_at
                  FROM credit_ledger
                  WHERE team_id = ? AND delta < 0 AND reason = 'turn'
                    AND tokens_input + tokens_output >= 500
                  GROUP BY user_id
                )
                SELECT u.id AS user_id, u.email, u.display_name, u.tier, u.status,
                       u.credit_balance, u.credit_quota,
                       COALESCE(d7.spend_7d, 0) AS spend_7d,
                       COALESCE(d30.spend_30d, 0) AS spend_30d,
                       COALESCE(p30.prior_30d_spend, 0) AS prior_30d_spend,
                       COALESCE(d7.turns_7d, 0) AS turns_7d,
                       COALESCE(d30.turns_30d, 0) AS turns_30d,
                       d30.last_activity AS last_activity_at,
                       tw.top_workspace_id AS top_workspace_id,
                       COALESCE(ex.exhausted_blocks_7d, 0) AS exhausted_blocks_7d,
                       CASE
                         WHEN datetime(u.created_at) >= datetime('now', '-14 days')
                              OR datetime(fa.first_activity_at) >= datetime('now', '-14 days')
                         THEN 1 ELSE 0
                       END AS is_new_user
                FROM users u
                LEFT JOIN user_7d d7 ON d7.user_id = u.id
                LEFT JOIN user_30d d30 ON d30.user_id = u.id
                LEFT JOIN prior_30d p30 ON p30.user_id = u.id
                LEFT JOIN top_workspace tw ON tw.user_id = u.id
                LEFT JOIN exhausted_7d ex ON ex.user_id = u.id
                LEFT JOIN first_activity fa ON fa.user_id = u.id
                WHERE u.team_id = ? AND u.status = 'active'
                """,
                (team_id, team_id, team_id, team_id, team_id, team_id, team_id),
            ) as cur:
                user_rows = await cur.fetchall()

        team_context = CreditTeamContext(
            team_id=team_id,
            team_credit_balance=int(team_row["team_credit_balance"]),
            team_credit_quota=int(team_row["team_credit_quota"]),
        )
        usage_rows = [
            CreditUsageCandidate(
                user_id=r["user_id"],
                email=r["email"],
                display_name=r["display_name"],
                tier=r["tier"],
                status=r["status"],
                credit_balance=int(r["credit_balance"]),
                credit_quota=int(r["credit_quota"]),
                spend_7d=int(r["spend_7d"]),
                spend_30d=int(r["spend_30d"]),
                prior_30d_spend=int(r["prior_30d_spend"]),
                meaningful_turns_7d=int(r["turns_7d"]),
                meaningful_turns_30d=int(r["turns_30d"]),
                last_activity_at=r["last_activity_at"],
                exhausted_blocks_7d=int(r["exhausted_blocks_7d"]),
                top_workspace_id=r["top_workspace_id"],
                is_new_user=bool(r["is_new_user"]),
            )
            for r in user_rows
        ]
        candidates = evaluate_credit_attention(team_context, usage_rows, now=now_ts)
        events = await self._upsert_credit_attention_state(
            team_id,
            candidates,
            now_ts=now_ts,
        )
        await self._track_credit_attention_events(metrics_log, events)
        return await self.list_credit_attention_items(
            team_id,
            limit=capped_limit,
            mark_shown=False,
        )

    async def _upsert_credit_attention_state(
        self,
        team_id: str,
        candidates: list[Any],
        *,
        now_ts: str,
    ) -> list[dict[str, Any]]:
        dedupe_keys = [c.dedupe_key for c in candidates]
        expires_at = (datetime.now(UTC) + timedelta(days=_CREDIT_ATTENTION_OPEN_TTL_DAYS)).isoformat()
        events: list[dict[str, Any]] = []

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                for item in candidates:
                    payload = item.to_dict()
                    payload["attention_reason"] = item.attention_reason
                    payload_json = json.dumps(payload, ensure_ascii=False)
                    async with db.execute(
                        "SELECT attention_item_id, state FROM credit_attention_state "
                        "WHERE team_id = ? AND dedupe_key = ? "
                        "AND state IN ('open', 'acted') "
                        "ORDER BY opened_at DESC LIMIT 1",
                        (team_id, item.dedupe_key),
                    ) as cur:
                        existing = await cur.fetchone()

                    if existing:
                        assignments = [
                            "user_id = ?",
                            "workspace_id = ?",
                            "policy_key = ?",
                            "severity = ?",
                            "severity_rank = ?",
                            "expires_at = ?",
                            "payload_json = ?",
                            "updated_at = ?",
                            "resolved_at = NULL",
                        ]
                        params: list[Any] = [
                            item.user_id,
                            item.workspace_id,
                            item.policy_key,
                            item.severity,
                            item.severity_rank,
                            expires_at,
                            payload_json,
                            now_ts,
                        ]
                        if existing["state"] == "acted":
                            assignments.extend(
                                [
                                    "state = 'open'",
                                    "state_reason = 'protection_failed'",
                                ]
                            )
                            events.append(
                                _credit_attention_event_from_candidate(
                                    "credit.attention.item_opened",
                                    team_id=team_id,
                                    item=item,
                                    attention_item_id=existing["attention_item_id"],
                                    state_reason="protection_failed",
                                )
                            )
                        await db.execute(
                            f"UPDATE credit_attention_state SET {', '.join(assignments)} WHERE attention_item_id = ?",
                            [*params, existing["attention_item_id"]],
                        )
                    else:
                        async with db.execute(
                            "SELECT attention_item_id, state, state_reason, severity_rank "
                            "FROM credit_attention_state "
                            "WHERE team_id = ? AND dedupe_key = ? "
                            "AND state IN ('dismissed', 'protected', 'suppressed') "
                            "AND datetime(expires_at) > datetime(?) "
                            "ORDER BY updated_at DESC LIMIT 1",
                            (team_id, item.dedupe_key, now_ts),
                        ) as cur:
                            suppressed = await cur.fetchone()
                        if suppressed:
                            suppressed_state = str(suppressed["state"])
                            suppressed_reason = suppressed["state_reason"]
                            suppressed_rank = int(suppressed["severity_rank"] or 0)
                            if suppressed_state == "protected" and suppressed_reason == "materializer_protected":
                                events.append(
                                    _credit_attention_event_from_candidate(
                                        "credit.attention.item_opened",
                                        team_id=team_id,
                                        item=item,
                                        attention_item_id=suppressed["attention_item_id"],
                                        state_reason="protection_failed",
                                    )
                                )
                                await db.execute(
                                    "UPDATE credit_attention_state SET "
                                    "user_id = ?, workspace_id = ?, policy_key = ?, "
                                    "state = 'open', state_reason = 'protection_failed', "
                                    "severity = ?, severity_rank = ?, expires_at = ?, "
                                    "payload_json = ?, protected_at = NULL, "
                                    "protection_reason = NULL, protection_snapshot_json = '{}', "
                                    "resolved_at = NULL, updated_at = ? "
                                    "WHERE attention_item_id = ?",
                                    [
                                        item.user_id,
                                        item.workspace_id,
                                        item.policy_key,
                                        item.severity,
                                        item.severity_rank,
                                        expires_at,
                                        payload_json,
                                        now_ts,
                                        suppressed["attention_item_id"],
                                    ],
                                )
                                continue
                            if suppressed_state == "protected" or item.severity_rank <= suppressed_rank:
                                events.append(
                                    _credit_attention_event_from_candidate(
                                        "credit.attention.suppressed",
                                        team_id=team_id,
                                        item=item,
                                        attention_item_id=suppressed["attention_item_id"],
                                        suppressed_reason=suppressed_state,
                                    )
                                )
                                continue
                        item_id = _id("cai")
                        await db.execute(
                            "INSERT INTO credit_attention_state("
                            "attention_item_id, team_id, user_id, workspace_id, "
                            "policy_key, dedupe_key, state, state_reason, severity, "
                            "severity_rank, opened_at, expires_at, payload_json, "
                            "created_at, updated_at"
                            ") VALUES (?, ?, ?, ?, ?, ?, 'open', NULL, ?, ?, ?, ?, ?, ?, ?)",
                            (
                                item_id,
                                team_id,
                                item.user_id,
                                item.workspace_id,
                                item.policy_key,
                                item.dedupe_key,
                                item.severity,
                                item.severity_rank,
                                now_ts,
                                expires_at,
                                payload_json,
                                now_ts,
                                now_ts,
                            ),
                        )
                        events.append(
                            _credit_attention_event_from_candidate(
                                "credit.attention.item_opened",
                                team_id=team_id,
                                item=item,
                                attention_item_id=item_id,
                            )
                        )

                if dedupe_keys:
                    placeholders = ",".join("?" for _ in dedupe_keys)
                    suffix = f"AND dedupe_key NOT IN ({placeholders})"
                    suffix_params: list[Any] = [*dedupe_keys]
                else:
                    suffix = ""
                    suffix_params = []
                async with db.execute(
                    "SELECT attention_item_id, team_id, user_id, workspace_id, "
                    "policy_key, dedupe_key, severity, payload_json "
                    "FROM credit_attention_state "
                    "WHERE team_id = ? AND state = 'open' "
                    f"{suffix}",
                    [team_id, *suffix_params],
                ) as cur:
                    resolved_rows = await cur.fetchall()
                events.extend(
                    _credit_attention_event_from_row(
                        "credit.attention.resolved",
                        row,
                        resolved_reason="materializer_resolved",
                        previous_severity=row["severity"],
                    )
                    for row in resolved_rows
                )
                async with db.execute(
                    "SELECT attention_item_id, team_id, user_id, workspace_id, "
                    "policy_key, dedupe_key, severity, acted_at, protected_at, payload_json "
                    "FROM credit_attention_state "
                    "WHERE team_id = ? AND state = 'acted' "
                    f"{suffix}",
                    [team_id, *suffix_params],
                ) as cur:
                    stale_acted_rows = await cur.fetchall()
                protected_rows: list[tuple[Any, dict[str, Any]]] = []
                resolved_acted_rows: list[Any] = []
                for row in stale_acted_rows:
                    if not self._credit_attention_verification_window_elapsed(
                        row,
                        now_ts=now_ts,
                    ):
                        continue
                    snapshot = await self._credit_attention_protection_snapshot(
                        db,
                        row,
                        now_ts=now_ts,
                    )
                    if snapshot is None:
                        resolved_acted_rows.append(row)
                        events.append(
                            _credit_attention_event_from_row(
                                "credit.attention.resolved",
                                row,
                                resolved_reason="materializer_resolved_unprotected",
                                previous_severity=row["severity"],
                            )
                        )
                    else:
                        protected_rows.append((row, snapshot))
                        events.append(
                            _credit_attention_event_from_row(
                                "credit.attention.protected",
                                row,
                                protection_reason="materializer_protected",
                                protected_at=now_ts,
                            )
                        )
                await db.execute(
                    "UPDATE credit_attention_state SET "
                    "state = 'resolved', state_reason = 'materializer_resolved', "
                    "resolved_at = ?, updated_at = ? "
                    "WHERE team_id = ? AND state = 'open' "
                    f"{suffix}",
                    [now_ts, now_ts, team_id, *suffix_params],
                )
                for row, snapshot in protected_rows:
                    await db.execute(
                        "UPDATE credit_attention_state SET "
                        "state = 'protected', state_reason = 'materializer_protected', "
                        "protected_at = COALESCE(protected_at, ?), "
                        "protection_reason = 'materializer_protected', "
                        "protection_snapshot_json = ?, "
                        "resolved_at = ?, updated_at = ? "
                        "WHERE team_id = ? AND state = 'acted' AND attention_item_id = ?",
                        [
                            now_ts,
                            json.dumps(snapshot, ensure_ascii=False),
                            now_ts,
                            now_ts,
                            team_id,
                            row["attention_item_id"],
                        ],
                    )
                for row in resolved_acted_rows:
                    await db.execute(
                        "UPDATE credit_attention_state SET "
                        "state = 'resolved', state_reason = 'materializer_resolved_unprotected', "
                        "resolved_at = ?, updated_at = ? "
                        "WHERE team_id = ? AND state = 'acted' AND attention_item_id = ?",
                        [now_ts, now_ts, team_id, row["attention_item_id"]],
                    )
                await db.commit()
            except Exception:
                await db.rollback()
                raise
        return events

    def _credit_attention_verification_window_elapsed(
        self,
        row: Any,
        *,
        now_ts: str,
    ) -> bool:
        acted_at = _parse_iso_datetime(_row_value(row, "acted_at"))
        now_at = _parse_iso_datetime(now_ts)
        if acted_at is None or now_at is None:
            return False
        return now_at - acted_at >= timedelta(days=_CREDIT_ATTENTION_VERIFICATION_WINDOW_DAYS)

    async def _credit_attention_protection_snapshot(
        self,
        db: aiosqlite.Connection,
        row: Any,
        *,
        now_ts: str,
    ) -> dict[str, Any] | None:
        """Return guard evidence only when an acted item is truly protected."""

        acted_at = _row_value(row, "acted_at") or now_ts
        payload = _json_dict(_row_value(row, "payload_json"))
        subject_type = payload.get("subject_type")
        team_id = _row_value(row, "team_id")
        user_id = _row_value(row, "user_id")

        if subject_type == "team" or user_id is None:
            async with db.execute(
                "SELECT team_credit_balance, team_credit_quota FROM teams WHERE id = ?",
                (team_id,),
            ) as cur:
                team = await cur.fetchone()
            if team is None or int(team["team_credit_quota"] or 0) <= 0:
                return None
            pool_pct = max(
                0.0,
                int(team["team_credit_balance"] or 0) / int(team["team_credit_quota"]),
            )
            async with db.execute(
                "SELECT COUNT(*) AS n FROM credit_exhaustion_events "
                "WHERE team_id = ? AND code = 'team_pool_exhausted' "
                "AND datetime(created_at) >= datetime(?)",
                (team_id, acted_at),
            ) as cur:
                blocks = await cur.fetchone()
            hard_blocks = int(blocks["n"] if blocks else 0)
            guard_reasons = []
            if pool_pct >= _CREDIT_ATTENTION_PROTECTED_MIN_BALANCE_PCT:
                guard_reasons.append("team_pool_balance")
            if guard_reasons and hard_blocks == 0:
                guard_reasons.append("team_pool_no_hard_block")
                return {
                    "guard": "team_pool",
                    "guard_reasons": guard_reasons,
                    "team_pool_pct": pool_pct,
                    "team_credit_balance": int(team["team_credit_balance"] or 0),
                    "team_credit_quota": int(team["team_credit_quota"] or 0),
                    "hard_blocks_since_action": hard_blocks,
                }
            return None

        async with db.execute(
            "SELECT credit_balance, credit_quota, status FROM users WHERE id = ? AND team_id = ?",
            (user_id, team_id),
        ) as cur:
            user = await cur.fetchone()
        if user is None or user["status"] != "active" or int(user["credit_quota"] or 0) <= 0:
            return None
        balance = int(user["credit_balance"] or 0)
        quota = int(user["credit_quota"])
        balance_pct = max(0.0, balance / quota)
        async with db.execute(
            "SELECT COALESCE(SUM(-delta), 0) AS spend_7d "
            "FROM credit_ledger "
            "WHERE team_id = ? AND user_id = ? "
            "AND datetime(created_at) >= datetime('now', '-7 days') "
            "AND delta < 0 AND reason = 'turn'",
            (team_id, user_id),
        ) as cur:
            spend = await cur.fetchone()
        spend_7d = int(spend["spend_7d"] if spend else 0)
        projected_runway_days: float | None = None
        if spend_7d > 0:
            projected_runway_days = balance / (spend_7d / 7.0)
        runway_ok = projected_runway_days is not None and projected_runway_days >= _CREDIT_ATTENTION_PROTECTED_MIN_RUNWAY_DAYS
        async with db.execute(
            "SELECT COUNT(*) AS n FROM credit_exhaustion_events "
            "WHERE team_id = ? AND user_id = ? "
            "AND code = 'user_credit_exhausted' "
            "AND datetime(created_at) >= datetime(?)",
            (team_id, user_id, acted_at),
        ) as cur:
            blocks = await cur.fetchone()
        hard_blocks = int(blocks["n"] if blocks else 0)
        guard_reasons = []
        if balance_pct >= _CREDIT_ATTENTION_PROTECTED_MIN_BALANCE_PCT:
            guard_reasons.append("user_balance")
        if runway_ok:
            guard_reasons.append("user_runway")
        if guard_reasons and hard_blocks == 0:
            guard_reasons.append("user_no_hard_block")
            return {
                "guard": "user_balance_runway",
                "guard_reasons": guard_reasons,
                "balance_pct": balance_pct,
                "credit_balance": balance,
                "credit_quota": quota,
                "spend_7d": spend_7d,
                "projected_runway_days": projected_runway_days,
                "hard_blocks_since_action": hard_blocks,
            }
        return None

    async def _track_credit_attention_events(
        self,
        metrics_log: Any | None,
        events: list[dict[str, Any]],
    ) -> None:
        if metrics_log is None:
            return
        for event in events:
            await metrics_log.track(
                event["name"],
                user_id=event.get("user_id"),
                attrs=event.get("attrs") or {},
            )

    async def list_credit_attention_items(
        self,
        team_id: str,
        *,
        limit: int = 20,
        state: str = "open",
        mark_shown: bool = True,
    ) -> list[dict[str, Any]]:
        capped_limit = max(1, min(int(limit), 100))
        where = "team_id = ?"
        params: list[Any] = [team_id]
        if state != "all":
            where += " AND state = ?"
            params.append(state)

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT attention_item_id, team_id, user_id, workspace_id, policy_key, "
                "dedupe_key, state, severity, severity_rank, opened_at, expires_at, updated_at, "
                "resolved_at, first_shown_at, last_shown_at, shown_count, payload_json "
                f"FROM credit_attention_state WHERE {where} "
                "ORDER BY severity_rank DESC, updated_at DESC LIMIT ?",
                [*params, capped_limit],
            ) as cur:
                rows = await cur.fetchall()
            shown_ids = [row["attention_item_id"] for row in rows if row["state"] == "open"]
            if mark_shown and shown_ids:
                await self.mark_credit_attention_items_shown(team_id, shown_ids, conn=db)

        items: list[dict[str, Any]] = []
        for row in rows:
            try:
                payload = json.loads(row["payload_json"] or "{}")
            except (json.JSONDecodeError, TypeError):
                payload = {}
            payload.update(
                {
                    "attention_item_id": row["attention_item_id"],
                    "team_id": row["team_id"],
                    "user_id": row["user_id"],
                    "workspace_id": row["workspace_id"],
                    "policy_key": row["policy_key"],
                    "dedupe_key": row["dedupe_key"],
                    "state": row["state"],
                    "severity": row["severity"],
                    "severity_rank": int(row["severity_rank"]),
                    "opened_at": row["opened_at"],
                    "expires_at": row["expires_at"],
                    "updated_at": row["updated_at"],
                    "resolved_at": row["resolved_at"],
                    "first_shown_at": row["first_shown_at"],
                    "last_shown_at": row["last_shown_at"],
                    "shown_count": int(row["shown_count"] or 0),
                }
            )
            items.append(payload)
        return items

    async def mark_credit_attention_items_shown(
        self,
        team_id: str,
        attention_item_ids: list[str],
        *,
        conn: aiosqlite.Connection | None = None,
    ) -> None:
        """Record that specific open attention items were returned to an admin surface."""

        ids = sorted({item_id for item_id in attention_item_ids if item_id})
        if not ids:
            return
        now_ts = _now()
        placeholders = ",".join("?" for _ in ids)
        sql = (
            "UPDATE credit_attention_state SET "
            "first_shown_at = COALESCE(first_shown_at, ?), "
            "last_shown_at = ?, shown_count = shown_count + 1 "
            f"WHERE team_id = ? AND state = 'open' AND attention_item_id IN ({placeholders})"
        )
        params = [now_ts, now_ts, team_id, *ids]
        if conn is not None:
            await conn.execute(sql, params)
            await conn.commit()
            return
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(sql, params)
            await db.commit()

    async def update_credit_attention_state(
        self,
        *,
        team_id: str,
        attention_item_id: str,
        state: str,
        actor_user_id: str,
        reason: str | None = None,
        protection_snapshot: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Move an attention item into an explicit operator state."""

        if state != "dismissed":
            raise ValueError(f"invalid_attention_state:{state}")
        if reason not in _CREDIT_ATTENTION_DISMISS_REASONS:
            item = await self.get_credit_attention_item(team_id, attention_item_id)
            if item is not None and item.get("state") == "dismissed":
                return item
            raise ValueError("dismiss_reason_required")

        now_ts = _now()
        assignments = [
            "state = ?",
            "state_reason = ?",
            "updated_at = ?",
        ]
        params: list[Any] = [state, reason, now_ts]
        dismiss_expires_at = (datetime.now(UTC) + timedelta(days=_CREDIT_ATTENTION_DISMISS_TTL_DAYS)).isoformat()
        assignments.extend(["dismissed_by = ?", "dismissed_at = ?", "expires_at = ?"])
        params.extend([actor_user_id, now_ts, dismiss_expires_at])

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                f"UPDATE credit_attention_state SET {', '.join(assignments)} WHERE team_id = ? AND attention_item_id = ? AND state = 'open'",
                [*params, team_id, attention_item_id],
            )
            if cur.rowcount == 0:
                await db.rollback()
                if state == "dismissed":
                    item = await self.get_credit_attention_item(team_id, attention_item_id)
                    if item is not None and item.get("state") == "dismissed":
                        return item
                raise ValueError("attention_item_not_found")
            await db.commit()

        item = await self.get_credit_attention_item(team_id, attention_item_id)
        if item is None:
            raise ValueError("attention_item_not_found")
        return item

    async def mark_credit_attention_acted(
        self,
        *,
        team_id: str,
        actor_user_id: str,
        action_type: str,
        user_id: str | None = None,
        policy_keys: list[str] | None = None,
        reason: str | None = None,
        linked_event_id: str | None = None,
    ) -> dict[str, Any]:
        """Mark currently open attention items covered by a successful admin action."""

        now_ts = _now()
        where = "team_id = ? AND state = 'open'"
        params: list[Any] = [team_id]
        if user_id is not None:
            where += " AND user_id = ?"
            params.append(user_id)

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                f"SELECT attention_item_id, user_id, policy_key, payload_json FROM credit_attention_state WHERE {where}",
                params,
            ) as cur:
                rows = await cur.fetchall()

            policy_filter = set(policy_keys or [])
            matched_ids: list[str] = []
            for row in rows:
                if policy_filter and row["policy_key"] not in policy_filter:
                    continue
                try:
                    payload = json.loads(row["payload_json"] or "{}")
                except (json.JSONDecodeError, TypeError):
                    payload = {}
                recommended_action = payload.get("recommended_action_key")
                policy_key = str(row["policy_key"])
                action_policy_matches = _CREDIT_ATTENTION_ACTED_POLICY_MATCHES.get(
                    action_type,
                    set(),
                )
                is_user_credit_action = user_id is not None and (recommended_action == action_type or policy_key in action_policy_matches)
                is_team_quota_action = action_type == "increase_team_quota" and (
                    row["policy_key"] in {"team_pool_critical", "team_pool_warning"} or recommended_action == "increase_team_quota"
                )
                is_period_reset_action = action_type == "period_reset" and row["user_id"] is not None
                if recommended_action == action_type or is_user_credit_action or is_team_quota_action or is_period_reset_action:
                    matched_ids.append(row["attention_item_id"])

            if not matched_ids:
                return {"acted_count": 0, "attention_item_ids": []}

            placeholders = ",".join("?" for _ in matched_ids)
            state_reason = reason or f"admin_action:{action_type}"
            await db.execute(
                "UPDATE credit_attention_state SET "
                "state = 'acted', state_reason = ?, acted_by = ?, acted_at = ?, "
                "linked_event_type = ?, linked_event_id = ?, updated_at = ? "
                f"WHERE attention_item_id IN ({placeholders})",
                [
                    state_reason,
                    actor_user_id,
                    now_ts,
                    action_type,
                    linked_event_id,
                    now_ts,
                    *matched_ids,
                ],
            )
            await db.commit()
        return {"acted_count": len(matched_ids), "attention_item_ids": matched_ids}

    async def get_credit_attention_item(
        self,
        team_id: str,
        attention_item_id: str,
    ) -> dict[str, Any] | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT attention_item_id, team_id, user_id, workspace_id, policy_key, "
                "dedupe_key, state, severity, severity_rank, opened_at, expires_at, updated_at, "
                "resolved_at, payload_json "
                "FROM credit_attention_state "
                "WHERE team_id = ? AND attention_item_id = ?",
                (team_id, attention_item_id),
            ) as cur:
                row = await cur.fetchone()
        if row is None:
            return None
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except (json.JSONDecodeError, TypeError):
            payload = {}
        payload.update(
            {
                "attention_item_id": row["attention_item_id"],
                "team_id": row["team_id"],
                "user_id": row["user_id"],
                "workspace_id": row["workspace_id"],
                "policy_key": row["policy_key"],
                "dedupe_key": row["dedupe_key"],
                "state": row["state"],
                "severity": row["severity"],
                "severity_rank": int(row["severity_rank"]),
                "opened_at": row["opened_at"],
                "expires_at": row["expires_at"],
                "updated_at": row["updated_at"],
                "resolved_at": row["resolved_at"],
            }
        )
        return payload

    async def credits_summary(
        self,
        team_id: str,
        *,
        mark_attention_shown: bool = False,
    ) -> dict[str, Any]:
        """Team credit 总览, 5 段聚合 query (last 30d 时间窗固定).

        返:
          {
            team: {balance, quota, period_start, last_reset_at},
            last_30d: {spend, turns, tokens_in, tokens_out},
            active_users_7d, total_users_active,
            view_attention: [...top 5...],
            view_top_users: [...top 5...],
            view_top_workspaces: [...top 5...],
          }

        spend = SUM(-delta) WHERE delta < 0 AND reason='turn' AND created_at >= now-30d.
        post-flight 扣点无 refund, 总扣点 = 净消耗.
        """
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row

            # 0. team meta
            async with db.execute(
                "SELECT team_credit_balance, team_credit_quota, team_credit_period_start FROM teams WHERE id = ?",
                (team_id,),
            ) as cur:
                team_row = await cur.fetchone()
            if not team_row:
                # Team absent (cross-team admin access); upstream should return 403, but fall back to empty data.
                return {
                    "team": {"balance": 0, "quota": 0, "period_start": None, "last_reset_at": None},
                    "last_30d": {"spend": 0, "turns": 0, "tokens_in": 0, "tokens_out": 0},
                    "active_users_7d": 0,
                    "total_users_active": 0,
                    "view_attention": [],
                    "view_top_users": [],
                    "view_top_workspaces": [],
                    "activation_attention": [],
                }

            # 1. 30d spend / turns / tokens
            # kb-matrix M2 §8④: expand reason to kb_matrix_run, including matrix spend in team 30-day totals
            # (space-admin persona). Recorded side effect: turns = COUNT(DISTINCT turn_id) includes
            # mxrun_ in turn counts (decision 8 cost: display-definition discrepancy).
            async with db.execute(
                "SELECT COALESCE(SUM(-delta), 0) AS spend, "
                "COALESCE(COUNT(DISTINCT turn_id), 0) AS turns, "
                "COALESCE(SUM(tokens_input), 0) AS tokens_in, "
                "COALESCE(SUM(tokens_output), 0) AS tokens_out "
                "FROM credit_ledger "
                "WHERE team_id = ? AND created_at >= datetime('now', '-30 days') "
                "AND delta < 0 AND reason IN ('turn', 'kb_matrix_run')",
                (team_id,),
            ) as cur:
                row_30d = await cur.fetchone()

            # 2. active_users_7d
            async with db.execute(
                "SELECT COUNT(DISTINCT user_id) AS n "
                "FROM credit_ledger "
                "WHERE team_id = ? AND created_at >= datetime('now', '-7 days') "
                "AND reason IN ('turn', 'kb_matrix_run')",
                (team_id,),
            ) as cur:
                row_active = await cur.fetchone()

            # 3. total_users_active (status='active') — shared seat-count helper (licensing §4.1)
            total_active = await self.count_active_seats(team_id, conn=db)

            # 4. top_users (last 30d)
            async with db.execute(
                "SELECT l.user_id, u.email, COALESCE(u.display_name, '') AS display_name, "
                "SUM(-l.delta) AS spend "
                "FROM credit_ledger l JOIN users u ON u.id = l.user_id "
                "WHERE l.team_id = ? AND l.created_at >= datetime('now', '-30 days') "
                "AND l.delta < 0 AND l.reason IN ('turn', 'kb_matrix_run') "
                "GROUP BY l.user_id, u.email, u.display_name "
                "ORDER BY spend DESC LIMIT 5",
                (team_id,),
            ) as cur:
                top_users_rows = await cur.fetchall()

            # 5. top_workspaces (last 30d)
            async with db.execute(
                "SELECT COALESCE(workspace_id, '_no_ws_') AS ws, SUM(-delta) AS spend "
                "FROM credit_ledger "
                "WHERE team_id = ? AND created_at >= datetime('now', '-30 days') "
                "AND delta < 0 AND reason IN ('turn', 'kb_matrix_run') "
                "GROUP BY ws ORDER BY spend DESC LIMIT 5",
                (team_id,),
            ) as cur:
                top_ws_rows = await cur.fetchall()

            # 6. attention_users (low balance / fast growth / new users)
            async with db.execute(
                "WITH user_30d AS ("
                "  SELECT user_id, SUM(-delta) AS spend_30d FROM credit_ledger "
                "  WHERE team_id = ? AND created_at >= datetime('now', '-30 days') "
                "  AND delta < 0 AND reason = 'turn' GROUP BY user_id"
                "), user_7d AS ("
                "  SELECT user_id, SUM(-delta) AS spend_7d FROM credit_ledger "
                "  WHERE team_id = ? AND created_at >= datetime('now', '-7 days') "
                "  AND delta < 0 AND reason = 'turn' GROUP BY user_id"
                "), first_activity AS ("
                "  SELECT user_id, MIN(created_at) AS first_activity_at FROM credit_ledger "
                "  WHERE team_id = ? AND delta < 0 AND reason = 'turn' "
                "  AND tokens_input + tokens_output >= 500 GROUP BY user_id"
                ") "
                "SELECT u.id AS user_id, u.email, "
                "  CAST(u.credit_balance AS REAL) / "
                "    CASE WHEN u.credit_quota > 0 THEN u.credit_quota ELSE 1 END AS balance_pct, "
                "  COALESCE(d7.spend_7d, 0) * 30.0 / 7.0 / "
                "    CASE WHEN COALESCE(d30.spend_30d, 0) > 0 THEN d30.spend_30d ELSE 1 END "
                "    AS growth_ratio, "
                "  CASE WHEN datetime(u.created_at) >= datetime('now', '-30 days') "
                "    OR datetime(fa.first_activity_at) >= datetime('now', '-30 days') THEN 1 "
                "    ELSE 0 END AS is_new "
                "FROM users u "
                "LEFT JOIN user_7d d7 ON d7.user_id = u.id "
                "LEFT JOIN user_30d d30 ON d30.user_id = u.id "
                "LEFT JOIN first_activity fa ON fa.user_id = u.id "
                "WHERE u.team_id = ? AND u.status = 'active' "
                "  AND ("
                "    (CAST(u.credit_balance AS REAL) / "
                "       CASE WHEN u.credit_quota > 0 THEN u.credit_quota ELSE 1 END) < 0.20 "
                "    OR (COALESCE(d7.spend_7d, 0) * 30.0 / 7.0 / "
                "        CASE WHEN COALESCE(d30.spend_30d, 0) > 0 THEN d30.spend_30d ELSE 1 END) > 1.5 "
                "    OR datetime(u.created_at) >= datetime('now', '-30 days')"
                "    OR datetime(fa.first_activity_at) >= datetime('now', '-30 days')"
                "  ) "
                "ORDER BY balance_pct ASC LIMIT 5",
                (team_id, team_id, team_id, team_id),
            ) as cur:
                attention_rows = await cur.fetchall()

            # 7. last_reset_at (latest period_reset event)
            async with db.execute(
                "SELECT created_at FROM user_audit_log "
                "WHERE team_id = ? AND event_type = 'credit_admin_period_reset' "
                "ORDER BY created_at DESC LIMIT 1",
                (team_id,),
            ) as cur:
                reset_row = await cur.fetchone()

        activation_attention = await self.list_credit_attention_items(
            team_id,
            limit=5,
            mark_shown=mark_attention_shown,
        )

        def _attention_flags(r: aiosqlite.Row) -> list[str]:
            flags = []
            if (r["balance_pct"] or 0) < 0.20:
                flags.append("low_balance")
            if (r["growth_ratio"] or 0) > 1.5:
                flags.append("growth_spike")
            if r["is_new"]:
                flags.append("new_user")
            return flags

        return {
            "team": {
                "balance": int(team_row["team_credit_balance"]),
                "quota": int(team_row["team_credit_quota"]),
                "period_start": team_row["team_credit_period_start"],
                "last_reset_at": reset_row["created_at"] if reset_row else None,
            },
            "last_30d": {
                "spend": int(row_30d["spend"]),
                "turns": int(row_30d["turns"]),
                "tokens_in": int(row_30d["tokens_in"]),
                "tokens_out": int(row_30d["tokens_out"]),
            },
            "active_users_7d": int(row_active["n"] or 0),
            "total_users_active": total_active,
            "view_attention": [
                {
                    "user_id": r["user_id"],
                    "email": r["email"],
                    "balance_pct": float(r["balance_pct"] or 0),
                    "growth_7d": float(r["growth_ratio"] or 0),
                    "flags": _attention_flags(r),
                }
                for r in attention_rows
            ],
            "view_top_users": [
                {
                    "user_id": r["user_id"],
                    "email": r["email"],
                    "display_name": r["display_name"],
                    "spend": int(r["spend"]),
                }
                for r in top_users_rows
            ],
            "view_top_workspaces": [
                {
                    "workspace_id": r["ws"] if r["ws"] != "_no_ws_" else None,
                    "spend": int(r["spend"]),
                }
                for r in top_ws_rows
            ],
            "activation_attention": activation_attention,
        }

    async def credits_users_list(
        self,
        team_id: str,
        *,
        page: int = 1,
        size: int = 20,
        sort: str = "attention",
        q: str | None = None,
        tier: str | None = None,
        workspace_id: str | None = None,
        include_removed: bool = False,
        user_id: str | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        """分页用户列表 with last 30d spend + attention score.

        排序选项:
          - "attention" (default): 综合分 (低余额 + 高增速 + new_user)
          - "spend": 近 30d 消耗 desc
          - "balance_pct": 余额% asc (低优先)
          - "email": email asc
          - "last_activity": 最近活跃 desc

        sort 字段必须在白名单内 (防 sql injection).
        """
        ALLOWED_SORTS = {
            "attention": "COALESCE(ca.severity_rank, 0) DESC, attention_score DESC, balance_pct ASC",
            "spend": "spend_30d DESC",
            "balance_pct": "balance_pct ASC",
            "email": "u.email ASC",
            "last_activity": "last_activity_at DESC",
        }
        order_by = ALLOWED_SORTS.get(sort, ALLOWED_SORTS["attention"])

        # Build WHERE clause + parameter list.
        where_parts = ["u.team_id = ?"]
        params: list[Any] = [team_id]

        if not include_removed:
            where_parts.append("u.status = 'active'")
        if tier in ("pro", "ultra"):
            where_parts.append("u.tier = ?")
            params.append(tier)
        if q:
            where_parts.append("(u.email LIKE ? OR u.display_name LIKE ?)")
            params.extend([f"%{q}%", f"%{q}%"])
        if user_id:
            where_parts.append("u.id = ?")
            params.append(user_id)
        if workspace_id:
            where_parts.append(
                "EXISTS (SELECT 1 FROM credit_ledger cl "
                "WHERE cl.user_id = u.id AND cl.workspace_id = ? "
                "AND cl.created_at >= datetime('now', '-30 days'))"
            )
            params.append(workspace_id)
        where_sql = " AND ".join(where_parts)

        # Main query: users + 30-day spend + last_activity + attention_score
        main_sql = f"""
            WITH user_30d AS (
              SELECT user_id, SUM(-delta) AS spend_30d, MAX(created_at) AS last_activity
              FROM credit_ledger
              WHERE team_id = ? AND created_at >= datetime('now', '-30 days')
                AND delta < 0 AND reason = 'turn'
              GROUP BY user_id
            ), user_7d AS (
              SELECT user_id, SUM(-delta) AS spend_7d
              FROM credit_ledger
              WHERE team_id = ? AND created_at >= datetime('now', '-7 days')
                AND delta < 0 AND reason = 'turn'
              GROUP BY user_id
            ), first_activity AS (
              SELECT user_id, MIN(created_at) AS first_activity_at
              FROM credit_ledger
              WHERE team_id = ? AND delta < 0 AND reason = 'turn'
                AND tokens_input + tokens_output >= 500
              GROUP BY user_id
            ), current_attention AS (
              SELECT * FROM (
                SELECT attention_item_id, user_id, workspace_id, policy_key,
                       state, severity, severity_rank, opened_at, updated_at,
                       expires_at, resolved_at, payload_json,
                       ROW_NUMBER() OVER (
                         PARTITION BY user_id
                         ORDER BY severity_rank DESC, updated_at DESC
                       ) AS rn
                FROM credit_attention_state
                WHERE team_id = ? AND state = 'open' AND user_id IS NOT NULL
              )
              WHERE rn = 1
            )
            SELECT
              u.id AS user_id, u.email, u.display_name, u.tier, u.status,
              u.credit_balance, u.credit_quota, u.credit_period_start,
              COALESCE(d30.spend_30d, 0) AS spend_30d,
              d30.last_activity AS last_activity_at,
              CASE WHEN o.created_by_user_id = u.id THEN 1 ELSE 0 END AS is_team_admin,
              CAST(u.credit_balance AS REAL) /
                CASE WHEN u.credit_quota > 0 THEN u.credit_quota ELSE 1 END AS balance_pct,
              ((CASE WHEN (CAST(u.credit_balance AS REAL) /
                CASE WHEN u.credit_quota > 0 THEN u.credit_quota ELSE 1 END) < 0.20
                  THEN 1 ELSE 0 END)
              + (CASE WHEN COALESCE(d7.spend_7d, 0) * 30.0 / 7.0 /
                  CASE WHEN COALESCE(d30.spend_30d, 0) > 0 THEN d30.spend_30d ELSE 1 END > 1.5
                  THEN 1 ELSE 0 END)
              + (CASE WHEN datetime(u.created_at) >= datetime('now', '-30 days')
                  OR datetime(fa.first_activity_at) >= datetime('now', '-30 days')
                  THEN 1 ELSE 0 END)) AS attention_score,
              ca.attention_item_id AS activation_attention_item_id,
              ca.workspace_id AS activation_workspace_id,
              ca.policy_key AS activation_policy_key,
              ca.state AS activation_state,
              ca.severity AS activation_severity,
              ca.severity_rank AS activation_severity_rank,
              ca.opened_at AS activation_opened_at,
              ca.expires_at AS activation_expires_at,
              ca.updated_at AS activation_updated_at,
              ca.resolved_at AS activation_resolved_at,
              ca.payload_json AS activation_payload_json
            FROM users u
            JOIN teams o ON o.id = u.team_id
            LEFT JOIN user_30d d30 ON d30.user_id = u.id
            LEFT JOIN user_7d d7 ON d7.user_id = u.id
            LEFT JOIN first_activity fa ON fa.user_id = u.id
            LEFT JOIN current_attention ca ON ca.user_id = u.id
            WHERE {where_sql}
            ORDER BY {order_by}
            LIMIT ? OFFSET ?
        """
        # Prepend team_id for CTE.
        cte_params: list[Any] = [team_id, team_id, team_id, team_id]
        page_params = [size, max(0, (page - 1) * size)]
        all_params = cte_params + params + page_params

        count_sql = f"SELECT COUNT(*) AS n FROM users u WHERE {where_sql}"

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(main_sql, all_params) as cur:
                rows = await cur.fetchall()
            async with db.execute(count_sql, params) as cur:
                count_row = await cur.fetchone()

            items = []
            for r in rows:
                activation_attention = None
                if r["activation_attention_item_id"]:
                    try:
                        activation_payload = json.loads(r["activation_payload_json"] or "{}")
                    except (json.JSONDecodeError, TypeError):
                        activation_payload = {}
                    activation_payload.update(
                        {
                            "attention_item_id": r["activation_attention_item_id"],
                            "team_id": team_id,
                            "user_id": r["user_id"],
                            "workspace_id": r["activation_workspace_id"],
                            "policy_key": r["activation_policy_key"],
                            "state": r["activation_state"],
                            "severity": r["activation_severity"],
                            "severity_rank": int(r["activation_severity_rank"] or 0),
                            "opened_at": r["activation_opened_at"],
                            "expires_at": r["activation_expires_at"],
                            "updated_at": r["activation_updated_at"],
                            "resolved_at": r["activation_resolved_at"],
                        }
                    )
                    activation_attention = activation_payload
                items.append(
                    {
                        "user_id": r["user_id"],
                        "email": r["email"],
                        "display_name": r["display_name"],
                        "tier": r["tier"],
                        "status": r["status"],
                        "credit_balance": int(r["credit_balance"]),
                        "credit_quota": int(r["credit_quota"]),
                        "credit_period_start": r["credit_period_start"],
                        "spend_30d": int(r["spend_30d"]),
                        "last_activity_at": r["last_activity_at"],
                        "is_team_admin": bool(r["is_team_admin"]),
                        "attention_score": float(r["attention_score"] or 0) / 3.0,
                        "activation_attention": activation_attention,
                    }
                )
            total = int(count_row["n"]) if count_row else 0
            return items, total

    # ─── audit log ──────────────────────────────────────

    async def log_audit(
        self,
        *,
        user_id: str | None,
        team_id: str | None,
        event_type: str,
        ip: str | None = None,
        user_agent: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO user_audit_log(id,user_id,team_id,event_type,ip,user_agent,payload_json,created_at) VALUES (?,?,?,?,?,?,?,?)",
                (_id("aud"), user_id, team_id, event_type, ip, user_agent, json.dumps(payload or {}, ensure_ascii=False), _now()),
            )
            await db.commit()


async def _find_workspace_by_datarc_id(
    db: Any,
    *,
    team_id: str,
    datarc_workspace_id: str,
) -> str | None:
    async with db.execute(
        "SELECT id FROM workspaces WHERE team_id = ? AND datarc_workspace_id = ? AND status = 'active' ORDER BY created_at LIMIT 1",
        (team_id, datarc_workspace_id),
    ) as cur:
        row = await cur.fetchone()
    return str(row["id"]) if row else None


async def _table_columns(db: Any, table: str) -> set[str]:
    async with db.execute(f"PRAGMA table_info({table})") as cur:
        rows = await cur.fetchall()
    return {str(row["name"]) for row in rows}


def _row_to_team(row: Any) -> Team:
    try:
        llm_config_version = int(row["llm_config_version"] or 0)
    except Exception:
        llm_config_version = 0
    return Team(
        id=row["id"],
        name=row["name"],
        domain=row["domain"],
        llm_config_encrypted=row["llm_config_encrypted"] or "",
        llm_config_version=llm_config_version,
        max_seats=int(row["max_seats"]),
        default_user_tier=row["default_user_tier"],
        team_credit_balance=int(row["team_credit_balance"]),
        team_credit_quota=int(row["team_credit_quota"]),
        team_credit_period_start=row["team_credit_period_start"],
        tier_credits=json.loads(row["tier_credits_json"] or '{"pro":5000,"ultra":30000}'),
        status=row["status"],
        created_at=row["created_at"],
        contract_expires_at=row["contract_expires_at"],
    )


def _row_to_team_license(row: Any) -> TeamLicense:
    return TeamLicense(
        team_id=row["team_id"],
        license_token=row["license_token"],
        license_id=row["license_id"],
        key_id=row["key_id"],
        issued_at=row["issued_at"],
        activated_at=row["activated_at"],
        expires_at=row["expires_at"],
        grace_period_days=int(row["grace_period_days"]),
        max_seats=int(row["max_seats"]),
        default_user_tier=row["default_user_tier"],
        tier_credits=json.loads(row["tier_credits_json"] or "{}"),
        features=json.loads(row["features_json"] or "[]"),
        deployment_mode=row["deployment_mode"],
        max_offline_lifetime_days=(int(row["max_offline_lifetime_days"]) if row["max_offline_lifetime_days"] is not None else None),
        last_verified_at=row["last_verified_at"],
        last_good_health=row["last_good_health"],
        instance_fingerprint=row["instance_fingerprint"],
    )


def _row_to_user(row: Any) -> User:
    # Sprint β: defensive .get() for new cols — old rows may lack them
    def _get(key: str, default: Any = None) -> Any:
        try:
            return row[key]
        except (KeyError, IndexError):
            return default

    return User(
        id=row["id"],
        team_id=row["team_id"],
        email=row["email"],
        email_verified=bool(row["email_verified"]),
        display_name=row["display_name"],
        avatar_url=row["avatar_url"],
        password_hash=row["password_hash"],
        oauth_providers=json.loads(row["oauth_providers_json"] or "[]"),
        tier=row["tier"],
        credit_balance=int(row["credit_balance"]),
        credit_quota=int(row["credit_quota"]),
        credit_period_start=row["credit_period_start"],
        locale=row["locale"],
        timezone=row["timezone"],
        preferences=json.loads(row["preferences_json"] or "{}"),
        status=row["status"],
        created_at=row["created_at"],
        last_active_at=row["last_active_at"],
        # Sprint β G10 fields
        pwd_version=int(_get("pwd_version", 0) or 0),
        session_token_version=int(_get("session_token_version", 0) or 0),
        last_password_change_at=_get("last_password_change_at"),
        active_team_id=_get("active_team_id"),
    )
