"""D16 — composer pin strip keeps only user-intent pins, not auto-pinned tool artifacts.

put_bytes auto-pins every tool artifact into session_attachments (for the Assets tab, which
uses a separate query). The composer pin strip read all of them, flooding the composer with
e.g. 9 bi_query CSVs. _is_user_composer_pin filters to inject='full' (user_upload class).
"""

from __future__ import annotations

from types import SimpleNamespace

from bowhead_gateway.routes.sessions import _is_user_composer_pin


def test_user_upload_full_is_composer_pin() -> None:
    assert _is_user_composer_pin(SimpleNamespace(inject="full", kind="user_upload")) is True


def test_bi_csv_list_not_composer_pin() -> None:
    assert _is_user_composer_pin(SimpleNamespace(inject="list", kind="tool_data")) is False


def test_sandbox_stub_not_composer_pin() -> None:
    assert _is_user_composer_pin(SimpleNamespace(inject="stub", kind="sandbox_output")) is False


def test_legacy_null_inject_user_upload_kept() -> None:
    # pre-asset-model upload (inject backfill may not have run on prod) → COALESCE on kind.
    assert _is_user_composer_pin(SimpleNamespace(inject=None, kind="user_upload")) is True


def test_legacy_null_inject_tool_dropped() -> None:
    assert _is_user_composer_pin(SimpleNamespace(inject=None, kind="tool_data")) is False
