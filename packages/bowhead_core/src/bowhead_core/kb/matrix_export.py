"""Excel export of a document matrix (document matrix step 1, S5).

The workbook is built only from the snapshot's per-viewer projection: ``columns``, ``rows`` and ``cells``
exactly as ``GET /matrices/{id}/snapshot`` returns them to the same caller. Rows the caller cannot read
arrive there with ``missing=True`` and no values, so they are written as a marker without values.

Three sheets, named in the requester's interface language:

- **Answers:** the file and one typed column per question (numbers, dates and booleans are real cells);
  verified and edited answers get a fill.
- **Sources:** one row per cell with file, column, question, answer, status, confidence, location and
  excerpt.
- **About:** matrix name, export time, exporter, counts, how many cells were hidden, and the status legend.

openpyxl runs in write-only mode. Every text cell is written with ``data_type="s"`` so text starting with
``=`` stays text (openpyxl would otherwise store it as a formula), characters a worksheet cannot hold are
dropped, and text is cut at ``MAX_CELL_TEXT_CHARS`` with a marker (Excel holds at most 32,767 characters
in a cell; an edited value may be longer).
"""

from __future__ import annotations

import io
import json
import math
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from openpyxl import Workbook  # type: ignore[import-untyped]
from openpyxl.cell import WriteOnlyCell  # type: ignore[import-untyped]
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE  # type: ignore[import-untyped]
from openpyxl.styles import Alignment, Font, PatternFill  # type: ignore[import-untyped]

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
MAX_CELL_TEXT_CHARS = 32_000
# Excel keeps 15 significant digits; larger integers stay text so no digit is silently changed.
_MAX_EXACT_INTEGER = 10**15
_EXCEL_FIRST_YEAR = 1900

FILL_VERIFIED = "E2F0D9"
FILL_EDITED = "FFF2CC"

# Statuses in the order the About sheet's legend lists them.
LEGEND_STATUSES = ("verified", "edited", "needs_review", "not_verified", "stale", "failed", "running", "not_run", "unavailable")
_ANSWERED = ("done", "needs_review")

_COPY: dict[str, dict[str, str]] = {
    "zh-CN": {
        "sheet_answers": "答案",
        "sheet_sources": "出处",
        "sheet_about": "说明",
        "file": "文件",
        "column": "列",
        "question": "问题",
        "answer": "答案",
        "status": "状态",
        "confidence": "置信度",
        "location": "位置",
        "excerpt": "原文摘录",
        "hidden_file": "（无权查看）",
        "page": "第 {n} 页",
        "paragraph": "第 {n} 段",
        "truncated": "…（已截断）",
        "list_separator": "、",
        "about_matrix": "矩阵",
        "about_exported_at": "导出时间",
        "about_exporter": "导出人",
        "about_files": "文件数",
        "about_columns": "问题列数",
        "about_hidden": "隐藏的格子",
        "about_hidden_note": "你无权查看或已删除的文件不导出值，文件名显示为「（无权查看）」。",
        "about_legend": "图例",
        "status_verified": "已核",
        "status_edited": "已编辑",
        "status_needs_review": "不符列类型",
        "status_not_verified": "未核",
        "status_stale": "已过期",
        "status_failed": "失败",
        "status_running": "处理中",
        "status_not_run": "未运行",
        "status_unavailable": "无权查看",
        "legend_verified": "有人核对并确认了答案，包括人工修改后再核对的。「答案」表中为绿色。",
        "legend_edited": "有人填写或修改了答案，还没有核对。「答案」表中为黄色。",
        "legend_needs_review": "抽取到的内容不符合列类型，按原文保存为文本，需要人工处理。",
        "legend_not_verified": "AI 给出的答案，还没有人核对。",
        "legend_stale": "抽取后文档或问题有变化，答案可能过时，可在矩阵里重跑。",
        "legend_failed": "抽取失败，没有答案。",
        "legend_running": "导出时仍在处理。",
        "legend_not_run": "这个问题还没有处理。",
        "legend_unavailable": "你无权查看该文件，或文件已删除，不导出它的值。",
    },
    "en-US": {
        "sheet_answers": "Answers",
        "sheet_sources": "Sources",
        "sheet_about": "About",
        "file": "File",
        "column": "Column",
        "question": "Question",
        "answer": "Answer",
        "status": "Status",
        "confidence": "Confidence",
        "location": "Location",
        "excerpt": "Source excerpt",
        "hidden_file": "(No access)",
        "page": "Page {n}",
        "paragraph": "Paragraph {n}",
        "truncated": "… (truncated)",
        "list_separator": ", ",
        "about_matrix": "Matrix",
        "about_exported_at": "Exported at",
        "about_exporter": "Exported by",
        "about_files": "Files",
        "about_columns": "Question columns",
        "about_hidden": "Hidden cells",
        "about_hidden_note": 'Files you can\'t view, or that were deleted, are exported without values and shown as "(No access)".',
        "about_legend": "Legend",
        "status_verified": "Verified",
        "status_edited": "Edited",
        "status_needs_review": "Type mismatch",
        "status_not_verified": "Not verified",
        "status_stale": "Stale",
        "status_failed": "Failed",
        "status_running": "Processing",
        "status_not_run": "Not run",
        "status_unavailable": "No access",
        "legend_verified": "A person checked and confirmed the answer, including answers edited by hand first. Green in Answers.",
        "legend_edited": "A person entered or changed the answer, and it hasn't been verified yet. Yellow in Answers.",
        "legend_needs_review": "The extracted content doesn't fit the column type and is kept as text for a person to review.",
        "legend_not_verified": "An AI answer nobody has checked yet.",
        "legend_stale": "The document or the question changed after extraction, so the answer may be out of date. Rerun it in the matrix.",
        "legend_failed": "Extraction failed, so there is no answer.",
        "legend_running": "Still being processed when the file was exported.",
        "legend_not_run": "This question hasn't been processed yet.",
        "legend_unavailable": "You can't view this file, or it was deleted, so its values are not exported.",
    },
    "de-DE": {
        "sheet_answers": "Antworten",
        "sheet_sources": "Quellen",
        "sheet_about": "Info",
        "file": "Datei",
        "column": "Spalte",
        "question": "Frage",
        "answer": "Antwort",
        "status": "Status",
        "confidence": "Konfidenz",
        "location": "Fundstelle",
        "excerpt": "Quellauszug",
        "hidden_file": "(Kein Zugriff)",
        "page": "Seite {n}",
        "paragraph": "Absatz {n}",
        "truncated": "… (gekürzt)",
        "list_separator": ", ",
        "about_matrix": "Matrix",
        "about_exported_at": "Exportiert am",
        "about_exporter": "Exportiert von",
        "about_files": "Dateien",
        "about_columns": "Fragespalten",
        "about_hidden": "Ausgeblendete Zellen",
        "about_hidden_note": "Dateien, die Sie nicht sehen können oder die gelöscht wurden, werden ohne Werte exportiert und als „(Kein Zugriff)“ angezeigt.",
        "about_legend": "Legende",
        "status_verified": "Verifiziert",
        "status_edited": "Bearbeitet",
        "status_needs_review": "Typabweichung",
        "status_not_verified": "Nicht verifiziert",
        "status_stale": "Veraltet",
        "status_failed": "Fehlgeschlagen",
        "status_running": "Wird verarbeitet",
        "status_not_run": "Nicht ausgeführt",
        "status_unavailable": "Kein Zugriff",
        "legend_verified": "Eine Person hat die Antwort geprüft und bestätigt, auch nach einer manuellen Bearbeitung. In „Antworten“ grün.",
        "legend_edited": "Eine Person hat die Antwort eingegeben oder geändert; sie ist noch nicht verifiziert. In „Antworten“ gelb.",
        "legend_needs_review": "Der extrahierte Inhalt passt nicht zum Spaltentyp und ist als Text zur Prüfung gespeichert.",
        "legend_not_verified": "Eine KI-Antwort, die noch niemand geprüft hat.",
        "legend_stale": "Dokument oder Frage haben sich nach der Extraktion geändert; die Antwort ist möglicherweise veraltet. Führen Sie sie in der Matrix erneut aus.",
        "legend_failed": "Die Extraktion ist fehlgeschlagen; es gibt keine Antwort.",
        "legend_running": "Wurde beim Export noch verarbeitet.",
        "legend_not_run": "Diese Frage wurde noch nicht verarbeitet.",
        "legend_unavailable": "Sie können diese Datei nicht sehen oder sie wurde gelöscht; ihre Werte werden nicht exportiert.",
    },
    "fr-FR": {
        "sheet_answers": "Réponses",
        "sheet_sources": "Sources",
        "sheet_about": "À propos",
        "file": "Fichier",
        "column": "Colonne",
        "question": "Question",
        "answer": "Réponse",
        "status": "Statut",
        "confidence": "Confiance",
        "location": "Emplacement",
        "excerpt": "Extrait de la source",
        "hidden_file": "(Aucun accès)",
        "page": "Page {n}",
        "paragraph": "Paragraphe {n}",
        "truncated": "… (tronqué)",
        "list_separator": ", ",
        "about_matrix": "Matrice",
        "about_exported_at": "Exporté le",
        "about_exporter": "Exporté par",
        "about_files": "Fichiers",
        "about_columns": "Colonnes de questions",
        "about_hidden": "Cellules masquées",
        "about_hidden_note": "Les fichiers que vous ne pouvez pas voir, ou qui ont été supprimés, sont exportés sans valeur et affichés comme « (Aucun accès) ».",
        "about_legend": "Légende",
        "status_verified": "Vérifiée",
        "status_edited": "Modifiée",
        "status_needs_review": "Type incompatible",
        "status_not_verified": "Non vérifiée",
        "status_stale": "Obsolète",
        "status_failed": "En échec",
        "status_running": "Traitement en cours",
        "status_not_run": "Non exécutée",
        "status_unavailable": "Aucun accès",
        "legend_verified": "Une personne a vérifié et confirmé la réponse, y compris après une modification manuelle. En vert dans Réponses.",
        "legend_edited": "Une personne a saisi ou modifié la réponse, qui n’est pas encore vérifiée. En jaune dans Réponses.",
        "legend_needs_review": "Le contenu extrait ne correspond pas au type de la colonne ; il est conservé en texte pour vérification.",
        "legend_not_verified": "Une réponse de l’IA que personne n’a encore vérifiée.",
        "legend_stale": "Le document ou la question a changé après l’extraction ; la réponse est peut-être périmée. Relancez-la dans la matrice.",
        "legend_failed": "L’extraction a échoué ; il n’y a pas de réponse.",
        "legend_running": "Encore en traitement au moment de l’export.",
        "legend_not_run": "Cette question n’a pas encore été traitée.",
        "legend_unavailable": "Vous ne pouvez pas voir ce fichier, ou il a été supprimé ; ses valeurs ne sont pas exportées.",
    },
}
_DEFAULT_LANGUAGE = "en-US"
# Fills of the Answers sheet, by status (the legend repeats them).
_STATUS_FILLS = {"verified": FILL_VERIFIED, "edited": FILL_EDITED}
_FILENAME_UNSAFE_RE = re.compile(r'[\x00-\x1f\x7f/\\:*?"<>|]+')
_MAX_FILENAME_CHARS = 120


@dataclass(frozen=True)
class MatrixExport:
    """The workbook and the counts the audit line and the About sheet report."""

    content: bytes
    files: int
    columns: int
    hidden_cells: int


def export_copy(language: str) -> dict[str, str]:
    """The workbook's words in ``language``; unknown languages get English."""
    return _COPY.get(language) or _COPY[_DEFAULT_LANGUAGE]


def export_filename(matrix_name: str) -> str:
    """``<name>.xlsx`` with characters that file systems refuse replaced; ``matrix.xlsx`` when nothing is left."""
    stem = " ".join(_FILENAME_UNSAFE_RE.sub("_", matrix_name or "").split()).strip(" .")
    return f"{stem[:_MAX_FILENAME_CHARS].rstrip(' .') or 'matrix'}.xlsx"


def sheet_text(value: Any, copy: dict[str, str]) -> str:
    """Text a worksheet can hold: illegal characters dropped, cut at ``MAX_CELL_TEXT_CHARS`` with a marker."""
    text = ILLEGAL_CHARACTERS_RE.sub("", value if isinstance(value, str) else str(value))
    if len(text) <= MAX_CELL_TEXT_CHARS:
        return text
    marker = copy["truncated"]
    return text[: MAX_CELL_TEXT_CHARS - len(marker)] + marker


def _plain_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _location_label(copy: dict[str, str], primary: dict[str, Any] | None) -> str | None:
    """Page when the source has one, else the 1-based paragraph (chunk) the excerpt was found in."""
    if not primary:
        return None
    if (page := _plain_int(primary.get("page"))) is not None:
        return copy["page"].format(n=page)
    if (paragraph := _plain_int(primary.get("chunk_index"))) is not None and paragraph > 0:
        return copy["paragraph"].format(n=paragraph)
    return None


def _primary(cell: dict[str, Any] | None) -> dict[str, Any] | None:
    """The primary variant, else the first one (the grid's rule)."""
    variants = [v for v in (cell or {}).get("variants") or [] if isinstance(v, dict)]
    return next((v for v in variants if v.get("is_primary")), variants[0] if variants else None)


def _number(value: Any) -> int | float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number: int | float = value
    else:
        text = str(value).strip().replace(",", "")
        try:
            number = int(text)
        except ValueError:
            try:
                number = float(text)
            except ValueError:
                return None
    if isinstance(number, float) and not math.isfinite(number):
        return None
    if abs(number) >= _MAX_EXACT_INTEGER:
        return None
    return number


def _date(value: Any) -> date | None:
    try:
        parsed = date.fromisoformat(str(value).strip())
    except ValueError:
        return None
    return parsed if parsed.year >= _EXCEL_FIRST_YEAR else None


def answer_value(cell: dict[str, Any] | None, column: dict[str, Any], copy: dict[str, str]) -> str | int | float | bool | date | None:
    """A cell's answer as a spreadsheet value typed by its column, or None when it has none.

    Only finished cells (done or needs review) carry an answer; numbers, dates and yes/no answers become
    real cells, everything else (and anything that does not parse) stays text.
    """
    if not cell or cell.get("status") not in _ANSWERED:
        return None
    primary = _primary(cell)
    if primary is None:
        return None
    column_type = str(column.get("type") or "string")
    normalized = primary.get("normalized")
    if isinstance(normalized, dict):
        if normalized.get("amount") is not None:
            amount = _number(normalized["amount"])
            return amount if amount is not None else sheet_text(normalized["amount"], copy)
        if normalized.get("value") is not None:
            value = normalized["value"]
            if column_type == "date":
                as_date = _date(value)
                if as_date is not None:
                    return as_date
            elif column_type in ("number", "currency"):
                as_number = _number(value)
                if as_number is not None:
                    return as_number
            if isinstance(value, list):
                return sheet_text(copy["list_separator"].join(map(str, value)), copy)
            return sheet_text(value, copy)
        if normalized.get("choice") is not None:
            return sheet_text(normalized["choice"], copy)
        if isinstance(normalized.get("choices"), list):
            return sheet_text(copy["list_separator"].join(map(str, normalized["choices"])), copy)
        if isinstance(normalized.get("flag"), bool):
            return normalized["flag"]
        if normalized.get("raw_string") is not None:
            return sheet_text(normalized["raw_string"], copy)
    raw = primary.get("raw")
    if raw is None:
        return None
    return sheet_text(raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False), copy)


def cell_status(cell: dict[str, Any] | None, *, row_missing: bool) -> str:
    """One of ``LEGEND_STATUSES``, the first that applies in this order: no access, not run, processing,
    failed, stale, verified (a verified cell edited first counts as verified), edited, type mismatch, not
    verified."""
    if row_missing:
        return "unavailable"
    if not cell:
        return "not_run"
    status = cell.get("status")
    if status in ("queued", "running"):
        return "running"
    if status == "failed":
        return "failed"
    if cell.get("stale"):
        return "stale"
    if cell.get("verified"):
        return "verified"
    if cell.get("edited"):
        return "edited"
    if status == "needs_review":
        return "needs_review"
    return "not_verified"


def _confidence(cell: dict[str, Any], primary: dict[str, Any]) -> float | None:
    """The AI's confidence; none for an answer a person entered or changed."""
    if cell.get("edited") or primary.get("edited_by_user"):
        return None
    value = primary.get("confidence")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def build_matrix_workbook(
    *,
    matrix_name: str,
    columns: list[dict[str, Any]],
    rows: list[dict[str, Any]],
    cells: dict[str, dict[str, Any]],
    language: str,
    exporter_name: str,
    exported_at: datetime,
) -> MatrixExport:
    """Build the workbook from a snapshot projection (``columns``, ``rows``, ``cells`` keyed ``file:column``)."""
    copy = export_copy(language)
    wb = Workbook(write_only=True)
    bold = Font(bold=True)
    wrap = Alignment(wrap_text=True, vertical="top")
    fills = {status: PatternFill("solid", fgColor=color) for status, color in _STATUS_FILLS.items()}

    def text_cell(ws: Any, value: Any, **style: Any) -> WriteOnlyCell:
        cell = WriteOnlyCell(ws, value=sheet_text(value, copy))
        cell.data_type = "s"  # Never a formula, whatever the text starts with.
        for key, val in style.items():
            setattr(cell, key, val)
        return cell

    def value_cell(ws: Any, value: Any, **style: Any) -> WriteOnlyCell:
        if isinstance(value, str):
            return text_cell(ws, value, **style)
        cell = WriteOnlyCell(ws, value=value)
        if isinstance(value, date):
            cell.number_format = "yyyy-mm-dd"
        for key, val in style.items():
            setattr(cell, key, val)
        return cell

    def file_label(row: dict[str, Any]) -> str:
        return copy["hidden_file"] if row.get("missing") else str(row.get("file_name") or "")

    # ── Answers ──
    answers = wb.create_sheet(copy["sheet_answers"])
    answers.column_dimensions["A"].width = 40
    answers.freeze_panes = "B2"
    answers.append([text_cell(answers, copy["file"], font=bold)] + [text_cell(answers, c.get("name") or "", font=bold) for c in columns])
    hidden_cells = 0
    for row in rows:
        missing = bool(row.get("missing"))
        line = [text_cell(answers, file_label(row))]
        for column in columns:
            if missing:
                hidden_cells += 1
                line.append(None)
                continue
            cell = cells.get(f"{row.get('file_id')}:{column.get('column_id')}")
            status = cell_status(cell, row_missing=False)
            style = {"fill": fills[status]} if status in fills else {}
            line.append(value_cell(answers, answer_value(cell, column, copy), **style))
        answers.append(line)

    # ── Sources ──
    sources = wb.create_sheet(copy["sheet_sources"])
    for letter, width in zip("ABCDEFGH", (40, 24, 40, 24, 16, 12, 12, 80), strict=True):
        sources.column_dimensions[letter].width = width
    sources.freeze_panes = "A2"
    headers = ("file", "column", "question", "answer", "status", "confidence", "location", "excerpt")
    sources.append([text_cell(sources, copy[h], font=bold) for h in headers])
    for row in rows:
        missing = bool(row.get("missing"))
        for column in columns:
            cell = None if missing else cells.get(f"{row.get('file_id')}:{column.get('column_id')}")
            status = cell_status(cell, row_missing=missing)
            # Where an answer came from is shown only with the answer (a cell being rerun keeps its old variants).
            primary = _primary(cell) if cell and cell.get("status") in _ANSWERED else None
            confidence = _confidence(cell, primary) if cell and primary else None
            excerpt = primary.get("source_excerpt") if primary else None
            line = [
                text_cell(sources, file_label(row)),
                text_cell(sources, column.get("name") or ""),
                text_cell(sources, column.get("question") or ""),
                value_cell(sources, answer_value(cell, column, copy)),
                text_cell(sources, copy[f"status_{status}"]),
                value_cell(sources, confidence, number_format="0%") if confidence is not None else None,
                text_cell(sources, location) if (location := _location_label(copy, primary)) else None,
                text_cell(sources, excerpt, alignment=wrap) if isinstance(excerpt, str) and excerpt else None,
            ]
            sources.append(line)

    # ── About ──
    about = wb.create_sheet(copy["sheet_about"])
    about.column_dimensions["A"].width = 24
    about.column_dimensions["B"].width = 100
    stamp = exported_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")
    for label, value in (
        ("about_matrix", matrix_name),
        ("about_exported_at", stamp),
        ("about_exporter", exporter_name),
        ("about_files", len(rows)),
        ("about_columns", len(columns)),
        ("about_hidden", hidden_cells),
    ):
        about.append([text_cell(about, copy[label], font=bold), value_cell(about, value)])
    about.append([None, text_cell(about, copy["about_hidden_note"], alignment=wrap)])
    about.append([])
    about.append([text_cell(about, copy["about_legend"], font=bold)])
    for status in LEGEND_STATUSES:
        style = {"fill": fills[status]} if status in fills else {}
        about.append([text_cell(about, copy[f"status_{status}"], **style), text_cell(about, copy[f"legend_{status}"], alignment=wrap)])

    buf = io.BytesIO()
    wb.save(buf)
    return MatrixExport(content=buf.getvalue(), files=len(rows), columns=len(columns), hidden_cells=hidden_cells)
