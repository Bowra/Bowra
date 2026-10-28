"""Lead tool for sending an email with optional artifact attachments through team SMTP.

Delivery retains the numeric and sending-confirmation gates:

1. **Quantities must come from this turn's data.** Every amount / percentage in the
   subject and body must be a faithful rounding of a value returned by a data tool
   in this turn (BI, KB, sandbox, web, team tools), a data file produced in this
   turn, or the user's own message. Model-authored text (peer answers, reports,
   canvas, skills) is never a source — otherwise a number made up once and repeated
   would count as sourced. Unsourced quantities are sent back to the model to fix.
2. **The user confirms the exact email.** The handler waits while the owner reviews
   recipient, subject, full body and attachment metadata in the web chat; only an explicit approval
   delivers. Rejection, timeout, cancellation and non-interactive channels all
   return "not sent". Custom Agents retain their released Owner grant.

Attachments reuse the owner/session Results fence and current Space boundary.
Bounded file bytes are frozen before confirmation; unavailable attachments stop
the entire email. The numeric gate does not parse attached documents.
"""

from __future__ import annotations

import csv
import io
import json
import logging
from collections.abc import Callable
from email.utils import parseaddr
from pathlib import Path
from typing import Any, Final

import anyio
from bowhead_router.types import ToolSchema

from bowhead_core.artifact_store import ArtifactReadLimitError
from bowhead_core.email_config import deliver_email, resolve_team_smtp_config
from bowhead_core.email_send_confirmation import EmailSendConfirmations, default_email_send_confirmations
from bowhead_core.email_sender import EmailAttachment
from bowhead_core.number_verifier import expand_for_unit_scale, extract_numbers, unsourced_quantities
from bowhead_core.tool_registry import ToolExecutionContext, ToolRegistry, ToolSpec

logger = logging.getLogger(__name__)

TOOL_NAME: Final = "send_email"
_SUBJECT_MAX = 200
_BODY_MAX = 20_000
_ATTACHMENT_MAX_FILES = 5
_ATTACHMENT_MAX_BYTES = 10 * 1024 * 1024
_ATTACHMENT_MIMES: Final = {
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": (".pptx",),
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": (".docx",),
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": (".xlsx",),
    "application/pdf": (".pdf",),
    "text/csv": (".csv",),
    "text/plain": (".txt",),
    "image/png": (".png",),
    "image/jpeg": (".jpg", ".jpeg"),
}
_store_provider: Callable[[], Any | None] | None = None
_confirmations: EmailSendConfirmations = default_email_send_confirmations()

# Tool families whose results are data from a system of record rather than text the
# model wrote. Everything else (subagent_*, canvas_*, report/skill/memory/clarification
# tools, send_email itself) can echo model prose and is never a number source.
_DATA_TOOL_PREFIXES: Final = ("bi_", "pbi_", "kb_", "python_", "web_", "teammcp.", "mcp.", "personalmcp.")
_DATA_ARTIFACT_KINDS: Final = frozenset({"tool_data", "sandbox_output"})
_DATA_ARTIFACT_SUFFIXES: Final = (".csv", ".tsv", ".json")
_ARTIFACT_MAX_BYTES: Final = 2 * 1024 * 1024
_ARTIFACT_MAX_FILES: Final = 20
_MAX_LISTED_NUMBERS: Final = 10
_ANSWER_RESERVE_SECONDS: Final = 30.0


def set_send_email_store_provider(store_provider: Callable[[], Any | None] | None) -> None:
    global _store_provider  # noqa: PLW0603 -- startup binding, resolved at invocation time
    _store_provider = store_provider


def set_send_email_confirmations(confirmations: EmailSendConfirmations) -> None:
    global _confirmations  # noqa: PLW0603 -- tests bind an isolated queue
    _confirmations = confirmations


def _failure(error: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error": error, "message": message}


def _schema() -> ToolSchema:
    return ToolSchema(
        name=TOOL_NAME,
        description=(
            "Send an email with a plain-text body and optional file attachments through the current team's configured SMTP service only when the user asks. "
            "Use the explicitly requested recipient; for 'my email', first resolve the account email with get_current_user. "
            "Never guess a recipient. Prepare the subject and body from the user's request and actual results. "
            "Amounts and percentages must come from this turn's data; calculate derived values with a calculation tool first. "
            "To send a PPT or other file, pass its existing artifact ID in attachment_ids; do not replace a requested file with a text summary. "
            "Only the requester's own ready files in this chat and Space are allowed: PPTX, DOCX, XLSX, PDF, CSV, TXT, PNG or JPEG, "
            "up to 5 files and 10 MiB total. No paths or URLs. Lead asks the requester to confirm the full email and attachment list; "
            "custom Agents with the Email resource send under their Owner's grant. "
            "Only ok=true means sent; otherwise tell the user the email was not sent."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "收件人邮箱地址"},
                "subject": {"type": "string", "description": "邮件主题", "maxLength": _SUBJECT_MAX},
                "body": {"type": "string", "description": "纯文本邮件正文", "maxLength": _BODY_MAX},
                "attachment_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": _ATTACHMENT_MAX_FILES,
                    "uniqueItems": True,
                    "description": "Existing artifact IDs of files to attach, from this chat. Optional; at most 5 files and 10 MiB total.",
                },
            },
            "required": ["to", "subject", "body"],
        },
    )


def _valid_address(value: str) -> bool:
    display_name, address = parseaddr(value)
    return not display_name and address == value and "@" in address and "\r" not in value and "\n" not in value


async def _send_email(arguments: dict[str, Any], ctx: ToolExecutionContext) -> dict[str, Any]:
    to = str(arguments.get("to") or "").strip()
    subject = str(arguments.get("subject") or "").strip()
    body = str(arguments.get("body") or "")
    if not _valid_address(to):
        return _failure("invalid_recipient", "收件人邮箱地址无效，暂时无法发送邮件。")
    if not subject or len(subject) > _SUBJECT_MAX or "\r" in subject or "\n" in subject:
        return _failure("invalid_subject", "邮件主题无效，暂时无法发送邮件。")
    if not body or len(body) > _BODY_MAX:
        return _failure("invalid_body", "邮件正文不能为空且不能过长，暂时无法发送邮件。")

    team_id = str(getattr(ctx, "team_id", "") or "")
    store = _store_provider() if _store_provider is not None else None
    if not team_id or store is None:
        return _failure("email_service_not_configured", "当前团队尚未配置邮件服务，暂时无法发送邮件。请前往团队设置的“邮件服务”完成配置。")
    try:
        config = await resolve_team_smtp_config(team_id, store)
    except Exception:
        logger.warning("send_email.config_unavailable team=%s", team_id, exc_info=True)
        return _failure("email_service_unavailable", "当前团队邮件服务暂时不可用，请稍后重试或联系团队管理员。")
    if config is None:
        return _failure("email_service_not_configured", "当前团队尚未配置邮件服务，暂时无法发送邮件。请前往团队设置的“邮件服务”完成配置。")

    # A custom Agent whose released definition includes the Email Plugin (or the Tool) runs under the
    # Owner's grant and sends without asking each time; Lead asks the requester for every email.
    owner_granted = _custom_agent_run(ctx)
    if not owner_granted and not bool(getattr(ctx, "config_lite_supported", False)):
        return _failure(
            "email_confirmation_unavailable",
            "当前通道无法请用户确认发送内容，邮件未发送。请如实告知用户，并请其在网页端对话中重新发起。",
        )

    unsourced = await _unsourced_quantities(f"{subject}\n{body}", ctx)
    if unsourced:
        listed = "、".join(unsourced[:_MAX_LISTED_NUMBERS])
        return {
            "ok": False,
            "error": "email_numbers_unverified",
            "unverified_numbers": unsourced[:_MAX_LISTED_NUMBERS],
            "message": (
                f"邮件未发送：以下数字在本轮取得的数据中找不到来源：{listed}。"
                "请回到本轮工具返回的数据逐一核对并改成数据里的数值；合计、差值等派生数先用计算工具算出；"
                "无法核实的数字请删去；数字只在前几轮出现过时，先在本轮重新取数或计算。"
                "修正后必须再次调用本工具，邮件才会发出；若最终无法发送，必须在回答里明确告诉用户邮件未发送及原因。"
            ),
        }

    attachment_ids = arguments.get("attachment_ids", [])
    if (
        not isinstance(attachment_ids, list)
        or len(attachment_ids) > _ATTACHMENT_MAX_FILES
        or any(not isinstance(aid, str) or not aid.startswith("art_") for aid in attachment_ids)
        or len(set(attachment_ids)) != len(attachment_ids)
    ):
        return _failure("invalid_attachments", "Provide at most 5 distinct artifact IDs in attachment_ids. No email was sent.")
    attachments: list[EmailAttachment] = []
    attachment_details: list[dict[str, str | int]] = []
    if attachment_ids:
        artifact_store = getattr(ctx, "artifact_store", None)
        if artifact_store is None or not ctx.user_id or not ctx.session_id or not ctx.workspace_id:
            return _failure(
                "email_attachment_unavailable",
                "Files cannot be resolved for the current chat. No email was sent; attach the files in this chat first.",
            )
        for aid in attachment_ids:
            try:
                art = await artifact_store.get_result_artifact_for_scope(artifact_id=aid, session_id=ctx.session_id, user_id=ctx.user_id)
                if art is None or art.workspace_id != ctx.workspace_id:
                    return {
                        **_failure(
                            "email_attachment_unavailable",
                            "The file is unavailable in your current chat and Space. Upload or generate it here first.",
                        ),
                        "artifact_id": aid,
                    }
                if Path(art.filename).suffix.lower() not in _ATTACHMENT_MIMES.get(art.mime, ()) or any(
                    ord(c) < 32 or ord(c) == 127 or c in "/\\" for c in art.filename
                ):
                    return {
                        **_failure(
                            "email_attachment_unsupported",
                            "Unsupported file type or filename. Use PPTX, DOCX, XLSX, PDF, CSV, TXT, PNG or JPEG with a plain filename.",
                        ),
                        "artifact_id": aid,
                    }
                remaining = _ATTACHMENT_MAX_BYTES - sum(len(item.content) for item in attachments)
                if art.size < 0 or art.size > remaining:
                    return _failure(
                        "email_attachments_too_large", "Attachments exceed the 10 MiB total limit. Use smaller files or send them separately."
                    )
                content = await artifact_store.backend.read_bounded(art.path, limit=remaining)
                if len(content) != art.size:
                    return {
                        **_failure("email_attachment_read_failed", "The file size changed. Generate or upload the file again before sending."),
                        "artifact_id": aid,
                    }
                attachments.append(EmailAttachment(filename=art.filename, mime=art.mime, content=content))
                attachment_details.append({"artifact_id": aid, "filename": art.filename, "mime": art.mime, "size": len(content)})
            except ArtifactReadLimitError:
                return _failure(
                    "email_attachments_too_large", "Attachments exceed the 10 MiB total limit. Use smaller files or send them separately."
                )
            except Exception:
                logger.warning("send_email.attachment_read_failed artifact=%s", aid)
                return {
                    **_failure("email_attachment_read_failed", "The file could not be read. Generate or upload it again before sending."),
                    "artifact_id": aid,
                }

    decision = (
        "approved"
        if owner_granted
        else await _confirmations.request(
            owner_user_id=str(getattr(ctx, "user_id", "") or ""),
            turn_id=str(getattr(ctx, "turn_id", "") or ""),
            session_id=str(getattr(ctx, "session_id", "") or ""),
            to=to,
            subject=subject,
            body=body,
            timeout_seconds=_confirmation_window(),
            attachments=attachment_details,
        )
    )
    if decision != "approved":
        reason = {
            "rejected": ("email_send_rejected", "用户拒绝了这封邮件，邮件未发送。"),
            "expired": ("email_confirmation_expired", "用户未在时限内确认，邮件未发送。"),
        }.get(decision, ("email_confirmation_unavailable", "暂时无法请用户确认，邮件未发送。"))
        return _failure(reason[0], reason[1] + "不要告诉用户邮件已发送；必须在回答里明确告诉用户邮件未发送。")

    # Send the bytes frozen before confirmation; never re-read a mutable blob after approval.
    if attachments:
        for aid in attachment_ids:
            try:
                current = await artifact_store.get_result_artifact_for_scope(artifact_id=aid, session_id=ctx.session_id, user_id=ctx.user_id)
            except Exception:
                logger.warning("send_email.attachment_recheck_failed artifact=%s", aid)
                current = None
            if current is None or current.workspace_id != ctx.workspace_id:
                return {
                    **_failure("email_attachment_unavailable", "The file is no longer available in this chat. No email was sent."),
                    "artifact_id": aid,
                }
        ok, _error = await deliver_email(to, subject, body, config=config, attachments=tuple(attachments))
    else:
        ok, _error = await deliver_email(to, subject, body, config=config)
    if not ok:
        logger.warning("send_email.delivery_failed team=%s to=%s", team_id, to)
        return _failure("email_delivery_failed", "邮件暂时无法发送，请检查团队邮件服务配置后重试。")
    # The receipt says what was sent, so a run without a confirmation card still shows it.
    return {
        "ok": True,
        "message": "邮件已发送。",
        "sent": {"to": to, "subject": subject, **({"attachments": attachment_details} if attachments else {})},
    }


def _custom_agent_run(ctx: ToolExecutionContext) -> bool:
    """A team Agent's own tool call (dispatched with the analyst role), not a Lead call.

    Only a custom Agent whose released definition selected the Email Plugin or Tool can reach this
    Tool under that role: Lead-only Tools are opened to team Agents through
    ``resource_disable.team_agent_opened_tools`` and system Agents never list it.
    """
    return getattr(ctx, "role", None) == "analyst"


async def _handler(arguments: dict[str, Any], ctx: ToolExecutionContext) -> dict[str, Any]:
    result = await _send_email(arguments, ctx)
    from bowhead_core.presentation_locale import resolve_presentation_locale

    context = getattr(ctx, "language_context", None)
    lang = resolve_presentation_locale(getattr(context, "output_language", None), getattr(context, "ui_language", None))
    if lang == "zh-CN":
        if str(result.get("error") or "").startswith(("email_attachment", "invalid_attachments")):
            result["message"] = _email_failure_copy(str(result["error"]), str(arguments.get("to") or ""), lang)
        return result
    if result.get("ok") is True:
        result["message"] = {"en-US": "The email was sent.", "de-DE": "Die E-Mail wurde gesendet.", "fr-FR": "L’e-mail a été envoyé."}[lang]
        return result
    # Keep the number-check and disclosure instructions in the actual tool result.
    result["message"] = _email_failure_copy(str(result.get("error") or ""), str(arguments.get("to") or ""), lang)
    if result.get("error") == "email_numbers_unverified":
        listed = ", ".join(str(value) for value in result.get("unverified_numbers", []))
        instructions = {
            "en-US": "Unverified numbers: {numbers}. Check each against data returned by tools in this turn and use those values. Calculate totals and differences with a calculation tool first; remove numbers that cannot be verified. If numbers came from an earlier turn, retrieve or calculate them again in this turn. After correcting the content, call this tool again to send the email.",
            "de-DE": "Nicht verifizierte Zahlen: {numbers}. Gleichen Sie jede Zahl mit den Tooldaten dieser Gesprächsrunde ab und verwenden Sie die dort enthaltenen Werte. Berechnen Sie Summen und Differenzen zunächst mit einem Berechnungstool; entfernen Sie nicht verifizierbare Zahlen. Stammen Zahlen aus einer früheren Gesprächsrunde, rufen Sie sie in dieser Runde erneut ab oder berechnen Sie sie neu. Rufen Sie dieses Tool nach der Korrektur erneut auf, um die E-Mail zu senden.",
            "fr-FR": "Nombres non vérifiés : {numbers}. Vérifiez chacun dans les données renvoyées par les outils pendant cet échange et utilisez ces valeurs. Calculez d’abord les totaux et différences avec un outil de calcul ; supprimez les nombres invérifiables. Si les nombres proviennent d’un échange précédent, récupérez-les ou calculez-les à nouveau pendant cet échange. Après correction, appelez à nouveau cet outil pour envoyer l’e-mail.",
        }
        result["message"] += " " + instructions[lang].format(numbers=listed)
    result["message"] += (
        " "
        + {
            "en-US": "Only ok=true means the email was sent. Otherwise explicitly tell the user it was not sent and why.",
            "de-DE": "Nur ok=true bedeutet, dass die E-Mail gesendet wurde. Teilen Sie dem Nutzer andernfalls ausdrücklich mit, dass sie nicht gesendet wurde, und nennen Sie den Grund.",
            "fr-FR": "Seul ok=true signifie que l’e-mail a été envoyé. Sinon, indiquez explicitement à l’utilisateur qu’il n’a pas été envoyé et pourquoi.",
        }[lang]
    )
    return result


def confirmation_window() -> float | None:
    """Public for other Lead delivery tools (Google Workspace writes) — one budget rule."""
    return _confirmation_window()


async def unsourced_turn_quantities(text: str, ctx: ToolExecutionContext) -> list[str]:
    """Numbers in ``text`` with no source in this turn's data — shared by every Lead delivery tool."""
    return await _unsourced_quantities(text, ctx)


def _confirmation_window() -> float | None:
    """Leave the Lead time to answer after the owner decides (the turn has a wall-clock budget)."""
    import time

    from bowhead_core.agents._lead_context import get_turn_deadline

    deadline = get_turn_deadline()
    return None if deadline is None else deadline - time.monotonic() - _ANSWER_RESERVE_SECONDS


async def _unsourced_quantities(text: str, ctx: ToolExecutionContext) -> list[str]:
    bag: set[float] = set()
    for result in _data_tool_results(ctx):
        extract_numbers(result, bag)
    for table in await _turn_data_files(ctx):
        _extract_table_numbers(table, bag)
    extract_numbers(str(getattr(ctx, "user_text", "") or ""), bag)
    return await anyio.to_thread.run_sync(lambda: unsourced_quantities(text, expand_for_unit_scale(bag)))


def _data_tool_results(ctx: ToolExecutionContext) -> list[Any]:
    """Results of data tools already executed in this turn, including peers' nested calls."""
    from bowhead_core.agents._lead_context import get_current_subagent_invocations

    invocations = get_current_subagent_invocations()
    if invocations is None:
        invocations = getattr(ctx, "_current_subagent_invocations", None)
    pending = list(invocations or [])
    results: list[Any] = []
    while pending:
        inv = pending.pop()
        pending.extend(getattr(inv, "nested", None) or [])
        name = str(getattr(inv, "tool_name", "") or "")
        if getattr(inv, "error", None) or getattr(inv, "result", None) is None:
            continue
        if name.startswith(_DATA_TOOL_PREFIXES):
            results.append(inv.result)
    return results


async def _turn_data_files(ctx: ToolExecutionContext) -> list[str]:
    """Text of data files (BI exports, sandbox tables) produced in this turn."""
    store = getattr(ctx, "artifact_store", None)
    turn_id = str(getattr(ctx, "turn_id", "") or "")
    if store is None or not turn_id:
        return []
    try:
        artifacts = await store.list_result_artifacts_for_turn(turn_id)
    except Exception:
        logger.warning("send_email.data_files_unavailable turn=%s", turn_id, exc_info=True)
        return []
    texts: list[str] = []
    for art in artifacts:
        if len(texts) >= _ARTIFACT_MAX_FILES:
            break
        if (
            getattr(art, "kind", "") not in _DATA_ARTIFACT_KINDS
            or getattr(art, "trust", "trusted") != "trusted"
            or not str(getattr(art, "filename", "")).lower().endswith(_DATA_ARTIFACT_SUFFIXES)
            or int(getattr(art, "size", 0) or 0) > _ARTIFACT_MAX_BYTES
        ):
            continue
        try:
            texts.append((await store.open_read(art)).decode("utf-8", errors="replace"))
        except Exception:
            logger.warning("send_email.data_file_unreadable artifact=%s", getattr(art, "id", ""), exc_info=True)
    return texts


def _extract_table_numbers(text: str, bag: set[float]) -> None:
    """Cell-wise so unquoted CSV separators never merge neighbouring values into one number."""
    stripped = text.lstrip()
    if stripped.startswith(("{", "[")):
        try:
            extract_numbers(json.loads(stripped), bag)
            return
        except ValueError:
            pass
    dialect = "excel-tab" if "\t" in text.split("\n", 1)[0] else "excel"
    for row in csv.reader(io.StringIO(text), dialect=dialect):
        for cell in row:
            value = cell.strip()
            try:
                bag.add(float(value.replace(",", "")))
            except ValueError:
                extract_numbers(value, bag)


# User-visible "email not sent" explanation by error code: reason + next step, in plain language without internal mechanisms.
_UNSENT_REASON: Final[dict[str, dict[str, tuple[str, str]]]] = {
    "zh-CN": {
        "invalid_attachments": ("附件列表无效", "请选择最多 5 个不同的文件成果，不要提供文件路径或网址。"),
        "email_attachment_unavailable": ("附件在当前对话或空间中不可用", "请在本对话上传或重新生成你本人的文件后再发送。"),
        "email_attachment_unsupported": (
            "附件的文件类型或文件名不受支持",
            "请使用 PPTX、DOCX、XLSX、PDF、CSV、TXT、PNG 或 JPEG，并使用不含路径或控制字符的文件名。",
        ),
        "email_attachments_too_large": ("附件合计超过 10 MiB", "请缩小文件或分开发送。"),
        "email_attachment_read_failed": ("无法完整读取附件", "请在本对话重新生成或上传文件后再发送。"),
        "email_numbers_unverified": (
            "邮件里的部分数字没能与本轮取得的数据逐一核对上",
            "如需发送，请在同一条消息里让我重新取数并发送，例如「重新查询这组数据并发邮件给 {to}」。",
        ),
        "email_send_rejected": ("你在发送前确认时拒绝了这封邮件", "如需修改后再发，告诉我要改的地方即可。"),
        "email_confirmation_expired": ("发送前的确认在时限内没有收到回应", "如仍需发送，请再让我发一次，并在弹出的确认框里确认。"),
        "email_confirmation_unavailable": ("当前无法请你确认邮件内容", "请在网页端对话里重新让我发送这封邮件。"),
        "email_service_not_configured": ("团队尚未配置邮件服务", "请团队管理员在团队设置的「邮件服务」里完成配置后再让我发送。"),
        "email_service_unavailable": ("团队邮件服务暂时不可用", "请稍后再让我发送，或联系团队管理员检查邮件服务。"),
        "email_delivery_failed": ("邮件服务没能把邮件投递出去", "请稍后再让我发送，或联系团队管理员检查邮件服务配置。"),
        "invalid_recipient": ("收件人邮箱地址无效", "请确认收件人地址后再让我发送。"),
        "invalid_subject": ("邮件主题无效", "请告诉我合适的主题后再让我发送。"),
        "invalid_body": ("邮件正文为空或过长", "请精简内容后再让我发送。"),
        "": ("发送过程中出现问题", "请稍后再让我发送一次。"),
    },
    "en-US": {
        "invalid_attachments": ("the attachment list is invalid", "Select at most 5 distinct artifact files, not paths or URLs."),
        "email_attachment_unavailable": (
            "an attachment is unavailable in this chat or Space",
            "Upload or generate your own file in this chat before sending.",
        ),
        "email_attachment_unsupported": (
            "an attachment type or filename is unsupported",
            "Use PPTX, DOCX, XLSX, PDF, CSV, TXT, PNG or JPEG with a filename containing no path or control characters.",
        ),
        "email_attachments_too_large": ("attachments exceed the 10 MiB total limit", "Use smaller files or send them separately."),
        "email_attachment_read_failed": (
            "an attachment could not be read completely",
            "Generate or upload the file again in this chat before sending.",
        ),
        "email_numbers_unverified": (
            "some numbers in the email could not be matched to the data retrieved in this turn",
            "To send it, ask me in a single message to re-fetch the data and send it, e.g. “re-run this query and email it to {to}”.",
        ),
        "email_send_rejected": (
            "you declined this email when asked to confirm it",
            "Tell me what to change if you would like to send a revised version.",
        ),
        "email_confirmation_expired": (
            "the confirmation request was not answered in time",
            "Ask me to send it again and confirm it in the dialog that appears.",
        ),
        "email_confirmation_unavailable": ("the email could not be shown to you for confirmation here", "Ask me to send it again from the web chat."),
        "email_service_not_configured": (
            "your team has not set up an email service yet",
            "Ask a team admin to configure Email Service in team settings, then ask me again.",
        ),
        "email_service_unavailable": (
            "the team email service is temporarily unavailable",
            "Ask me again later, or contact a team admin to check the email service.",
        ),
        "email_delivery_failed": (
            "the email service could not deliver the message",
            "Ask me again later, or contact a team admin to check the email service settings.",
        ),
        "invalid_recipient": ("the recipient address is not valid", "Confirm the address, then ask me again."),
        "invalid_subject": ("the subject is not valid", "Give me a suitable subject, then ask me again."),
        "invalid_body": ("the email body is empty or too long", "Shorten it, then ask me again."),
        "": ("something went wrong while sending", "Please ask me to send it again later."),
    },
    "de-DE": {
        "invalid_attachments": ("die Anhangsliste ist ungültig", "Wählen Sie höchstens 5 unterschiedliche Ergebnisdateien, keine Pfade oder URLs."),
        "email_attachment_unavailable": (
            "ein Anhang ist in diesem Chat oder Space nicht verfügbar",
            "Laden Sie Ihre eigene Datei in diesem Chat hoch oder erstellen Sie sie hier erneut.",
        ),
        "email_attachment_unsupported": (
            "der Dateityp oder Dateiname eines Anhangs wird nicht unterstützt",
            "Verwenden Sie PPTX, DOCX, XLSX, PDF, CSV, TXT, PNG oder JPEG mit einem Dateinamen ohne Pfad oder Steuerzeichen.",
        ),
        "email_attachments_too_large": ("die Anhänge überschreiten zusammen 10 MiB", "Verkleinern Sie die Dateien oder senden Sie sie getrennt."),
        "email_attachment_read_failed": (
            "ein Anhang konnte nicht vollständig gelesen werden",
            "Erstellen Sie die Datei erneut oder laden Sie sie in diesem Chat nochmals hoch.",
        ),
        "email_numbers_unverified": (
            "einige Zahlen in der E-Mail konnten nicht mit den in dieser Gesprächsrunde abgerufenen Daten abgeglichen werden",
            "Bitten Sie mich zum Senden in einer einzigen Nachricht, die Daten erneut abzurufen und zu versenden, etwa: „Führen Sie diese Abfrage erneut aus und senden Sie das Ergebnis per E-Mail an {to}“.",
        ),
        "email_send_rejected": (
            "Sie haben diese E-Mail bei der Bestätigung abgelehnt",
            "Teilen Sie mir mit, was ich ändern soll, wenn Sie eine überarbeitete Fassung senden möchten.",
        ),
        "email_confirmation_expired": (
            "die Bestätigung ist nicht innerhalb der vorgesehenen Zeit eingegangen",
            "Bitten Sie mich erneut um das Senden und bestätigen Sie es im angezeigten Dialog.",
        ),
        "email_confirmation_unavailable": (
            "der Inhalt der E-Mail konnte Ihnen hier nicht zur Bestätigung angezeigt werden",
            "Bitten Sie mich im Webchat erneut, die E-Mail zu senden.",
        ),
        "email_service_not_configured": (
            "Ihr Team hat noch keinen E-Mail-Dienst eingerichtet",
            "Bitten Sie einen Teamadministrator, den E-Mail-Dienst in den Teameinstellungen einzurichten, und fragen Sie mich anschließend erneut.",
        ),
        "email_service_unavailable": (
            "der E-Mail-Dienst des Teams ist vorübergehend nicht verfügbar",
            "Fragen Sie mich später erneut oder bitten Sie einen Teamadministrator, den E-Mail-Dienst zu prüfen.",
        ),
        "email_delivery_failed": (
            "der E-Mail-Dienst konnte die Nachricht nicht zustellen",
            "Fragen Sie mich später erneut oder bitten Sie einen Teamadministrator, die Einstellungen des E-Mail-Dienstes zu prüfen.",
        ),
        "invalid_recipient": ("die Empfängeradresse ist ungültig", "Prüfen Sie die Adresse und fragen Sie mich anschließend erneut."),
        "invalid_subject": ("der Betreff ist ungültig", "Nennen Sie mir einen geeigneten Betreff und fragen Sie mich anschließend erneut."),
        "invalid_body": ("der E-Mail-Text ist leer oder zu lang", "Kürzen Sie den Text und fragen Sie mich anschließend erneut."),
        "": ("beim Senden ist ein Problem aufgetreten", "Bitten Sie mich später erneut, die E-Mail zu senden."),
    },
    "fr-FR": {
        "invalid_attachments": (
            "la liste des pièces jointes est invalide",
            "Sélectionnez au maximum 5 fichiers de résultats distincts, sans chemins ni URL.",
        ),
        "email_attachment_unavailable": (
            "une pièce jointe est indisponible dans ce chat ou cet espace",
            "Importez ou générez votre propre fichier dans ce chat avant l’envoi.",
        ),
        "email_attachment_unsupported": (
            "le type ou le nom d’une pièce jointe n’est pas pris en charge",
            "Utilisez PPTX, DOCX, XLSX, PDF, CSV, TXT, PNG ou JPEG avec un nom sans chemin ni caractère de contrôle.",
        ),
        "email_attachments_too_large": (
            "les pièces jointes dépassent la limite totale de 10 Mio",
            "Réduisez les fichiers ou envoyez-les séparément.",
        ),
        "email_attachment_read_failed": (
            "une pièce jointe n’a pas pu être lue entièrement",
            "Générez ou importez à nouveau le fichier dans ce chat avant l’envoi.",
        ),
        "email_numbers_unverified": (
            "certains nombres de l’e-mail n’ont pas pu être rapprochés des données récupérées pendant cet échange",
            "Pour l’envoyer, demandez-moi dans un seul message de récupérer à nouveau les données et de les envoyer, par exemple : « Relancez cette requête et envoyez le résultat par e-mail à {to} ».",
        ),
        "email_send_rejected": (
            "vous avez refusé cet e-mail lors de la confirmation",
            "Indiquez-moi les modifications souhaitées si vous voulez envoyer une version révisée.",
        ),
        "email_confirmation_expired": (
            "la demande de confirmation n’a pas reçu de réponse à temps",
            "Demandez-moi de l’envoyer à nouveau et confirmez dans le dialogue qui s’affiche.",
        ),
        "email_confirmation_unavailable": (
            "le contenu de l’e-mail n’a pas pu vous être présenté ici pour confirmation",
            "Demandez-moi de l’envoyer à nouveau depuis la conversation web.",
        ),
        "email_service_not_configured": (
            "votre équipe n’a pas encore configuré de service de messagerie",
            "Demandez à un administrateur d’équipe de configurer le service de messagerie dans les paramètres de l’équipe, puis demandez-moi à nouveau.",
        ),
        "email_service_unavailable": (
            "le service de messagerie de l’équipe est temporairement indisponible",
            "Demandez-moi à nouveau plus tard ou contactez un administrateur d’équipe pour vérifier le service de messagerie.",
        ),
        "email_delivery_failed": (
            "le service de messagerie n’a pas pu livrer le message",
            "Demandez-moi à nouveau plus tard ou contactez un administrateur d’équipe pour vérifier les paramètres du service de messagerie.",
        ),
        "invalid_recipient": ("l’adresse du destinataire est invalide", "Vérifiez l’adresse, puis demandez-moi à nouveau."),
        "invalid_subject": ("l’objet est invalide", "Donnez-moi un objet approprié, puis demandez-moi à nouveau."),
        "invalid_body": ("le corps de l’e-mail est vide ou trop long", "Raccourcissez-le, puis demandez-moi à nouveau."),
        "": ("un problème est survenu pendant l’envoi", "Demandez-moi de l’envoyer à nouveau plus tard."),
    },
}


def _invocation_outcome(inv: Any) -> tuple[bool, str] | None:
    """(sent, error_code) for a send_email invocation; None for other tools."""
    if str(getattr(inv, "tool_name", "") or "") != TOOL_NAME:
        return None
    result = getattr(inv, "result", None)
    if isinstance(result, dict) and result.get("ok") is True and not getattr(inv, "error", None):
        return True, ""
    return False, str(result.get("error") or "") if isinstance(result, dict) else ""


def unsent_email_notice(invocations: Any, lang: str = "en-US") -> str:
    """User-facing "email was not sent" paragraph, or "" when nothing is owed.

    Owed iff the turn's last ``send_email`` call did not succeed — some call failed and
    no later call went through. The model's closing prose is not trusted to say this
    (116 t_4eabdf899529: blocked by the number check, recomputed, then answered without
    a word about the email), so the Lead's final text gets this appended deterministically.
    """
    last: tuple[bool, str, str] | None = None
    for inv in list(invocations or []):
        outcome = _invocation_outcome(inv)
        if outcome is None:
            continue
        args = getattr(inv, "arguments", None)
        to = str(args.get("to") or "").strip() if isinstance(args, dict) else ""
        last = (outcome[0], outcome[1], to if _valid_address(to) else "")
    if last is None or last[0]:
        return ""
    _sent, code, to = last
    from bowhead_core.presentation_locale import resolve_presentation_locale

    return _email_failure_copy(code, to, resolve_presentation_locale(lang))


def _email_failure_copy(code: str, to: str, lang: str) -> str:
    to = to.strip()
    if not _valid_address(to):
        to = ""
    table = _UNSENT_REASON[lang]
    reason, next_step = table.get(code) or table[""]
    if lang == "en-US":
        next_step = next_step.replace("{to}", to or "the recipient")
        target = f" to {to}" if to else ""
        return f"**The email{target} was not sent**: {reason}. {next_step}"
    if lang == "de-DE":
        next_step = next_step.replace("{to}", to or "die Empfängeradresse")
        target = f" an {to}" if to else ""
        return f"**Die E-Mail{target} wurde nicht gesendet**: {reason}. {next_step}"
    if lang == "fr-FR":
        next_step = next_step.replace("{to}", to or "l’adresse du destinataire")
        target = f" à {to}" if to else ""
        return f"**L’e-mail{target} n’a pas été envoyé** : {reason}. {next_step}"
    next_step = next_step.replace("{to}", to or "收件人")
    target = f"（收件人 {to}）" if to else ""
    return f"**邮件未发送**{target}：{reason}。{next_step}"


def register_send_email_tool(
    registry: ToolRegistry,
    *,
    store_provider: Callable[[], Any | None] | None = None,
) -> bool:
    set_send_email_store_provider(store_provider)
    registry.register(
        ToolSpec(
            schema=_schema(),
            handler=_handler,
            risk_level="medium",
            allowed_for_roles=frozenset({"lead"}),
        )
    )
    return True
