from __future__ import annotations

import pytest
from bowhead_gateway.auth_policy import (
    AccountCreationPolicy,
    AccountPolicy,
    EmailVerificationPolicy,
    configured_oauth_providers,
    resolve_account_policy,
    self_registration_allowed,
)


def _clear_policy_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "BOWHEAD_ACCOUNT_CREATION_POLICY",
        "BOWHEAD_REGISTRATION_DISABLED",
        "BOWHEAD_EMAIL_VERIFICATION_POLICY",
        "BOWHEAD_EMAIL_VERIFY_REQUIRED",
        "BOWHEAD_SMTP_HOST",
        "BOWHEAD_OAUTH_FEISHU_APP_ID",
        "BOWHEAD_OAUTH_FEISHU_APP_SECRET",
    ):
        monkeypatch.delenv(name, raising=False)


def test_policy_defaults_are_derived_from_deployment_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_policy_env(monkeypatch)
    local = resolve_account_policy(deployment_mode_override="local")
    saas = resolve_account_policy(deployment_mode_override="saas")

    assert local.account_creation is AccountCreationPolicy.INVITE_ONLY
    assert saas.account_creation is AccountCreationPolicy.OPEN
    assert local.email_verification is EmailVerificationPolicy.NOT_REQUIRED


def test_policy_defaults_to_local_deployment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_policy_env(monkeypatch)
    monkeypatch.delenv("BOWHEAD_DEPLOYMENT_MODE", raising=False)

    policy = resolve_account_policy()

    assert policy.deployment_mode == "local"
    assert policy.account_creation is AccountCreationPolicy.INVITE_ONLY


def test_explicit_policy_conflict_fails_loud(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_policy_env(monkeypatch)
    monkeypatch.setenv("BOWHEAD_ACCOUNT_CREATION_POLICY", "open")
    monkeypatch.setenv("BOWHEAD_REGISTRATION_DISABLED", "1")

    with pytest.raises(RuntimeError, match="conflicts"):
        resolve_account_policy()


def test_smtp_required_fails_startup_without_instance_smtp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_policy_env(monkeypatch)
    monkeypatch.setenv("BOWHEAD_EMAIL_VERIFICATION_POLICY", "smtp_required")

    with pytest.raises(RuntimeError, match="BOWHEAD_SMTP_HOST"):
        resolve_account_policy(validate_runtime=True)


def test_trusted_idp_required_needs_a_configured_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_policy_env(monkeypatch)
    monkeypatch.setenv(
        "BOWHEAD_EMAIL_VERIFICATION_POLICY",
        "trusted_idp_required",
    )

    with pytest.raises(RuntimeError, match="OAuth provider"):
        resolve_account_policy(validate_runtime=True)


def test_oauth_provider_requires_complete_client_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_policy_env(monkeypatch)
    monkeypatch.setenv("BOWHEAD_OAUTH_FEISHU_APP_ID", "client-id")
    assert configured_oauth_providers() == ()

    monkeypatch.setenv("BOWHEAD_OAUTH_FEISHU_APP_SECRET", "client-secret")
    assert configured_oauth_providers() == ("feishu",)


# ── feedback-triage-2026-08-24: customer report '邀请用户，用户点开邮件链接，无法进入下一步' ──
#
# Three dead ends overlapped; this covers the final one. Registration previously checked
# if account_creation is not OPEN: raise 403 **before** invite_token validation,
# blocking even valid invitations on INVITE_ONLY deployments. A few dozen lines below in the same function,
# complete invite_token intake existed; the preceding guard explicitly rejects register+token only with activation flow enabled.
# 116 production: account_creation=invite_only + FEATURE_INVITE_ONLY_ACTIVATION unset;
# invitees had no working entry. The customer's invitation still has consumed_at=null.


def _policy(account_creation: AccountCreationPolicy) -> AccountPolicy:
    return AccountPolicy(
        deployment_mode="saas",
        account_creation=account_creation,
        email_verification=EmailVerificationPolicy.NOT_REQUIRED,
        auth_methods=("password",),
    )


def test_open_policy_allows_registration_with_or_without_invite() -> None:
    policy = _policy(AccountCreationPolicy.OPEN)
    assert self_registration_allowed(policy, has_invite_token=False) is True
    assert self_registration_allowed(policy, has_invite_token=True) is True


def test_invite_only_allows_only_with_invite() -> None:
    """★ 这条就是 116 那个死路：invite_only 下持邀请必须放行。"""
    policy = _policy(AccountCreationPolicy.INVITE_ONLY)
    assert self_registration_allowed(policy, has_invite_token=True) is True
    assert self_registration_allowed(policy, has_invite_token=False) is False


def test_admin_only_blocks_even_with_invite() -> None:
    """反向对照：ADMIN_ONLY 的语义就是「只能管理员建号」，邀请也不放行。

    没有这条，上一条很容易被改成「有 token 就放行」而把 ADMIN_ONLY 一起放开。
    """
    policy = _policy(AccountCreationPolicy.ADMIN_ONLY)
    assert self_registration_allowed(policy, has_invite_token=True) is False
    assert self_registration_allowed(policy, has_invite_token=False) is False


def test_invite_only_is_not_the_same_as_admin_only() -> None:
    """★ 修复前这两档行为完全一致，枚举值等于没有意义 —— 钉住它们必须可区分。"""
    invite_only = _policy(AccountCreationPolicy.INVITE_ONLY)
    admin_only = _policy(AccountCreationPolicy.ADMIN_ONLY)
    assert self_registration_allowed(invite_only, has_invite_token=True) != self_registration_allowed(admin_only, has_invite_token=True)
