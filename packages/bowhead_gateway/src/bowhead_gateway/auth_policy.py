"""Deployment-level account creation and email verification policy.

The policy is deliberately resolved on the server. Browser clients consume the
derived capability document and never submit policy values back to mutations.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import StrEnum

from bowhead_core.email_config import env_smtp_config


class AccountCreationPolicy(StrEnum):
    OPEN = "open"
    INVITE_ONLY = "invite_only"
    ADMIN_ONLY = "admin_only"


class EmailVerificationPolicy(StrEnum):
    NOT_REQUIRED = "not_required"
    SMTP_REQUIRED = "smtp_required"
    TRUSTED_IDP_REQUIRED = "trusted_idp_required"


_OAUTH_PROVIDER_CREDENTIALS: tuple[tuple[str, str, str], ...] = (("feishu", "BOWHEAD_OAUTH_FEISHU_APP_ID", "BOWHEAD_OAUTH_FEISHU_APP_SECRET"),)


def configured_oauth_providers() -> tuple[str, ...]:
    """Return only OAuth providers whose complete client credentials are present."""
    return tuple(
        provider
        for provider, client_id_env, client_secret_env in _OAUTH_PROVIDER_CREDENTIALS
        if (os.environ.get(client_id_env) or "").strip() and (os.environ.get(client_secret_env) or "").strip()
    )


@dataclass(frozen=True)
class AccountPolicy:
    deployment_mode: str
    account_creation: AccountCreationPolicy
    email_verification: EmailVerificationPolicy
    auth_methods: tuple[str, ...]
    bootstrap_required: bool = False

    @property
    def public_registration(self) -> bool:
        return self.account_creation is AccountCreationPolicy.OPEN

    @property
    def invite_activation_enabled(self) -> bool:
        return invite_only_activation_enabled()


def invite_only_activation_enabled() -> bool:
    return _env_bool("FEATURE_INVITE_ONLY_ACTIVATION")


def self_registration_allowed(policy: AccountPolicy, *, has_invite_token: bool) -> bool:
    """`/api/v1/auth/register` 的准入判据。

    - `OPEN`：一律放行。
    - `INVITE_ONLY`：**只放行持邀请的**。一张通过签名、行状态与有效期校验的邀请，
      本身就是这档策略下的授权凭据 —— 否则这个枚举值和 `ADMIN_ONLY` 行为完全一样，
      名字就没有意义了。
    - `ADMIN_ONLY`：一律拒绝（语义就是「只能管理员建号」）。

    ⚠ feedback-triage-2026-08-24 才把 INVITE_ONLY 这档接上。此前 register 路由里
    是一句 `if account_creation is not OPEN: raise 403`，排在 invite_token 校验**前面**，
    于是受邀用户也一并被挡；而同一个函数下面几十行就是完整的 invite_token 入队逻辑，
    上一条守卫也明写着「激活流开着时才拒绝 register+token」——代码明确设想了
    「激活流关着 → 走 register + invite_token」，却被那一行提前挡死。
    116 现网（account_creation=invite_only + FEATURE_INVITE_ONLY_ACTIVATION 未开）
    因此**没有任何一条受邀用户能走通的入口**。
    """
    if policy.account_creation is AccountCreationPolicy.OPEN:
        return True
    if policy.account_creation is AccountCreationPolicy.INVITE_ONLY:
        return has_invite_token
    return False


def resolve_account_policy(
    *,
    bootstrap_required: bool = False,
    deployment_mode_override: str | None = None,
    validate_runtime: bool = False,
) -> AccountPolicy:
    deployment_mode = (deployment_mode_override or os.environ.get("BOWHEAD_DEPLOYMENT_MODE") or "local").strip().lower()
    if deployment_mode not in {"local", "saas"}:
        raise RuntimeError("BOWHEAD_DEPLOYMENT_MODE must be local or saas")

    explicit_account = (os.environ.get("BOWHEAD_ACCOUNT_CREATION_POLICY") or "").strip().lower()
    legacy_registration_disabled = _env_bool("BOWHEAD_REGISTRATION_DISABLED")
    if explicit_account:
        try:
            account_creation = AccountCreationPolicy(explicit_account)
        except ValueError as exc:
            raise RuntimeError(
                "BOWHEAD_ACCOUNT_CREATION_POLICY must be open, invite_only, or admin_only",
            ) from exc
        if legacy_registration_disabled and account_creation is not AccountCreationPolicy.ADMIN_ONLY:
            raise RuntimeError(
                "BOWHEAD_ACCOUNT_CREATION_POLICY conflicts with BOWHEAD_REGISTRATION_DISABLED=1",
            )
    elif legacy_registration_disabled:
        account_creation = AccountCreationPolicy.ADMIN_ONLY
    elif deployment_mode == "local":
        account_creation = AccountCreationPolicy.INVITE_ONLY
    else:
        account_creation = AccountCreationPolicy.OPEN

    explicit_verification = (os.environ.get("BOWHEAD_EMAIL_VERIFICATION_POLICY") or "").strip().lower()
    legacy_verify_required = _env_bool("BOWHEAD_EMAIL_VERIFY_REQUIRED")
    if explicit_verification:
        try:
            email_verification = EmailVerificationPolicy(explicit_verification)
        except ValueError as exc:
            raise RuntimeError(
                "BOWHEAD_EMAIL_VERIFICATION_POLICY must be not_required, smtp_required, or trusted_idp_required",
            ) from exc
        if legacy_verify_required and email_verification is not EmailVerificationPolicy.SMTP_REQUIRED:
            raise RuntimeError(
                "BOWHEAD_EMAIL_VERIFICATION_POLICY conflicts with BOWHEAD_EMAIL_VERIFY_REQUIRED=1",
            )
    elif legacy_verify_required:
        email_verification = EmailVerificationPolicy.SMTP_REQUIRED
    else:
        email_verification = EmailVerificationPolicy.NOT_REQUIRED

    methods = ["password", *configured_oauth_providers()]

    policy = AccountPolicy(
        deployment_mode=deployment_mode,
        account_creation=account_creation,
        email_verification=email_verification,
        auth_methods=tuple(methods),
        bootstrap_required=bootstrap_required,
    )
    if validate_runtime:
        _validate_policy(policy)
    return policy


def _validate_policy(policy: AccountPolicy) -> None:
    if policy.email_verification is EmailVerificationPolicy.SMTP_REQUIRED and env_smtp_config() is None:
        raise RuntimeError(
            "smtp_required identity verification needs instance-level BOWHEAD_SMTP_HOST",
        )
    if policy.email_verification is EmailVerificationPolicy.TRUSTED_IDP_REQUIRED:
        providers = set(policy.auth_methods) - {"password"}
        if not providers:
            raise RuntimeError(
                "trusted_idp_required needs at least one configured OAuth provider",
            )


def _env_bool(name: str) -> bool:
    return (os.environ.get(name) or "0").strip().lower() in {"1", "true", "yes"}
