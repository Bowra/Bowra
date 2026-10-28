"""email-config sprint M1a — core resolver / validation / delivery UT.

设计:docs/sprints/email-config-2026-06-12/tech-design.md §10。
"""

from __future__ import annotations

import pytest
from bowhead_core.email_config import (
    EmailConfigError,
    SmtpConfig,
    deliver_email,
    env_smtp_config,
    resolve_smtp_config,
    validate_smtp_settings,
)

from bowhead_core import email_config as ec

_SMTP_ENV = (
    "BOWHEAD_SMTP_HOST",
    "BOWHEAD_SMTP_PORT",
    "BOWHEAD_SMTP_USER",
    "BOWHEAD_SMTP_PASS",
    "BOWHEAD_SMTP_FROM",
    "BOWHEAD_SMTP_FROM_NAME",
    "BOWHEAD_SMTP_TLS",
)


@pytest.fixture(autouse=True)
def _clear_smtp_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in (*_SMTP_ENV, "BOWHEAD_TEAM_EMAIL_CONFIG"):
        monkeypatch.delenv(k, raising=False)


# ── env_smtp_config ─────────────────────────────────────────────


def test_env_config_none_without_host() -> None:
    assert env_smtp_config() is None


def test_env_config_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOWHEAD_SMTP_HOST", "smtp.x.com")
    monkeypatch.setenv("BOWHEAD_SMTP_PORT", "465")
    cfg = env_smtp_config()
    assert cfg is not None
    assert (cfg.host, cfg.port, cfg.tls, cfg.source) == ("smtp.x.com", 465, True, "env")


def test_env_tls_default_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOWHEAD_SMTP_HOST", "smtp.x.com")
    assert env_smtp_config().tls is True
    monkeypatch.setenv("BOWHEAD_SMTP_TLS", "0")
    assert env_smtp_config().tls is False


# ── resolve_smtp_config priority ────────────────────────────────


class _FakeStore:
    def __init__(self, cfg: dict | None = None, raise_on_read: bool = False) -> None:
        self._cfg = cfg
        self._raise = raise_on_read

    async def get_team_email_config_decrypted(self, team_id: str) -> dict:
        if self._raise:
            raise ValueError("decrypt boom")
        return self._cfg or {}


@pytest.mark.asyncio
async def test_resolve_prefers_team(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOWHEAD_SMTP_HOST", "env.x.com")
    store = _FakeStore({"host": "team.x.com", "port": 587, "from_addr": "a@x.com"})
    cfg = await resolve_smtp_config("team_1", store)
    assert cfg.host == "team.x.com"
    assert cfg.source == "team"


@pytest.mark.asyncio
async def test_resolve_env_fallback_no_team_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOWHEAD_SMTP_HOST", "env.x.com")
    store = _FakeStore({})  # Team has no configuration
    cfg = await resolve_smtp_config("team_1", store)
    assert (cfg.host, cfg.source) == ("env.x.com", "env")


@pytest.mark.asyncio
async def test_resolve_none_when_neither() -> None:
    cfg = await resolve_smtp_config("team_1", _FakeStore({}))
    assert cfg is None


@pytest.mark.asyncio
async def test_resolve_flag_off_skips_team(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOWHEAD_TEAM_EMAIL_CONFIG", "0")
    monkeypatch.setenv("BOWHEAD_SMTP_HOST", "env.x.com")
    store = _FakeStore({"host": "team.x.com"})
    cfg = await resolve_smtp_config("team_1", store)
    assert cfg.host == "env.x.com"  # flag off → ignore team


@pytest.mark.asyncio
async def test_resolve_team_unreadable_lenient_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOWHEAD_SMTP_HOST", "env.x.com")
    cfg = await resolve_smtp_config("team_1", _FakeStore(raise_on_read=True))
    assert cfg.host == "env.x.com"  # strict_team=False → no exception


@pytest.mark.asyncio
async def test_resolve_team_unreadable_strict_raises() -> None:
    with pytest.raises(EmailConfigError):
        await resolve_smtp_config("team_1", _FakeStore(raise_on_read=True), strict_team=True)


@pytest.mark.asyncio
async def test_resolve_no_team_id_returns_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOWHEAD_SMTP_HOST", "env.x.com")
    cfg = await resolve_smtp_config(None, _FakeStore({"host": "team.x.com"}))
    assert cfg.host == "env.x.com"


# ── validate_smtp_settings ──────────────────────────────────────


def _ok_cfg(**over: object) -> dict:
    # Use a public IP literal: ssrf_guard directly checks is_global without getaddrinfo
    # → unit tests need no real DNS (hostname coverage belongs to M4 aiosmtpd integration tests).
    base = {"host": "8.8.8.8", "port": 587, "user": "u", "password": "p", "from_addr": "no-reply@example.com", "tls": True}
    base.update(over)
    return base


def test_validate_ok() -> None:
    assert validate_smtp_settings(_ok_cfg()) == []


def test_validate_nonstandard_port_is_allowed_locally() -> None:
    errs = validate_smtp_settings(_ok_cfg(port=8080))
    assert errs == []


def test_validate_bad_port_is_rejected_in_saas(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOWHEAD_DEPLOYMENT_MODE", "saas")
    assert validate_smtp_settings(_ok_cfg(port=8080)) == ["smtp_port_unsupported"]


def test_validate_port_allowlist_accepts_smtp_ports() -> None:
    for p in (25, 465, 587, 2525):
        assert validate_smtp_settings(_ok_cfg(port=p)) == []


def test_validate_empty_host() -> None:
    assert "smtp_host_required" in validate_smtp_settings(_ok_cfg(host=""))


def test_validate_bad_from() -> None:
    assert "smtp_from_address_invalid" in validate_smtp_settings(_ok_cfg(from_addr="notanemail"))


def test_validate_crlf_injection_rejected() -> None:
    errs = validate_smtp_settings(_ok_cfg(from_name="Evil\r\nBcc: victim@x.com"))
    assert "smtp_control_characters" in errs


def test_validate_host_charset_rejected() -> None:
    errs = validate_smtp_settings(_ok_cfg(host="smtp_under score"))
    assert "smtp_host_invalid" in errs


def test_validate_internal_host_is_allowed() -> None:
    assert validate_smtp_settings(_ok_cfg(host="10.0.0.5")) == []


def test_validate_user_without_password_rejected() -> None:
    # review-fix: login supplied with empty password = omission (otherwise saves succeed but sends become SMTPAuthError dead letters).
    errs = validate_smtp_settings(_ok_cfg(user="bot@x.com", password=""))
    assert errs == ["smtp_password_required"]


def test_validate_no_user_no_password_ok() -> None:
    # Unauthenticated relays (empty user) allow an empty password.
    assert validate_smtp_settings(_ok_cfg(user="", password="")) == []


# ── deliver_email ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_deliver_none_config() -> None:
    ok, err = await deliver_email("a@x.com", "s", "b", config=None)
    assert ok is False and err == "smtp not configured"


@pytest.mark.asyncio
async def test_deliver_success(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {}

    def _fake_detailed(to, subject, body, *, config):
        calls["to"] = to
        calls["host"] = config.host
        return True, None

    monkeypatch.setattr("bowhead_core.email_sender._send_smtp_detailed", _fake_detailed)
    cfg = SmtpConfig(host="smtp.x.com", port=587, user="", password="", from_addr="a@x.com", source="team")
    ok, err = await deliver_email("b@x.com", "s", "body", config=cfg)
    assert ok is True and err is None
    assert calls == {"to": "b@x.com", "host": "smtp.x.com"}


@pytest.mark.asyncio
async def test_deliver_timeout_does_not_block(monkeypatch: pytest.MonkeyPatch) -> None:
    # Slow smtplib (simulated blackhole host) → wait_for times out and returns smtp_timeout_* without blocking the event loop.
    import time as _time

    def _slow(to, subject, body, *, config):
        _time.sleep(2)  # Runs in to_thread without blocking the loop
        return True, None

    monkeypatch.setattr("bowhead_core.email_sender._send_smtp_detailed", _slow)
    cfg = SmtpConfig(host="blackhole", port=587, user="", password="", from_addr="a@x.com")
    ok, err = await deliver_email("b@x.com", "s", "b", config=cfg, timeout=0.2)
    assert ok is False
    assert err == "smtp_timeout_0s"


class _ConnectRecorded(Exception):
    pass


def _record_connect(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, int]]:
    seen: list[tuple[str, int]] = []

    def _create_connection(address, *_args, **_kwargs):
        seen.append(address)
        raise _ConnectRecorded

    import smtplib

    monkeypatch.setattr(smtplib.socket, "create_connection", _create_connection)
    return seen


def test_saas_team_delivery_revalidates_and_blocks_rebound_host(monkeypatch: pytest.MonkeyPatch) -> None:
    import bowhead_router.ssrf_guard as guard
    from bowhead_core.email_sender import _send_smtp_detailed

    monkeypatch.setenv("BOWHEAD_DEPLOYMENT_MODE", "saas")
    monkeypatch.setattr(guard.socket, "getaddrinfo", lambda *_a, **_k: [(2, 1, 6, "", ("10.0.0.7", 0))])
    seen = _record_connect(monkeypatch)
    cfg = SmtpConfig(host="smtp.example.com", port=587, user="", password="", from_addr="a@x.com", source="team")

    assert _send_smtp_detailed("b@x.com", "s", "body", config=cfg) == (False, "smtp_target_blocked")
    assert seen == []


def test_saas_team_delivery_connects_to_validated_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    import bowhead_router.ssrf_guard as guard
    from bowhead_core.email_sender import _send_smtp_detailed

    monkeypatch.setenv("BOWHEAD_DEPLOYMENT_MODE", "saas")
    monkeypatch.setattr(guard.socket, "getaddrinfo", lambda *_a, **_k: [(2, 1, 6, "", ("8.8.8.8", 0))])
    seen = _record_connect(monkeypatch)
    cfg = SmtpConfig(host="smtp.example.com", port=587, user="", password="", from_addr="a@x.com", source="team")

    ok, _err = _send_smtp_detailed("b@x.com", "s", "body", config=cfg)

    assert ok is False
    assert seen == [("8.8.8.8", 587)]


def test_local_team_delivery_keeps_internal_relay(monkeypatch: pytest.MonkeyPatch) -> None:
    from bowhead_core.email_sender import _send_smtp_detailed

    monkeypatch.setenv("BOWHEAD_DEPLOYMENT_MODE", "local")
    seen = _record_connect(monkeypatch)
    cfg = SmtpConfig(host="mail.intranet", port=2500, user="", password="", from_addr="a@x.com", source="team")

    _send_smtp_detailed("b@x.com", "s", "body", config=cfg)

    assert seen == [("mail.intranet", 2500)]


def test_flag_default_on() -> None:
    assert ec.team_email_config_enabled() is True


@pytest.fixture
def community_sender_defaults(tmp_path, monkeypatch):
    path = tmp_path / "email-defaults.json"
    path.write_text('{"schema_version": 1, "from_addr": ""}')
    monkeypatch.setattr(ec, "SMTP_DEFAULTS_PATH", path)


def test_packaged_business_sender_preserves_existing_relay_default(tmp_path, monkeypatch):
    path = tmp_path / "email-defaults.json"
    path.write_text('{"schema_version": 1, "from_addr": "no-reply@bowhead.ai"}')
    monkeypatch.setattr(ec, "SMTP_DEFAULTS_PATH", path)
    monkeypatch.setenv("BOWHEAD_SMTP_HOST", "smtp.example.com")
    assert env_smtp_config().from_addr == "no-reply@bowhead.ai"


@pytest.mark.asyncio
async def test_community_requires_sender_before_opening_relay(community_sender_defaults, monkeypatch):
    from bowhead_core import email_sender

    monkeypatch.setenv("BOWHEAD_SMTP_HOST", "smtp.example.com")
    connections = []
    monkeypatch.setattr(email_sender, "_open_smtp", lambda *args: connections.append(args))
    for config in (env_smtp_config(), ec._config_from_dict({"host": "smtp.example.com"})):
        assert config.from_addr == ""
        assert await deliver_email("recipient@example.test", "Test", "Body", config=config) == (False, "smtp sender not configured")
    assert connections == []


@pytest.mark.asyncio
async def test_community_preserves_operator_and_team_senders(community_sender_defaults, monkeypatch):
    monkeypatch.setenv("BOWHEAD_SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("BOWHEAD_SMTP_FROM", "operator@example.test")
    assert env_smtp_config().from_addr == "operator@example.test"
    team = _FakeStore({"host": "smtp.example.com", "from_addr": "team@example.test"})
    assert (await resolve_smtp_config("team_example", team)).from_addr == "team@example.test"
