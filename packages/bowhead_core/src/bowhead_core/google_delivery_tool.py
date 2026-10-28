"""Lead delivery tools that write Google Docs, Slides and Sheets as the current member.

Design: ``docs/design/Google Workspace 连接-技术设计-2026-09-24.md`` §6.3.

A delivery action belongs to the Lead, which holds the final report. There is no
per-write confirmation dialog (user decision 2026-10-01): like a coding agent editing
a shared document, the Lead writes when asked and uses the clarification tool when the
target or an overwrite is ambiguous. The guards that remain:

1. web runs only (``config_lite_supported``) — a member's personal grant is never used
   by scheduled jobs, plans, workflow runs or IM channels;
2. every number written must come from this turn's data (shared number check);
3. a write is dispatched once — a failure after dispatch is "outcome unknown", never
   retried automatically;
4. edits to existing Docs/Slides carry the ``requiredRevisionId`` read just before the
   write, so a document changed meanwhile is refused instead of overwritten; Sheets
   have no revision precondition, so a non-empty target range is refused until the
   Lead has asked the user (clarification) and retries with ``overwrite``;
5. an ambiguous section heading is refused with the candidate headings, never guessed.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from collections.abc import Callable
from typing import Any, Final
from urllib.parse import quote

import httpx2
from bowhead_router.types import ToolSchema

from bowhead_core.builtin_google_tool import parse_file_reference
from bowhead_core.google_docs_markdown import document_tab_choices, document_tab_id, select_document_tab
from bowhead_core.google_docs_requests import body_end_index, find_section, insert_requests, plain_text
from bowhead_core.google_workspace import (
    BASE_BUNDLE,
    PROVIDER,
    GoogleWorkspaceError,
    enabled_bundles,
    feature_enabled,
    http_client,
    token_broker,
)
from bowhead_core.tool_registry import ToolExecutionContext, ToolRegistry, ToolSpec

logger = logging.getLogger(__name__)

DOC_TOOL: Final = "google_doc_write"
SLIDES_TOOL: Final = "google_slides_write"
SHEET_TOOL: Final = "google_sheet_write"
TOOL_NAMES: Final = (DOC_TOOL, SLIDES_TOOL, SHEET_TOOL)
_MARKDOWN_MAX = 100_000
_TITLE_MAX = 200
_MAX_SLIDES = 50
_MAX_BULLETS = 20
_MAX_ROWS = 5_000
_MAX_COLS = 200
_CELL = re.compile(r"^([A-Z]{1,3})([1-9][0-9]{0,6})$")

_store_provider: Callable[[], Any | None] | None = None


def set_store_provider(provider: Callable[[], Any | None] | None) -> None:
    global _store_provider  # noqa: PLW0603 -- startup binding, resolved at invocation time
    _store_provider = provider


_MESSAGES: Final[dict[str, str]] = {
    "google_connection_unavailable": "团队未启用 Google Workspace 连接，未写入。请如实告诉用户。",
    "google_web_only": "Google 写入只在网页对话中进行，未写入。请如实告知用户，并请其在网页端对话中重新发起。",
    "google_not_connected": "用户尚未连接 Google 账号，未写入。请告诉用户到「我的连接」连接 Google 后再试。",
    "google_reauth_required": "用户的 Google 授权已过期或被撤销，未写入。请告诉用户到「我的连接」重新连接 Google。",
    "google_scope_missing": "用户的 Google 授权尚未包含该能力，未写入。请告诉用户到「我的连接」更新 Google 授权。",
    "google_file_not_granted": "当前权限无法修改这个文件，未写入。用户可在输入框用「从 Google Drive 选择」插入该文件，或请团队管理员开启对应能力。",
    "google_file_not_found": "文件不存在或用户无权编辑，未写入。请让用户核对链接和权限。",
    "google_invalid_reference": "不是有效的 Google 文件链接，未写入。请向用户确认文件链接。",
    "google_invalid_arguments": "参数无效，未写入。请按工具说明修正参数后重试。",
    "google_tab_required": "文档有多个标签页，未写入。请用澄清工具让用户选择返回的标签页链接，再按该链接写入，不要猜。",
    "google_heading_not_found": "文档中没有唯一匹配的标题，未写入。返回的标题列表里有明确对应的就用它重试；拿不准时用澄清工具问用户要替换哪一节，不要猜。",
    "google_range_not_empty": "目标区域已有数据，未写入。请用澄清工具告诉用户会覆盖哪个区域、多少个非空单元格，问是否覆盖；用户同意后再以 overwrite=true 调用，否则改写到空白区域。",
    "google_no_edit_access": "用户对这个文件只有查看权限或无权编辑，未写入。请如实告诉用户，或改为新建文件。",
    "google_capability_disabled": "团队管理员关闭了修改已有文件的能力，只能修改用户在本次对话中用「从 Google Drive 选择」选中或 Bowhead 新建的文件，未写入。可以改为新建文件，或请用户联系团队管理员。",
    "google_revision_conflict": "写入前文档刚被他人修改，未写入。请重新读取文档后再写；修改内容可能受影响时先用澄清工具问用户。",
    "google_outcome_unknown": "写入请求已发出但结果未知，系统不会自动重试。请告诉用户到 Google 中核实，不要声称已写入。",
    "google_quota_exceeded": "Google 暂时限流，未写入。请稍后再试。",
    "google_unreachable": "暂时无法连接 Google，未写入。请稍后再试。",
    "google_operation_failed": "写入失败，未写入。请如实告诉用户。",
}


_LOCALIZED_MESSAGES: Final[dict[str, dict[str, str]]] = {
    "en-US": {
        "google_connection_unavailable": "The team has not enabled the Google Workspace connection; nothing was written. Tell the user plainly.",
        "google_web_only": "Google writes only happen in web chats; nothing was written. Tell the user and ask them to try again from the web chat.",
        "google_not_connected": "The user has not connected their Google account; nothing was written. Ask them to connect Google under My connections and try again.",
        "google_reauth_required": "The user’s Google authorization expired or was revoked; nothing was written. Ask them to reconnect Google under My connections.",
        "google_scope_missing": "The user’s Google authorization does not include this capability; nothing was written. Ask them to update Google access under My connections.",
        "google_file_not_granted": "Current permissions do not allow editing this file; nothing was written. The user can insert it with Select from Google Drive in the composer, or ask a team admin to enable the required capability.",
        "google_file_not_found": "The file does not exist or the user cannot edit it; nothing was written. Ask them to check the link and permissions.",
        "google_invalid_reference": "The Google file link is invalid; nothing was written. Ask the user to confirm the link.",
        "google_invalid_arguments": "The arguments are invalid; nothing was written. Correct them according to the tool instructions and try again.",
        "google_tab_required": "The document has multiple tabs; nothing was written. Ask the user with the clarification tool to choose one of the returned tab links, then write using that link. Do not guess.",
        "google_heading_not_found": "The document has no uniquely matching heading; nothing was written. Retry with a returned heading only if it clearly matches; otherwise ask the user with the clarification tool which section to replace. Do not guess.",
        "google_range_not_empty": "The target range already contains data; nothing was written. Use the clarification tool to tell the user which range and how many non-empty cells would be overwritten and ask whether to overwrite. Call with overwrite=true only after they agree; otherwise write to an empty range.",
        "google_no_edit_access": "The user can only view this file or cannot edit it; nothing was written. Tell the user plainly, or create a new file instead.",
        "google_capability_disabled": "A team admin turned off editing existing files; only files the user picked with Select from Google Drive or that Bowhead created can be changed. Nothing was written. Create a new file instead, or ask the user to contact a team admin.",
        "google_revision_conflict": "Someone changed the document just before the write; nothing was written. Read it again before writing; if the change may affect your edit, ask the user with the clarification tool first.",
        "google_outcome_unknown": "The write request was sent but its outcome is unknown. It will not be retried automatically. Ask the user to check in Google; do not claim it was written.",
        "google_quota_exceeded": "Google is temporarily rate limiting requests; nothing was written. Try again later.",
        "google_unreachable": "Google could not be reached; nothing was written. Try again later.",
        "google_operation_failed": "The write failed; nothing was written. Tell the user plainly.",
    },
    "de-DE": {
        "google_connection_unavailable": "Das Team hat die Google-Workspace-Verbindung nicht aktiviert; es wurde nichts geschrieben. Teilen Sie dies dem Nutzer ausdrücklich mit.",
        "google_web_only": "In Google wird nur in Web-Chats geschrieben; es wurde nichts geschrieben. Teilen Sie dies mit und bitten Sie um eine erneute Anfrage im Webchat.",
        "google_not_connected": "Der Nutzer hat sein Google-Konto nicht verbunden; es wurde nichts geschrieben. Bitten Sie ihn, Google unter „Meine Verbindungen“ zu verbinden und es erneut zu versuchen.",
        "google_reauth_required": "Die Google-Autorisierung des Nutzers ist abgelaufen oder wurde widerrufen; es wurde nichts geschrieben. Bitten Sie ihn, Google unter „Meine Verbindungen“ erneut zu verbinden.",
        "google_scope_missing": "Die Google-Autorisierung des Nutzers umfasst diese Funktion nicht; es wurde nichts geschrieben. Bitten Sie ihn, die Google-Berechtigungen unter „Meine Verbindungen“ zu aktualisieren.",
        "google_file_not_granted": "Die aktuellen Berechtigungen erlauben keine Änderung dieser Datei; es wurde nichts geschrieben. Der Nutzer kann sie mit „Aus Google Drive auswählen“ im Eingabefeld einfügen oder einen Teamadministrator bitten, die erforderliche Funktion zu aktivieren.",
        "google_file_not_found": "Die Datei existiert nicht oder der Nutzer darf sie nicht bearbeiten; es wurde nichts geschrieben. Bitten Sie ihn, den Link und die Berechtigungen zu prüfen.",
        "google_invalid_reference": "Der Google-Dateilink ist ungültig; es wurde nichts geschrieben. Bitten Sie den Nutzer, den Link zu bestätigen.",
        "google_invalid_arguments": "Die Parameter sind ungültig; es wurde nichts geschrieben. Korrigieren Sie sie gemäß der Toolbeschreibung und versuchen Sie es erneut.",
        "google_tab_required": "Das Dokument hat mehrere Tabs; es wurde nichts geschrieben. Bitten Sie den Nutzer mit dem Klärungstool, einen der zurückgegebenen Tab-Links auszuwählen, und schreiben Sie dann über diesen Link. Nicht raten.",
        "google_heading_not_found": "Im Dokument gibt es keine eindeutig passende Überschrift; es wurde nichts geschrieben. Versuchen Sie es nur mit einer zurückgegebenen Überschrift erneut, wenn sie eindeutig passt; fragen Sie sonst den Nutzer mit dem Klärungstool, welcher Abschnitt ersetzt werden soll. Nicht raten.",
        "google_range_not_empty": "Der Zielbereich enthält bereits Daten; es wurde nichts geschrieben. Teilen Sie dem Nutzer mit dem Klärungstool mit, welcher Bereich und wie viele nicht leere Zellen überschrieben würden, und fragen Sie, ob überschrieben werden soll. Rufen Sie das Tool erst nach Zustimmung mit overwrite=true auf; schreiben Sie sonst in einen leeren Bereich.",
        "google_no_edit_access": "Der Nutzer darf diese Datei nur ansehen oder nicht bearbeiten; es wurde nichts geschrieben. Teilen Sie dies ausdrücklich mit oder erstellen Sie stattdessen eine neue Datei.",
        "google_capability_disabled": "Ein Teamadministrator hat das Bearbeiten vorhandener Dateien deaktiviert; nur Dateien, die der Nutzer mit „Aus Google Drive auswählen“ gewählt hat oder die Bowhead erstellt hat, können geändert werden. Es wurde nichts geschrieben. Erstellen Sie stattdessen eine neue Datei oder bitten Sie den Nutzer, einen Teamadministrator zu kontaktieren.",
        "google_revision_conflict": "Das Dokument wurde kurz vor dem Schreiben von einer anderen Person geändert; es wurde nichts geschrieben. Lesen Sie es vor dem Schreiben erneut; fragen Sie den Nutzer mit dem Klärungstool, falls die Änderung Ihre Bearbeitung betreffen könnte.",
        "google_outcome_unknown": "Die Schreibanfrage wurde gesendet, ihr Ergebnis ist jedoch unbekannt. Es erfolgt kein automatischer erneuter Versuch. Bitten Sie den Nutzer um Prüfung in Google; behaupten Sie nicht, dass der Inhalt geschrieben wurde.",
        "google_quota_exceeded": "Google begrenzt Anfragen vorübergehend; es wurde nichts geschrieben. Versuchen Sie es später erneut.",
        "google_unreachable": "Google konnte nicht erreicht werden; es wurde nichts geschrieben. Versuchen Sie es später erneut.",
        "google_operation_failed": "Das Schreiben ist fehlgeschlagen; es wurde nichts geschrieben. Teilen Sie dies dem Nutzer ausdrücklich mit.",
    },
    "fr-FR": {
        "google_connection_unavailable": "L’équipe n’a pas activé la connexion Google Workspace ; rien n’a été écrit. Indiquez-le clairement à l’utilisateur.",
        "google_web_only": "L’écriture dans Google n’a lieu que dans les conversations web ; rien n’a été écrit. Indiquez-le et demandez à l’utilisateur de réessayer depuis la conversation web.",
        "google_not_connected": "L’utilisateur n’a pas connecté son compte Google ; rien n’a été écrit. Demandez-lui de connecter Google dans « Mes connexions », puis de réessayer.",
        "google_reauth_required": "L’autorisation Google de l’utilisateur a expiré ou a été révoquée ; rien n’a été écrit. Demandez-lui de reconnecter Google dans « Mes connexions ».",
        "google_scope_missing": "L’autorisation Google de l’utilisateur n’inclut pas cette fonctionnalité ; rien n’a été écrit. Demandez-lui de mettre à jour ses autorisations Google dans « Mes connexions ».",
        "google_file_not_granted": "Les droits actuels ne permettent pas de modifier ce fichier ; rien n’a été écrit. L’utilisateur peut l’insérer avec « Sélectionner dans Google Drive » dans le champ de saisie ou demander à un administrateur d’équipe d’activer la fonctionnalité requise.",
        "google_file_not_found": "Le fichier n’existe pas ou l’utilisateur ne peut pas le modifier ; rien n’a été écrit. Demandez-lui de vérifier le lien et les droits d’accès.",
        "google_invalid_reference": "Le lien du fichier Google est invalide ; rien n’a été écrit. Demandez à l’utilisateur de confirmer le lien.",
        "google_invalid_arguments": "Les paramètres sont invalides ; rien n’a été écrit. Corrigez-les selon les instructions de l’outil, puis réessayez.",
        "google_tab_required": "Le document comporte plusieurs onglets ; rien n’a été écrit. Avec l’outil de clarification, demandez à l’utilisateur de choisir un des liens d’onglet renvoyés, puis utilisez ce lien pour écrire. Ne devinez pas.",
        "google_heading_not_found": "Aucun titre du document ne correspond de manière unique ; rien n’a été écrit. Réessayez avec un titre renvoyé uniquement s’il correspond clairement ; sinon, demandez à l’utilisateur avec l’outil de clarification quelle section remplacer. Ne devinez pas.",
        "google_range_not_empty": "La plage cible contient déjà des données ; rien n’a été écrit. Avec l’outil de clarification, indiquez à l’utilisateur la plage et le nombre de cellules non vides qui seraient écrasées et demandez s’il faut les remplacer. N’appelez l’outil avec overwrite=true qu’après son accord ; sinon, écrivez dans une plage vide.",
        "google_no_edit_access": "L’utilisateur peut seulement consulter ce fichier ou ne peut pas le modifier ; rien n’a été écrit. Indiquez-le clairement ou créez plutôt un nouveau fichier.",
        "google_capability_disabled": "Un administrateur d’équipe a désactivé la modification des fichiers existants ; seuls les fichiers choisis avec « Sélectionner dans Google Drive » ou créés par Bowhead peuvent être modifiés. Rien n’a été écrit. Créez plutôt un nouveau fichier ou demandez à l’utilisateur de contacter un administrateur d’équipe.",
        "google_revision_conflict": "Le document a été modifié par une autre personne juste avant l’écriture ; rien n’a été écrit. Relisez-le avant d’écrire ; si la modification peut affecter votre changement, demandez d’abord à l’utilisateur avec l’outil de clarification.",
        "google_outcome_unknown": "La demande d’écriture a été envoyée, mais son résultat est inconnu. Aucune nouvelle tentative automatique ne sera effectuée. Demandez à l’utilisateur de vérifier dans Google ; ne prétendez pas que le contenu a été écrit.",
        "google_quota_exceeded": "Google limite temporairement les requêtes ; rien n’a été écrit. Réessayez plus tard.",
        "google_unreachable": "Impossible de joindre Google ; rien n’a été écrit. Réessayez plus tard.",
        "google_operation_failed": "L’écriture a échoué ; rien n’a été écrit. Indiquez-le clairement à l’utilisateur.",
    },
}


def _failure(kind: str, **extra: Any) -> dict[str, Any]:
    message = _MESSAGES.get(kind, _MESSAGES["google_operation_failed"])
    return {"ok": False, "error": kind, "message": message + " 只有返回 ok=true 才表示已写入。", **extra}


class _CallError(Exception):
    def __init__(self, status: int, reason: str = "", details: tuple[str, ...] = (), message: str = "") -> None:
        super().__init__(status)
        self.status = status
        self.reason = reason
        self.details = details
        self.message = message


async def _call(
    token: str,
    method: str,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    json_body: Any = None,
    content: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    request_headers = {"Authorization": f"Bearer {token}", "Accept": "application/json", **(headers or {})}
    async with http_client() as client:
        response = await client.request(method, url, params=params, json=json_body, content=content, headers=request_headers)
    if not response.is_success:
        reason, details, message = "", (), ""
        try:
            error = response.json().get("error") or {}
            errors = error.get("errors") or []
            reason = str(errors[0].get("reason") or "") if errors else str(error.get("status") or "")
            # Docs/Slides v1 put the specific cause in details[].reason (e.g. ACCESS_TOKEN_SCOPE_INSUFFICIENT).
            details = tuple(str(item.get("reason") or "") for item in error.get("details") or [] if isinstance(item, dict))
            message = str(error.get("message") or "")
        except (ValueError, AttributeError, IndexError):
            reason = ""
        raise _CallError(response.status_code, reason, details, message)
    try:
        payload = response.json()
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


_SCOPE_INSUFFICIENT = {"ACCESS_TOKEN_SCOPE_INSUFFICIENT", "insufficientScopes"}


def _forget_token(grant: Any) -> None:
    """A 401 means the cached access token is bad: drop it so the next call refreshes."""
    store = _store_provider() if _store_provider is not None else None
    if store is not None and grant is not None:
        token_broker(store.db_path).forget(grant.id)


def _read_error(exc: _CallError, grant: Any = None) -> dict[str, Any]:
    if exc.status == 401:
        # Not marked needs_reauth: only a refresh answering invalid_grant proves the grant is gone.
        _forget_token(grant)
        return _failure("google_reauth_required")
    if exc.status == 429 or exc.reason in {"rateLimitExceeded", "userRateLimitExceeded", "quotaExceeded", "RESOURCE_EXHAUSTED"}:
        return _failure("google_quota_exceeded")
    if exc.status == 403 and (_SCOPE_INSUFFICIENT & ({exc.reason} | set(exc.details))):
        return _failure("google_scope_missing")
    if exc.status in {403, 404}:
        return _failure("google_file_not_granted" if exc.status == 403 else "google_file_not_found")
    if exc.status >= 500:
        return _failure("google_unreachable")
    return _failure("google_operation_failed")


def _is_revision_conflict(exc: _CallError) -> bool:
    # The documented shape is "400 bad request"; accept the precondition status or a revision message.
    return exc.status == 400 and (exc.reason in {"FAILED_PRECONDITION", "failedPrecondition"} or "revision" in exc.message.lower())


def _write_error(exc: Exception, grant: Any = None) -> dict[str, Any]:
    """Failures after dispatch. Google batch writes are atomic, so a 4xx means nothing was written."""
    if isinstance(exc, _CallError):
        if _is_revision_conflict(exc):
            return _failure("google_revision_conflict")
        if exc.status == 403 and not (_SCOPE_INSUFFICIENT & ({exc.reason} | set(exc.details))):
            return _failure("google_no_edit_access")
        if exc.status in {400, 401, 403, 404, 429}:
            return _read_error(exc, grant) if exc.status != 400 else _failure("google_operation_failed")
    return _failure("google_outcome_unknown")


# --------------------------------------------------------------------------- A1 helpers


def _col_index(letters: str) -> int:
    value = 0
    for char in letters:
        value = value * 26 + (ord(char) - 64)
    return value


def _col_letters(index: int) -> str:
    out = ""
    while index:
        index, rem = divmod(index - 1, 26)
        out = chr(65 + rem) + out
    return out


def _a1_block(sheet: str, start_cell: str, rows: list[list[Any]]) -> str | None:
    match = _CELL.match(start_cell.upper())
    if not match:
        return None
    width = max((len(row) for row in rows), default=1) or 1
    first_col, first_row = _col_index(match.group(1)), int(match.group(2))
    end = f"{_col_letters(first_col + width - 1)}{first_row + len(rows) - 1}"
    return f"'{sheet.replace(chr(39), chr(39) * 2)}'!{match.group(1)}{first_row}:{end}"


# --------------------------------------------------------------------------- shared gate


async def _prepare(ctx: ToolExecutionContext) -> tuple[Any, str, Any] | dict[str, Any]:
    """(connection, access token, grant) or a failure result."""
    store = _store_provider() if _store_provider is not None else None
    team_id = str(getattr(ctx, "team_id", "") or "")
    if not feature_enabled() or store is None or not team_id:
        return _failure("google_connection_unavailable")
    tool = await store.get_team_builtin_tool(team_id, PROVIDER)
    if tool is None or tool.status != "enabled":
        return _failure("google_connection_unavailable")
    if not getattr(ctx, "config_lite_supported", False):
        return _failure("google_web_only")
    if not await store.user_has_active_team_membership(ctx.user_id, team_id):
        return _failure("google_connection_unavailable")
    auth = await store.get_team_builtin_tool_instance_auth_decrypted(team_id, tool.id)
    try:
        token, grant = await token_broker(store.db_path).access_token(
            tool=tool, auth=auth, user_id=ctx.user_id, required_bundles=frozenset({BASE_BUNDLE})
        )
    except GoogleWorkspaceError as exc:
        return _failure(exc.code)
    return tool, token, grant


async def _per_file_granted(token: str, file_id: str) -> bool:
    """Whether Bowhead holds per-file access (drive.file): the member picked the file or Bowhead created it.

    The access token never carries a Drive-wide scope, so Drive metadata only answers for such files.
    """
    try:
        await _call(token, "GET", f"https://www.googleapis.com/drive/v3/files/{file_id}", params={"fields": "id", "supportsAllDrives": "true"})
    except (_CallError, httpx2.HTTPError):
        return False
    return True


async def _edit_allowed(tool: Any, bundle: str, token: str, file_id: str) -> dict[str, Any] | None:
    """Enforce a disabled capability bundle server-side: only picked or Bowhead-created files stay editable."""
    if bundle in enabled_bundles(tool.config) or await _per_file_granted(token, file_id):
        return None
    return _failure("google_capability_disabled")


async def _numbers_ok(text: str, ctx: ToolExecutionContext) -> dict[str, Any] | None:
    from bowhead_core.send_email_tool import unsourced_turn_quantities

    unsourced = await unsourced_turn_quantities(text, ctx)
    if not unsourced:
        return None
    listed = "、".join(unsourced[:10])
    return {
        "ok": False,
        "error": "google_unsourced_numbers",
        "unverified_numbers": unsourced[:10],
        "message": (
            f"未写入：以下数字在本轮取得的数据中找不到来源：{listed}。请回到本轮工具返回的数据逐一核对并改成数据里的数值；"
            "合计、差值等派生数先用计算工具算出；无法核实的数字请删去。修正后必须再次调用本工具；若最终无法写入，必须明确告诉用户未写入及原因。"
        ),
    }


async def _show_in_canvas(
    ctx: ToolExecutionContext,
    token: str,
    grant: Any,
    *,
    file_kind: str,
    file_id: str,
    title: str,
    open_url: str,
    rows: list[list[Any]] | None = None,
    a1_range: str = "",
    written: dict[str, Any] | None = None,
) -> bool:
    """Card in the report canvas: link, snapshot, optional live preview. Best effort.

    New files snapshot the whole file (it is all Bowhead's content). Edits of existing files
    snapshot only what was written: a shared report must not reveal the rest of a file the
    viewer may have no Google access to.
    """
    from bowhead_core import google_canvas_preview as preview

    snapshot = written if written is not None else await preview.read_snapshot(token, file_kind, file_id, rows, a1_range)
    payload = preview.card_payload(
        file_kind=file_kind,
        file_id=file_id,
        title=title,
        open_url=open_url,
        account_email=str(getattr(grant, "provider_email", "") or ""),
        snapshot=snapshot,
    )
    return await preview.publish(ctx, payload)


def _file_id(args: dict[str, Any], kind: str) -> str | dict[str, Any]:
    parsed = parse_file_reference(str(args.get("file") or ""))
    if parsed is None or (parsed[0] is not None and parsed[0] != kind):
        return _failure("google_invalid_reference")
    return parsed[1]


# --------------------------------------------------------------------------- Docs


async def _doc(args: dict[str, Any], ctx: ToolExecutionContext) -> dict[str, Any]:
    op = str(args.get("op") or "")
    markdown = str(args.get("markdown") or "")
    if op not in {"create", "append", "replace_section", "replace_text"}:
        return _failure("google_invalid_arguments")
    if op in {"create", "append", "replace_section"} and (not markdown.strip() or len(markdown) > _MARKDOWN_MAX):
        return _failure("google_invalid_arguments")
    if op == "replace_text" and (
        not str(args.get("find") or "") or len(str(args.get("find"))) > 1000 or len(str(args.get("replace") or "")) > 10_000
    ):
        return _failure("google_invalid_arguments")
    prepared = await _prepare(ctx)
    if isinstance(prepared, dict):
        return prepared
    tool, token, grant = prepared
    checked_text = str(args.get("replace") or "") if op == "replace_text" else plain_text(markdown)
    if (numbers := await _numbers_ok(checked_text, ctx)) is not None:
        return numbers

    if op == "create":
        title = str(args.get("title") or "").strip()[:_TITLE_MAX]
        if not title:
            return _failure("google_invalid_arguments")
        boundary = f"bowhead-{uuid.uuid4().hex}"
        metadata = json.dumps({"name": title, "mimeType": "application/vnd.google-apps.document"})
        body = (
            f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{metadata}\r\n"
            f"--{boundary}\r\nContent-Type: text/markdown; charset=UTF-8\r\n\r\n{markdown}\r\n--{boundary}--"
        ).encode()
        try:
            created = await _call(
                token,
                "POST",
                "https://www.googleapis.com/upload/drive/v3/files",
                params={"uploadType": "multipart", "fields": "id,name,webViewLink"},
                content=body,
                headers={"Content-Type": f"multipart/related; boundary={boundary}"},
            )
        except Exception as exc:
            return _write_error(exc, grant)
        file_id = str(created.get("id") or "")
        url = created.get("webViewLink") or f"https://docs.google.com/document/d/{file_id}"
        name = created.get("name") or title
        shown = await _show_in_canvas(ctx, token, grant, file_kind="document", file_id=file_id, title=name, open_url=url)
        return {"ok": True, "op": op, "file_id": file_id, "title": name, "url": url, "shown_in_canvas": shown}

    file_id = _file_id(args, "document")
    if isinstance(file_id, dict):
        return file_id
    if (denied := await _edit_allowed(tool, "edit_docs", token, file_id)) is not None:
        return denied
    try:
        document = await _call(token, "GET", f"https://docs.googleapis.com/v1/documents/{file_id}", params={"includeTabsContent": "true"})
    except _CallError as exc:
        return _read_error(exc, grant)
    except httpx2.HTTPError:
        return _failure("google_unreachable")
    title = str(document.get("title") or "")
    revision = str(document.get("revisionId") or "")
    tab_id = document_tab_id(str(args.get("file") or ""))
    selected = select_document_tab(document, tab_id)
    if selected is None:
        return _failure("google_tab_required" if tab_id is None else "google_invalid_reference", tabs=document_tab_choices(document, file_id))
    tab_id = str((selected.get("tabProperties") or {}).get("tabId") or "") or tab_id
    tab_content = selected.get("documentTab", selected)
    url = f"https://docs.google.com/document/d/{file_id}"
    if tab_id is not None:
        url = next(choice["url"] for choice in document_tab_choices(document, file_id) if choice["id"] == tab_id)
    target = {"file_id": file_id, "title": title, "url": url}
    if op == "append":
        requests, _ = insert_requests(markdown, body_end_index(tab_content), leading_newline=True)
    elif op == "replace_section":
        section, headings = find_section(tab_content, str(args.get("heading") or ""))
        if section is None:
            return _failure("google_heading_not_found", headings=headings[:50])
        requests = []
        if section.content_start > body_end_index(tab_content):
            # The heading is the last paragraph: insert inside it (before its newline) as a new paragraph.
            inserts, _ = insert_requests(markdown, body_end_index(tab_content), leading_newline=True)
        else:
            if section.content_end > section.content_start:
                requests.append({"deleteContentRange": {"range": {"startIndex": section.content_start, "endIndex": section.content_end}}})
            inserts, _ = insert_requests(markdown, section.content_start, leading_newline=False)
        requests.extend(inserts)
    else:
        requests = [
            {
                "replaceAllText": {
                    "containsText": {"text": str(args["find"]), "matchCase": bool(args.get("match_case", True))},
                    "replaceText": str(args.get("replace") or ""),
                }
            }
        ]
    if tab_id is not None:
        for request in requests:
            operation = next(iter(request.values()))
            if "replaceAllText" in request:
                operation["tabsCriteria"] = {"tabIds": [tab_id]}
            else:
                (operation.get("range") or operation.get("location"))["tabId"] = tab_id
    try:
        result = await _call(
            token,
            "POST",
            f"https://docs.googleapis.com/v1/documents/{file_id}:batchUpdate",
            json_body={"requests": requests, "writeControl": {"requiredRevisionId": revision}},
        )
    except Exception as exc:
        return _write_error(exc, grant)
    out: dict[str, Any] = {"ok": True, "op": op, **target}
    if op == "replace_text":
        out["occurrences_changed"] = int(((result.get("replies") or [{}])[0].get("replaceAllText") or {}).get("occurrencesChanged") or 0)
    out["shown_in_canvas"] = await _show_in_canvas(
        ctx,
        token,
        grant,
        file_kind="document",
        file_id=file_id,
        title=title,
        open_url=target["url"],
        written=_written_snapshot(op, args, markdown, out),
    )
    return out


def _written_snapshot(op: str, args: dict[str, Any], markdown: str, out: dict[str, Any]) -> dict[str, Any]:
    if op == "replace_text":
        text = f"{args.get('find')} → {args.get('replace') or ''} ({out.get('occurrences_changed', 0)})"
        return {"format": "markdown", "content": text, "truncated": False, "part": "written"}
    content = markdown[:20_000]
    return {"format": "markdown", "content": content, "truncated": len(markdown) > 20_000, "part": "written"}


# --------------------------------------------------------------------------- Slides


def _slides_arg(args: dict[str, Any]) -> list[dict[str, Any]] | None:
    slides = args.get("slides")
    if not isinstance(slides, list) or not 1 <= len(slides) <= _MAX_SLIDES:
        return None
    out: list[dict[str, Any]] = []
    for slide in slides:
        if not isinstance(slide, dict):
            return None
        title = str(slide.get("title") or "").strip()[:_TITLE_MAX]
        bullets = [str(item).strip() for item in slide.get("bullets") or [] if str(item).strip()][:_MAX_BULLETS]
        if not title and not bullets:
            return None
        out.append({"title": title, "bullets": bullets})
    return out


def _slide_requests(slides: list[dict[str, Any]]) -> list[dict[str, Any]]:
    requests: list[dict[str, Any]] = []
    for slide in slides:
        suffix = uuid.uuid4().hex[:12]
        slide_id, title_id, body_id = f"bh_s_{suffix}", f"bh_t_{suffix}", f"bh_b_{suffix}"
        requests.append(
            {
                "createSlide": {
                    "objectId": slide_id,
                    "slideLayoutReference": {"predefinedLayout": "TITLE_AND_BODY"},
                    "placeholderIdMappings": [
                        {"layoutPlaceholder": {"type": "TITLE", "index": 0}, "objectId": title_id},
                        {"layoutPlaceholder": {"type": "BODY", "index": 0}, "objectId": body_id},
                    ],
                }
            }
        )
        if slide["title"]:
            requests.append({"insertText": {"objectId": title_id, "text": slide["title"]}})
        if slide["bullets"]:
            requests.append({"insertText": {"objectId": body_id, "text": "\n".join(slide["bullets"])}})
            requests.append(
                {"createParagraphBullets": {"objectId": body_id, "textRange": {"type": "ALL"}, "bulletPreset": "BULLET_DISC_CIRCLE_SQUARE"}}
            )
    return requests


async def _slides(args: dict[str, Any], ctx: ToolExecutionContext) -> dict[str, Any]:
    op = str(args.get("op") or "")
    if op not in {"create", "append", "replace_text"}:
        return _failure("google_invalid_arguments")
    slides = _slides_arg(args) if op != "replace_text" else []
    if slides is None or (op == "replace_text" and not str(args.get("find") or "")):
        return _failure("google_invalid_arguments")
    prepared = await _prepare(ctx)
    if isinstance(prepared, dict):
        return prepared
    tool, token, grant = prepared
    outline = "\n".join([s["title"] for s in slides] + [b for s in slides for b in s["bullets"]])
    if (numbers := await _numbers_ok(str(args.get("replace") or "") if op == "replace_text" else outline, ctx)) is not None:
        return numbers

    if op == "create":
        title = str(args.get("title") or "").strip()[:_TITLE_MAX]
        if not title:
            return _failure("google_invalid_arguments")
        try:
            created = await _call(token, "POST", "https://slides.googleapis.com/v1/presentations", json_body={"title": title})
        except Exception as exc:
            return _write_error(exc, grant)
        file_id = str(created.get("presentationId") or "")
        url = f"https://docs.google.com/presentation/d/{file_id}"
        placeholder = [str(s.get("objectId")) for s in created.get("slides") or [] if s.get("objectId")]
        requests = _slide_requests(slides) + [{"deleteObject": {"objectId": object_id}} for object_id in placeholder]
        try:
            await _call(token, "POST", f"https://slides.googleapis.com/v1/presentations/{file_id}:batchUpdate", json_body={"requests": requests})
        except Exception as exc:
            # The empty presentation already exists: say where it is so the user can fill or delete it.
            return {**_write_error(exc, grant), "created_file_id": file_id, "created_url": url}
        shown = await _show_in_canvas(ctx, token, grant, file_kind="presentation", file_id=file_id, title=title, open_url=url)
        return {"ok": True, "op": op, "file_id": file_id, "title": title, "url": url, "slide_count": len(slides), "shown_in_canvas": shown}

    file_id = _file_id(args, "presentation")
    if isinstance(file_id, dict):
        return file_id
    if (denied := await _edit_allowed(tool, "edit_docs", token, file_id)) is not None:
        return denied
    try:
        presentation = await _call(
            token, "GET", f"https://slides.googleapis.com/v1/presentations/{file_id}", params={"fields": "title,revisionId,slides.objectId"}
        )
    except _CallError as exc:
        return _read_error(exc, grant)
    except httpx2.HTTPError:
        return _failure("google_unreachable")
    target = {"file_id": file_id, "title": str(presentation.get("title") or ""), "url": f"https://docs.google.com/presentation/d/{file_id}"}
    if op == "append":
        requests = _slide_requests(slides)
    else:
        requests = [
            {
                "replaceAllText": {
                    "containsText": {"text": str(args["find"]), "matchCase": bool(args.get("match_case", True))},
                    "replaceText": str(args.get("replace") or ""),
                }
            }
        ]
    try:
        result = await _call(
            token,
            "POST",
            f"https://slides.googleapis.com/v1/presentations/{file_id}:batchUpdate",
            json_body={"requests": requests, "writeControl": {"requiredRevisionId": str(presentation.get("revisionId") or "")}},
        )
    except Exception as exc:
        return _write_error(exc, grant)
    out: dict[str, Any] = {"ok": True, "op": op, **target}
    if op == "replace_text":
        out["occurrences_changed"] = int(((result.get("replies") or [{}])[0].get("replaceAllText") or {}).get("occurrencesChanged") or 0)
    else:
        out["slides_added"] = len(slides)
    written = _written_snapshot("replace_text", args, "", out) if op == "replace_text" else _written_snapshot("append", args, outline, out)
    out["shown_in_canvas"] = await _show_in_canvas(
        ctx, token, grant, file_kind="presentation", file_id=file_id, title=target["title"], open_url=target["url"], written=written
    )
    return out


# --------------------------------------------------------------------------- Sheets


async def _sheet(args: dict[str, Any], ctx: ToolExecutionContext) -> dict[str, Any]:
    rows = args.get("rows")
    if not isinstance(rows, list) or not rows or len(rows) > _MAX_ROWS or not all(isinstance(row, list) and len(row) <= _MAX_COLS for row in rows):
        return _failure("google_invalid_arguments")
    width = max(len(row) for row in rows)
    if not width:
        return _failure("google_invalid_arguments")
    # Google skips null and omitted cells. Explicit blanks keep the written rectangle and its snapshot identical.
    rows = [
        ["" if cell is None else cell if isinstance(cell, (int, float, bool)) else str(cell) for cell in row] + [""] * (width - len(row))
        for row in rows
    ]
    requested_sheet = str(args.get("sheet") or "").strip()[:100]
    sheet = requested_sheet or "Sheet1"
    start_cell = str(args.get("start_cell") or "A1").strip().upper()
    a1 = _a1_block(sheet, start_cell, rows)
    if a1 is None:
        return _failure("google_invalid_arguments")
    prepared = await _prepare(ctx)
    if isinstance(prepared, dict):
        return prepared
    tool, token, grant = prepared
    cells_text = "\n".join("\t".join("" if cell is None else str(cell) for cell in row) for row in rows)
    if (numbers := await _numbers_ok(cells_text, ctx)) is not None:
        return numbers

    reference = str(args.get("file") or "").strip()
    if not reference:
        title = str(args.get("title") or "").strip()[:_TITLE_MAX]
        if not title:
            return _failure("google_invalid_arguments")
        try:
            created = await _call(
                token,
                "POST",
                "https://sheets.googleapis.com/v4/spreadsheets",
                json_body={"properties": {"title": title}, "sheets": [{"properties": {"title": sheet}}]},
            )
        except Exception as exc:
            return _write_error(exc, grant)
        file_id = str(created.get("spreadsheetId") or "")
        url = f"https://docs.google.com/spreadsheets/d/{file_id}"
        try:
            await _call(
                token,
                "PUT",
                f"https://sheets.googleapis.com/v4/spreadsheets/{file_id}/values/{quote(a1, safe=chr(39) + '!:')}",
                params={"valueInputOption": "RAW"},
                json_body={"range": a1, "majorDimension": "ROWS", "values": rows},
            )
        except Exception as exc:
            # The empty spreadsheet already exists: say where it is so the user can fill or delete it.
            return {**_write_error(exc, grant), "created_file_id": file_id, "created_url": url}
        shown = await _show_in_canvas(ctx, token, grant, file_kind="spreadsheet", file_id=file_id, title=title, open_url=url, rows=rows, a1_range=a1)
        return {
            "ok": True,
            "op": "create",
            "file_id": file_id,
            "title": title,
            "url": url,
            "range": a1,
            "rows_written": len(rows),
            "shown_in_canvas": shown,
        }

    file_id = _file_id(args, "spreadsheet")
    if isinstance(file_id, dict):
        return file_id
    if (denied := await _edit_allowed(tool, "edit_sheets", token, file_id)) is not None:
        return denied
    base = f"https://sheets.googleapis.com/v4/spreadsheets/{file_id}"
    try:
        meta = await _call(token, "GET", base, params={"fields": "properties.title,sheets.properties.title"})
        if not requested_sheet:
            # Default to the first tab: its name is localized ("Sheet1", "工作表1", ...).
            first = ((meta.get("sheets") or [{}])[0].get("properties") or {}).get("title")
            a1 = _a1_block(str(first or "Sheet1"), start_cell, rows) or a1
        existing = await _call(token, "GET", f"{base}/values/{quote(a1, safe=chr(39) + '!:')}", params={"valueRenderOption": "FORMULA"})
    except _CallError as exc:
        return _read_error(exc, grant)
    except httpx2.HTTPError:
        return _failure("google_unreachable")
    non_empty = sum(1 for row in existing.get("values") or [] for cell in row if cell is not None and cell != "")
    overwrite = bool(args.get("overwrite"))
    if non_empty and not overwrite:
        return _failure("google_range_not_empty", range=a1, non_empty_cells=non_empty)
    target = {
        "file_id": file_id,
        "title": str((meta.get("properties") or {}).get("title") or ""),
        "url": f"https://docs.google.com/spreadsheets/d/{file_id}",
    }
    try:
        await _call(
            token,
            "PUT",
            f"{base}/values/{quote(a1, safe=chr(39) + '!:')}",
            params={"valueInputOption": "RAW"},
            json_body={"range": a1, "majorDimension": "ROWS", "values": rows},
        )
    except Exception as exc:
        return _write_error(exc, grant)
    shown = await _show_in_canvas(
        ctx, token, grant, file_kind="spreadsheet", file_id=file_id, title=target["title"], open_url=target["url"], rows=rows, a1_range=a1
    )
    return {"ok": True, "op": "write", **target, "range": a1, "rows_written": len(rows), "overwrote_cells": non_empty, "shown_in_canvas": shown}


# --------------------------------------------------------------------------- registration


def _localized_result(result: dict[str, Any], ctx: ToolExecutionContext) -> dict[str, Any]:
    from bowhead_core.presentation_locale import resolve_presentation_locale

    context = getattr(ctx, "language_context", None)
    lang = resolve_presentation_locale(getattr(context, "output_language", None), getattr(context, "ui_language", None))
    if result.get("ok") is True or lang == "zh-CN":
        return result
    code = str(result.get("error") or "")
    messages = _LOCALIZED_MESSAGES[lang]
    message = messages.get(code, messages["google_operation_failed"])
    if code == "google_unsourced_numbers":
        listed = ", ".join(str(value) for value in result.get("unverified_numbers", []))
        message = {
            "en-US": "Nothing was written. These numbers have no source in data retrieved this turn: {numbers}. Check each against tool data from this turn and use those values. Calculate totals and differences with a calculation tool first; remove unverifiable numbers. After correcting, call this tool again. If writing remains impossible, explicitly tell the user nothing was written and why.",
            "de-DE": "Es wurde nichts geschrieben. Für diese Zahlen fehlt eine Quelle in den Daten dieser Gesprächsrunde: {numbers}. Gleichen Sie jede Zahl mit den Tooldaten dieser Runde ab und verwenden Sie die dort enthaltenen Werte. Berechnen Sie Summen und Differenzen zunächst mit einem Berechnungstool; entfernen Sie nicht verifizierbare Zahlen. Rufen Sie das Tool nach der Korrektur erneut auf. Ist das Schreiben weiterhin nicht möglich, teilen Sie dem Nutzer ausdrücklich mit, dass nichts geschrieben wurde, und nennen Sie den Grund.",
            "fr-FR": "Rien n’a été écrit. Ces nombres n’ont pas de source dans les données récupérées pendant cet échange : {numbers}. Vérifiez chacun dans les données des outils de cet échange et utilisez ces valeurs. Calculez d’abord les totaux et différences avec un outil de calcul ; supprimez les nombres invérifiables. Après correction, appelez à nouveau cet outil. Si l’écriture reste impossible, indiquez explicitement à l’utilisateur que rien n’a été écrit et pourquoi.",
        }[lang].format(numbers=listed)
    result["message"] = (
        message
        + " "
        + {
            "en-US": "Only ok=true means the content was written.",
            "de-DE": "Nur ok=true bedeutet, dass der Inhalt geschrieben wurde.",
            "fr-FR": "Seul ok=true signifie que le contenu a été écrit.",
        }[lang]
    )
    return result


def _guard(handler: Callable[[dict[str, Any], ToolExecutionContext], Any]) -> Callable[[dict[str, Any], ToolExecutionContext], Any]:
    async def run(args: dict[str, Any], ctx: ToolExecutionContext) -> dict[str, Any]:
        try:
            result = await handler(args, ctx)
        except GoogleWorkspaceError as exc:
            result = _failure(exc.code)
        except Exception as exc:  # detail stays server-side
            logger.warning("google_delivery.failed err=%s", type(exc).__name__)
            result = _failure("google_operation_failed")
        result.setdefault("provider", PROVIDER)
        return _localized_result(result, ctx)

    return run


_COMMON = (
    "以当前用户本人的 Google 权限写入；仅在用户明确要求写到 Google 时调用，写入不会再请用户确认。"
    "目标文件、要替换的章节或是否覆盖已有数据不明确时，先用澄清工具问用户，不要猜。"
    "内容里的金额、百分比等数字必须逐字取自本轮工具返回的数据。只有返回 ok=true 才表示已写入，否则必须如实告诉用户没有写入。"
)


def _schemas() -> list[ToolSchema]:
    file_prop = {"type": "string", "maxLength": 500, "description": "用户给出的 Google 文件链接或 ID"}
    return [
        ToolSchema(
            name=DOC_TOOL,
            description=(
                "Create a Google Doc from markdown, append text, replace a uniquely matching heading's section, or replace matching text. "
                "For multi-tab documents use a file link containing ?tab=<tabId>; edits affect only that tab. "
                "Without a selected tab, the tool returns tab links for clarification. Tables in existing Docs become tab-separated text. " + _COMMON
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "op": {"type": "string", "enum": ["create", "append", "replace_section", "replace_text"]},
                    "title": {"type": "string", "maxLength": _TITLE_MAX, "description": "create 时必填"},
                    "file": file_prop,
                    "markdown": {"type": "string", "maxLength": _MARKDOWN_MAX},
                    "heading": {"type": "string", "maxLength": 500},
                    "find": {"type": "string", "maxLength": 1000},
                    "replace": {"type": "string", "maxLength": 10_000},
                    "match_case": {"type": "boolean"},
                },
                "required": ["op"],
                "additionalProperties": False,
            },
        ),
        ToolSchema(
            name=SLIDES_TOOL,
            description="新建 Google 演示文稿、在已有演示末尾追加页（每页标题 + 要点），或全文查找替换。" + _COMMON,
            input_schema={
                "type": "object",
                "properties": {
                    "op": {"type": "string", "enum": ["create", "append", "replace_text"]},
                    "title": {"type": "string", "maxLength": _TITLE_MAX},
                    "file": file_prop,
                    "slides": {
                        "type": "array",
                        "maxItems": _MAX_SLIDES,
                        "items": {
                            "type": "object",
                            "properties": {
                                "title": {"type": "string"},
                                "bullets": {"type": "array", "items": {"type": "string"}, "maxItems": _MAX_BULLETS},
                            },
                            "additionalProperties": False,
                        },
                    },
                    "find": {"type": "string", "maxLength": 1000},
                    "replace": {"type": "string", "maxLength": 10_000},
                    "match_case": {"type": "boolean"},
                },
                "required": ["op"],
                "additionalProperties": False,
            },
        ),
        ToolSchema(
            name=SHEET_TOOL,
            description=(
                "把表格数据写进 Google 表格：不给 file 时新建（需 title）；给 file 时写入指定区域。值按原样写入，不解析公式。"
                "目标区域已有数据时会拒绝，需先用澄清工具问用户，同意后再以 overwrite=true 重试。" + _COMMON
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "file": file_prop,
                    "title": {"type": "string", "maxLength": _TITLE_MAX},
                    "sheet": {"type": "string", "maxLength": 100},
                    "start_cell": {"type": "string", "maxLength": 10, "description": "例如 A1"},
                    "rows": {"type": "array", "maxItems": _MAX_ROWS, "items": {"type": "array", "maxItems": _MAX_COLS}},
                    "overwrite": {"type": "boolean"},
                },
                "required": ["rows"],
                "additionalProperties": False,
            },
        ),
    ]


def register_google_delivery_tools(registry: ToolRegistry, *, store_provider: Callable[[], Any | None] | None = None) -> None:
    """Always registered (like send_email); advertised only when the team connection is enabled."""
    set_store_provider(store_provider)
    handlers = {DOC_TOOL: _doc, SLIDES_TOOL: _slides, SHEET_TOOL: _sheet}
    for schema in _schemas():
        registry.register(ToolSpec(schema=schema, handler=_guard(handlers[schema.name]), risk_level="medium", allowed_for_roles=frozenset({"lead"})))


async def delivery_tools_unavailable(store: Any, team_id: str, *, attended: bool | None) -> frozenset[str]:
    """Delivery tools to hide from this turn's advertising (same readiness as execution)."""
    if attended is False or not feature_enabled() or store is None or not team_id:
        return frozenset(TOOL_NAMES)
    try:
        tool = await store.get_team_builtin_tool(team_id, PROVIDER)
    except Exception:
        logger.warning("google_delivery.readiness_failed team=%s", team_id, exc_info=True)
        return frozenset(TOOL_NAMES)
    return frozenset() if tool is not None and tool.status == "enabled" else frozenset(TOOL_NAMES)


# --------------------------------------------------------------------------- turn-level notice

_UNDELIVERED = {
    "zh-CN": ("**Google 未写入**", "{what}：{reason}。", "请到 Google 中核实该文件，系统不会自动重试。"),
    "en-US": ("**Not written to Google**", "{what}: {reason}.", "Check the file in Google; nothing will be retried automatically."),
    "de-DE": (
        "**Nicht in Google gespeichert**",
        "{what}: {reason}.",
        "Prüfen Sie die Datei in Google; es erfolgt kein automatischer erneuter Versuch.",
    ),
    "fr-FR": (
        "**Non enregistré dans Google**",
        "{what} : {reason}.",
        "Vérifiez le fichier dans Google ; aucune nouvelle tentative ne sera effectuée automatiquement.",
    ),
}
_REASONS = {
    "zh-CN": {
        "google_unsourced_numbers": "部分数字没能与本轮取得的数据核对上",
        "google_web_only": "Google 写入只在网页对话中进行，请在网页端对话中重新发起",
        "google_not_connected": "你尚未连接 Google，请到「我的连接」连接后再试",
        "google_reauth_required": "你的 Google 授权已过期，请到「我的连接」重新连接",
        "google_scope_missing": "你的 Google 授权尚未包含该能力，请到「我的连接」更新授权",
        "google_no_edit_access": "你对这个文件只有查看权限",
        "google_capability_disabled": "团队管理员关闭了修改已有文件的能力",
        "connection_see_above": "Google 授权需要处理（见上）",
        "google_revision_conflict": "写入前文档刚被他人修改",
        "google_range_not_empty": "目标区域已有数据",
        "google_tab_required": "文档有多个标签页，需要先选择要修改的标签页",
        "google_outcome_unknown": "写入请求已发出但结果未知",
        "": "写入过程中出现问题",
    },
    "en-US": {
        "google_unsourced_numbers": "some numbers could not be matched to the data retrieved in this turn",
        "google_web_only": "Google writes only happen in web chats; ask again from the web chat",
        "google_not_connected": "your Google account is not connected; connect it under My connections",
        "google_reauth_required": "your Google authorization expired; reconnect under My connections",
        "google_scope_missing": "your Google authorization does not include this yet; update it under My connections",
        "google_no_edit_access": "you can only view this file",
        "google_capability_disabled": "a team admin turned off editing existing files",
        "connection_see_above": "your Google authorization needs attention (see above)",
        "google_revision_conflict": "someone changed the document just before the write",
        "google_range_not_empty": "the target range already has data",
        "google_tab_required": "the document has multiple tabs; choose the tab to edit first",
        "google_outcome_unknown": "the write was sent but its outcome is unknown",
        "": "something went wrong while writing",
    },
    "de-DE": {
        "google_unsourced_numbers": "einige Zahlen konnten nicht mit den in dieser Gesprächsrunde abgerufenen Daten abgeglichen werden",
        "google_web_only": "in Google wird nur in Web-Chats geschrieben; stellen Sie die Anfrage erneut im Webchat",
        "google_not_connected": "Ihr Google-Konto ist nicht verbunden; verbinden Sie es unter „Meine Verbindungen“",
        "google_reauth_required": "Ihre Google-Autorisierung ist abgelaufen; verbinden Sie Google unter „Meine Verbindungen“ erneut",
        "google_scope_missing": "Ihre Google-Autorisierung umfasst diese Funktion noch nicht; aktualisieren Sie sie unter „Meine Verbindungen“",
        "google_no_edit_access": "Sie dürfen diese Datei nur ansehen",
        "google_capability_disabled": "ein Teamadministrator hat das Bearbeiten vorhandener Dateien deaktiviert",
        "connection_see_above": "Ihre Google-Autorisierung erfordert eine Aktion (siehe oben)",
        "google_revision_conflict": "das Dokument wurde kurz vor dem Schreiben von einer anderen Person geändert",
        "google_range_not_empty": "der Zielbereich enthält bereits Daten",
        "google_tab_required": "das Dokument hat mehrere Tabs; wählen Sie zuerst den zu bearbeitenden Tab",
        "google_outcome_unknown": "die Schreibanfrage wurde gesendet, ihr Ergebnis ist jedoch unbekannt",
        "": "beim Schreiben ist ein Problem aufgetreten",
    },
    "fr-FR": {
        "google_unsourced_numbers": "certains nombres n’ont pas pu être rapprochés des données récupérées pendant cet échange",
        "google_web_only": "l’écriture dans Google n’a lieu que dans les conversations web ; reformulez votre demande depuis la conversation web",
        "google_not_connected": "votre compte Google n’est pas connecté ; connectez-le dans « Mes connexions »",
        "google_reauth_required": "votre autorisation Google a expiré ; reconnectez Google dans « Mes connexions »",
        "google_scope_missing": "votre autorisation Google n’inclut pas encore cette fonctionnalité ; mettez-la à jour dans « Mes connexions »",
        "google_no_edit_access": "vous pouvez seulement consulter ce fichier",
        "google_capability_disabled": "un administrateur d’équipe a désactivé la modification des fichiers existants",
        "connection_see_above": "votre autorisation Google demande une action (voir ci-dessus)",
        "google_revision_conflict": "le document a été modifié par une autre personne juste avant l’écriture",
        "google_range_not_empty": "la plage cible contient déjà des données",
        "google_tab_required": "le document comporte plusieurs onglets ; choisissez d’abord l’onglet à modifier",
        "google_outcome_unknown": "la demande d’écriture a été envoyée, mais son résultat est inconnu",
        "": "un problème est survenu pendant l’écriture",
    },
}

_WHAT = {
    "zh-CN": {DOC_TOOL: "Google 文档", SLIDES_TOOL: "Google 演示", SHEET_TOOL: "Google 表格"},
    "en-US": {DOC_TOOL: "Google Doc", SLIDES_TOOL: "Google Slides", SHEET_TOOL: "Google Sheet"},
    "de-DE": {DOC_TOOL: "Google-Dokument", SLIDES_TOOL: "Google-Präsentation", SHEET_TOOL: "Google-Tabelle"},
    "fr-FR": {DOC_TOOL: "Document Google", SLIDES_TOOL: "Présentation Google", SHEET_TOOL: "Feuille de calcul Google"},
}


_CONNECTION_CODES = frozenset({"google_not_connected", "google_reauth_required", "google_scope_missing"})


def _target_key(name: str, inv: Any) -> tuple[str, str]:
    """One outcome per tool and target file (new files are keyed by title)."""
    args = getattr(inv, "arguments", None)
    args = args if isinstance(args, dict) else {}
    reference = str(args.get("file") or "").strip()
    parsed = parse_file_reference(reference) if reference else None
    return name, parsed[1] if parsed else f"new:{str(args.get('title') or '').strip()}"


def undelivered_google_notice(invocations: Any, lang: str = "en-US", *, connection_noticed: bool = False) -> str:
    """User-facing "not written to Google" paragraph for the Lead's final answer.

    Owed when, for some file, the turn's last delivery call to it did not succeed (a later
    success on a different file does not hide it). Like the email notice, the model's
    closing prose is not trusted to disclose it. When the connection notice is already
    shown, connection reasons point to it instead of repeating the guidance.
    """
    last: dict[tuple[str, str], tuple[bool, str]] = {}
    for inv in list(invocations or []):
        name = str(getattr(inv, "tool_name", "") or "")
        if name not in TOOL_NAMES:
            continue
        result = getattr(inv, "result", None)
        ok = isinstance(result, dict) and result.get("ok") is True and not getattr(inv, "error", None)
        last[_target_key(name, inv)] = (ok, "" if ok or not isinstance(result, dict) else str(result.get("error") or ""))
    failed = [(name, code) for (name, _target), (ok, code) in last.items() if not ok]
    if not failed:
        return ""
    from bowhead_core.presentation_locale import resolve_presentation_locale

    lang = resolve_presentation_locale(lang)
    table = _UNDELIVERED[lang]
    reasons = _REASONS[lang]
    what = _WHAT[lang]

    def reason(code: str) -> str:
        if connection_noticed and code in _CONNECTION_CODES:
            return reasons["connection_see_above"]
        return reasons.get(code) or reasons[""]

    parts = [table[1].format(what=what[name], reason=reason(code)) for name, code in failed]
    separator = "：" if lang == "zh-CN" else " : " if lang == "fr-FR" else ": "
    text = table[0] + separator + " ".join(parts)
    if any(code == "google_outcome_unknown" for _name, code in failed):
        text += " " + table[2]
    return text
