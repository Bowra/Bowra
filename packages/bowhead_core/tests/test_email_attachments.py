"""Attachment authorization, bounded reads and the approved-byte snapshot."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiosqlite
import anyio
import pytest
from alembic import command
from alembic.config import Config
from bowhead_core.agents._lead_context import set_current_subagent_invocations
from bowhead_core.artifact_store import Artifact, ArtifactStore, LocalBackend
from bowhead_core.email_config import SmtpConfig
from bowhead_core.email_send_confirmation import EmailSendConfirmations
from bowhead_core.language_context import LanguageContext
from bowhead_core.session_store import SessionStore

from bowhead_core import migrations
from bowhead_core import send_email_tool as tool

MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def schema(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("email-attachment-schema") / "core.db"
    config = Config()
    config.set_main_option("script_location", str(Path(migrations.__file__).resolve().parent))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    config.attributes["bowhead_target"] = "core"
    command.upgrade(config, "head")
    return path


@pytest.fixture
async def ctx(tmp_path: Path, schema: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    path = tmp_path / "core.db"
    with sqlite3.connect(schema) as source, sqlite3.connect(path) as destination:
        source.backup(destination)
    store = ArtifactStore(db_path=path, backend=LocalBackend(tmp_path / "files"))
    sessions = SessionStore(path)
    await sessions.ensure_session(session_id="s_1", workspace_id="ws_1", user_id="usr_owner")
    await sessions.ensure_session(session_id="s_other", workspace_id="ws_other", user_id="usr_owner")
    cfg = SmtpConfig(host="smtp.example.com", port=587, user="", password="", from_addr="sender@example.com")
    monkeypatch.setattr(tool, "resolve_team_smtp_config", AsyncMock(return_value=cfg))
    monkeypatch.setattr(tool, "_store_provider", lambda: object())
    monkeypatch.setattr(tool, "deliver_email", AsyncMock(return_value=(True, None)))
    monkeypatch.setattr(tool, "_confirmations", SimpleNamespace(request=AsyncMock(return_value="approved")))
    set_current_subagent_invocations([])
    return SimpleNamespace(
        team_id="team_1",
        workspace_id="ws_1",
        user_id="usr_owner",
        session_id="s_1",
        turn_id="t_1",
        user_text="Send the PPT to my email.",
        config_lite_supported=True,
        artifact_store=store,
        language_context=LanguageContext(output_language="en-US"),
    )


async def _file(ctx: SimpleNamespace, **over: object) -> Artifact:
    args = {
        "content": b"presentation bytes",
        "filename": "销售报告.pptx",
        "mime": MIME,
        "created_by": "usr_owner",
        "session_id": "s_1",
        "kind": "deliverable",
        "workspace_id": "ws_1",
    }
    args.update(over)
    return await ctx.artifact_store.put_bytes(**args)


async def _send(ctx: SimpleNamespace, ids: object) -> dict[str, object]:
    return await tool._handler(
        {"to": "me@example.com", "subject": "Sales report", "body": "Please see the attached report.", "attachment_ids": ids}, ctx
    )


async def test_sends_the_exact_approved_file_and_returns_metadata(ctx: SimpleNamespace) -> None:
    art = await _file(ctx)
    original = await ctx.artifact_store.open_read(art)

    async def approve(**snapshot: object) -> str:
        assert snapshot["attachments"] == [{"artifact_id": art.id, "filename": art.filename, "mime": MIME, "size": len(original)}]
        tool.deliver_email.assert_not_awaited()
        Path(art.path).write_bytes(b"changed after approval snapshot")
        return "approved"

    tool._confirmations.request.side_effect = approve
    result = await _send(ctx, [art.id])
    assert result["ok"] is True
    assert result["sent"]["attachments"][0]["artifact_id"] == art.id
    sent = tool.deliver_email.call_args.kwargs["attachments"]
    assert len(sent) == 1 and sent[0].content == original and sent[0].filename == art.filename


@pytest.mark.parametrize("boundary", ["other_user", "other_session", "other_space", "deleted", "pending", "internal", "missing"])
async def test_unavailable_artifacts_never_reach_confirmation_or_smtp(ctx: SimpleNamespace, boundary: str) -> None:
    overrides = {
        "other_user": {"created_by": "usr_other"},
        "other_session": {"session_id": "s_other", "workspace_id": "ws_other"},
        "other_space": {"workspace_id": "ws_other"},
    }
    art = await _file(ctx, **overrides.get(boundary, {}))
    async with aiosqlite.connect(ctx.artifact_store.db_path) as db:
        if boundary == "deleted":
            await db.execute("UPDATE artifacts SET deleted_at='2026-10-21' WHERE id=?", (art.id,))
        if boundary == "pending":
            await db.execute("UPDATE artifacts SET status='pending_upload' WHERE id=?", (art.id,))
        if boundary == "internal":
            await db.execute("UPDATE artifacts SET producer_kind='template_document_internal_v1' WHERE id=?", (art.id,))
        await db.commit()
    result = await _send(ctx, ["art_missing" if boundary == "missing" else art.id])
    assert result["error"] == "email_attachment_unavailable"
    assert result["artifact_id"] == ("art_missing" if boundary == "missing" else art.id)
    tool._confirmations.request.assert_not_awaited()
    tool.deliver_email.assert_not_awaited()


@pytest.mark.parametrize(
    "ids", [None, "art_1", ["/etc/passwd"], ["https://example.com/file"], ["art_1", "art_1"], [f"art_{i}" for i in range(6)], [{}]]
)
async def test_invalid_attachment_input_is_rejected(ctx: SimpleNamespace, ids: object) -> None:
    result = await _send(ctx, ids)
    assert result["error"] == "invalid_attachments"
    tool.deliver_email.assert_not_awaited()


@pytest.mark.parametrize("mime", ["application/octet-stream", "text/plain"])
async def test_unsupported_file_type_is_not_sent(ctx: SimpleNamespace, mime: str) -> None:
    art = await _file(ctx, filename="run.exe", mime=mime)
    assert (await _send(ctx, [art.id]))["error"] == "email_attachment_unsupported"
    tool.deliver_email.assert_not_awaited()


async def test_multiple_attachments_at_the_exact_size_limit(ctx: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tool, "_ATTACHMENT_MAX_BYTES", 36)
    files = [await _file(ctx), await _file(ctx)]
    result = await _send(ctx, [art.id for art in files])
    assert result["ok"] is True
    assert [item["artifact_id"] for item in result["sent"]["attachments"]] == [art.id for art in files]
    assert sum(len(item.content) for item in tool.deliver_email.call_args.kwargs["attachments"]) == 36


async def test_attachment_does_not_bypass_subject_and_body_number_checks(ctx: SimpleNamespace) -> None:
    art = await _file(ctx)
    result = await tool._handler({"to": "me@example.com", "subject": "Sales report", "body": "Revenue was $123.45.", "attachment_ids": [art.id]}, ctx)
    assert result["error"] == "email_numbers_unverified"
    tool._confirmations.request.assert_not_awaited()
    tool.deliver_email.assert_not_awaited()


@pytest.mark.parametrize("locale", ["en-US", "zh-CN", "de-DE", "fr-FR"])
async def test_attachment_failures_keep_actionable_localized_copy(ctx: SimpleNamespace, locale: str) -> None:
    ctx.language_context = LanguageContext(output_language=locale)
    result = await _send(ctx, ["art_missing"])
    assert result["error"] == "email_attachment_unavailable"
    assert result["artifact_id"] == "art_missing"
    action = {
        "en-US": "Upload or generate",
        "zh-CN": "请在本对话上传",
        "de-DE": "Laden Sie Ihre eigene Datei",
        "fr-FR": "Importez ou générez votre propre fichier",
    }
    assert action[locale] in result["message"]


@pytest.mark.parametrize("fault", ["metadata_size", "oversized_blob", "missing_blob", "combined_size"])
async def test_size_limits_and_unreadable_files_stop_the_whole_email(ctx: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, fault: str) -> None:
    monkeypatch.setattr(tool, "_ATTACHMENT_MAX_BYTES", 30)
    art = await _file(ctx)
    ids = [art.id]
    if fault == "metadata_size":
        async with aiosqlite.connect(ctx.artifact_store.db_path) as db:
            await db.execute("UPDATE artifacts SET size=31 WHERE id=?", (art.id,))
            await db.commit()
    elif fault == "oversized_blob":
        Path(art.path).write_bytes(b"x" * 31)
    elif fault == "missing_blob":
        Path(art.path).unlink()
    else:
        ids.append((await _file(ctx)).id)
    result = await _send(ctx, ids)
    assert result["error"] == ("email_attachment_read_failed" if fault == "missing_blob" else "email_attachments_too_large")
    tool._confirmations.request.assert_not_awaited()
    tool.deliver_email.assert_not_awaited()


@pytest.mark.parametrize("decision", ["rejected", "expired"])
async def test_attachment_does_not_bypass_confirmation(ctx: SimpleNamespace, decision: str) -> None:
    art = await _file(ctx)
    tool._confirmations.request.return_value = decision
    assert (await _send(ctx, [art.id]))["ok"] is False
    tool.deliver_email.assert_not_awaited()


async def test_deletion_during_confirmation_prevents_delivery(ctx: SimpleNamespace) -> None:
    art = await _file(ctx)

    async def approve(**_snapshot: object) -> str:
        await ctx.artifact_store.soft_delete(art.id, user_id="usr_owner")
        return "approved"

    tool._confirmations.request.side_effect = approve
    assert (await _send(ctx, [art.id]))["error"] == "email_attachment_unavailable"
    tool.deliver_email.assert_not_awaited()


async def test_custom_agent_keeps_owner_grant_for_attachment_sending(ctx: SimpleNamespace) -> None:
    ctx.role = "analyst"
    ctx.config_lite_supported = False
    art = await _file(ctx)
    assert (await _send(ctx, [art.id]))["ok"] is True
    tool._confirmations.request.assert_not_awaited()
    tool.deliver_email.assert_awaited_once()


async def test_real_confirmation_queue_shows_file_metadata_only(ctx: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    queue = EmailSendConfirmations(timeout_seconds=1)
    monkeypatch.setattr(tool, "_confirmations", queue)
    art = await _file(ctx)

    async with anyio.create_task_group() as tasks:

        async def send() -> None:
            assert (await _send(ctx, [art.id]))["ok"] is True

        tasks.start_soon(send)
        with anyio.fail_after(1):
            while not (pending := queue.pending(ctx.user_id)):
                await anyio.sleep(0)
        assert queue.pending("usr_other") == []
        snapshot = pending[0]
        assert snapshot["attachments"] == [{"artifact_id": art.id, "filename": art.filename, "mime": MIME, "size": art.size}]
        assert "path" not in str(snapshot) and "content" not in str(snapshot)
        assert queue.decide(ctx.user_id, snapshot["id"], True)
