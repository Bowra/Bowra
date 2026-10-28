"use client";

import {
  useCallback,
  type Dispatch,
  type PointerEvent as ReactPointerEvent,
  type SetStateAction,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
} from "react";
import { useTranslations } from "next-intl";

import { Check, Search } from "@/components/icons";
import { Tab, TabList, Tabs } from "@/components/ui/tabs";
import { Markdown } from "@/lib/md";
import type { ArtifactViewerProfile } from "@/lib/artifact-viewer-profile";
import { copyTextWithFallback } from "@/lib/clipboard";
import { CharIcon } from "@/components/char-icon";

export type PreviewLike = {
  family: string;
  mode?: "text" | "rows" | "image" | string;
  text?: string | null;
  truncated?: boolean;
  error?: string | null;
  note?: string | null;
  columns?: string[] | null;
  rows?: Record<string, unknown>[] | null;
  total_rows?: number | null;
  shown_rows?: number | null;
};

export type ImageViewState = {
  zoom: number;
  fit: boolean;
  offsetX: number;
  offsetY: number;
};

export const DEFAULT_IMAGE_VIEW_STATE: ImageViewState = {
  zoom: 1,
  fit: true,
  offsetX: 0,
  offsetY: 0,
};

export type TextViewState = {
  query: string;
  wrap: boolean;
};

export const DEFAULT_TEXT_VIEW_STATE: TextViewState = {
  query: "",
  wrap: true,
};

export function ArtifactImagePreview({
  src,
  alt,
  view,
  onViewChange,
}: {
  src: string;
  alt: string;
  view: ImageViewState;
  onViewChange: Dispatch<SetStateAction<ImageViewState>>;
}) {
  const t = useTranslations("artifact_viewer");
  const dragRef = useRef<{ x: number; y: number; offsetX: number; offsetY: number } | null>(null);
  const zoomPercent = Math.round(view.zoom * 100);

  const changeZoom = (next: number) => {
    onViewChange((current) => ({
      ...current,
      fit: false,
      zoom: Math.min(4, Math.max(0.25, next)),
    }));
  };

  const onPointerDown = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (view.fit) return;
    dragRef.current = {
      x: event.clientX,
      y: event.clientY,
      offsetX: view.offsetX,
      offsetY: view.offsetY,
    };
    event.currentTarget.setPointerCapture(event.pointerId);
  };
  const onPointerMove = (event: ReactPointerEvent<HTMLDivElement>) => {
    const drag = dragRef.current;
    if (!drag) return;
    onViewChange((current) => ({
      ...current,
      offsetX: drag.offsetX + event.clientX - drag.x,
      offsetY: drag.offsetY + event.clientY - drag.y,
    }));
  };
  const stopDragging = () => {
    dragRef.current = null;
  };

  return (
    <div className="artifact-image-preview">
      <div className="artifact-content-toolbar" aria-label={t("image_controls")}>
        <button
          type="button"
          className={view.fit ? "active" : undefined}
          onClick={() => onViewChange(DEFAULT_IMAGE_VIEW_STATE)}
        >
          {t("image_fit")}
        </button>
        <button
          type="button"
          className={!view.fit && view.zoom === 1 ? "active" : undefined}
          onClick={() => onViewChange({ zoom: 1, fit: false, offsetX: 0, offsetY: 0 })}
        >
          {t("image_actual")}
        </button>
        <button type="button" onClick={() => changeZoom(view.zoom - 0.25)} aria-label={t("zoom_out")}>−</button>
        <output aria-live="polite">{view.fit ? t("image_fit") : `${zoomPercent}%`}</output>
        <button type="button" onClick={() => changeZoom(view.zoom + 0.25)} aria-label={t("zoom_in")}>＋</button>
      </div>
      <div
        className={view.fit ? "artifact-image-stage fit" : "artifact-image-stage pannable"}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={stopDragging}
        onPointerCancel={stopDragging}
      >
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <img
          src={src}
          alt={alt}
          decoding="async"
          draggable={false}
          style={view.fit
            ? undefined
            : {
                transform: `translate(${view.offsetX}px, ${view.offsetY}px) scale(${view.zoom})`,
              }}
        />
      </div>
    </div>
  );
}

export function ArtifactTextPreview({
  text,
  profile,
  raw,
  truncated,
  view,
  onViewChange,
}: {
  text: string;
  profile: ArtifactViewerProfile;
  raw: boolean;
  truncated?: boolean;
  view: TextViewState;
  onViewChange: Dispatch<SetStateAction<TextViewState>>;
}) {
  const t = useTranslations("artifact_viewer");
  const [copied, setCopied] = useState(false);
  const readableText = useMemo(() => {
    if (!raw && profile === "json") {
      try {
        return JSON.stringify(JSON.parse(text), null, 2);
      } catch {
        return text;
      }
    }
    return text;
  }, [profile, raw, text]);
  const matchCount = view.query
    ? readableText.toLocaleLowerCase().split(view.query.toLocaleLowerCase()).length - 1
    : 0;

  const copy = async () => {
    if (!(await copyTextWithFallback(readableText, t("text_copy")))) {
      setCopied(false);
      return; // Used the prompt fallback; do not falsely report a successful copy
    }
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1500);
  };

  return (
    <div className="artifact-text-preview">
      <div className="artifact-content-toolbar artifact-text-toolbar">
        <label>
          <Search size={13} aria-hidden="true" />
          <span className="sr-only">{t("text_find")}</span>
          <input
            type="search"
            value={view.query}
            onChange={(event) => onViewChange((current) => ({
              ...current,
              query: event.target.value,
            }))}
            placeholder={t("text_find")}
          />
        </label>
        {view.query ? <output>{t("text_matches", { count: matchCount })}</output> : null}
        <button
          type="button"
          className={view.wrap ? "active" : undefined}
          onClick={() => onViewChange((current) => ({
            ...current,
            wrap: !current.wrap,
          }))}
        >
          {t("text_wrap")}
        </button>
        <button type="button" onClick={() => void copy()}>
          {copied ? <Check size={12} aria-hidden="true" /> : null}
          {copied ? t("text_copied") : t("text_copy")}
        </button>
      </div>
      {!raw && profile === "markdown" ? (
        <article className="artifact-markdown-preview">
          <Markdown markdown={readableText} />
        </article>
      ) : (
        <pre className={view.wrap ? "wrap" : "nowrap"}>
          <HighlightedText text={readableText} query={view.query} />
        </pre>
      )}
      {truncated ? <div className="artifact-viewer-footnote">{t("truncated")}</div> : null}
    </div>
  );
}

function HighlightedText({ text, query }: { text: string; query: string }) {
  if (!query) return text;
  const lowerText = text.toLocaleLowerCase();
  const lowerQuery = query.toLocaleLowerCase();
  const parts: Array<{ value: string; match: boolean }> = [];
  let cursor = 0;
  while (cursor < text.length) {
    const index = lowerText.indexOf(lowerQuery, cursor);
    if (index < 0) {
      parts.push({ value: text.slice(cursor), match: false });
      break;
    }
    if (index > cursor) parts.push({ value: text.slice(cursor, index), match: false });
    parts.push({ value: text.slice(index, index + query.length), match: true });
    cursor = index + query.length;
  }
  return parts.map((part, index) => part.match
    ? <mark key={`${index}:${part.value}`}>{part.value}</mark>
    : part.value);
}

type SortState = { column: string; direction: "asc" | "desc" } | null;

export function ArtifactRowsPreview({ preview }: { preview: PreviewLike }) {
  const t = useTranslations("artifact_viewer");
  const rowDetailId = useId();
  const columns = preview.columns ?? [];
  const rows = preview.rows ?? [];
  const [visibleColumns, setVisibleColumns] = useState<ReadonlySet<string>>(() => new Set(columns));
  const [sort, setSort] = useState<SortState>(null);
  const [selectedRowIndex, setSelectedRowIndex] = useState<number | null>(null);
  const rowRefs = useRef(new Map<number, HTMLTableRowElement>());
  const detailCloseRef = useRef<HTMLButtonElement | null>(null);
  const shownColumns = columns.filter((column) => visibleColumns.has(column));
  const sortedRows = useMemo(() => {
    const indexedRows = rows.map((row, sourceIndex) => ({ row, sourceIndex }));
    if (!sort) return indexedRows;
    return indexedRows.sort((left, right) => {
      const compared = compareCells(left.row[sort.column], right.row[sort.column]);
      if (compared !== 0) return sort.direction === "asc" ? compared : -compared;
      return left.sourceIndex - right.sourceIndex;
    });
  }, [rows, sort]);
  const selectedPosition = sortedRows.findIndex(({ sourceIndex }) => sourceIndex === selectedRowIndex);
  const selectedEntry = selectedPosition >= 0 ? sortedRows[selectedPosition] : null;

  const closeRowDetails = useCallback(() => {
    if (selectedRowIndex === null) return;
    const rowIndex = selectedRowIndex;
    setSelectedRowIndex(null);
    requestAnimationFrame(() => rowRefs.current.get(rowIndex)?.focus());
  }, [selectedRowIndex]);

  useEffect(() => {
    if (selectedRowIndex === null) return;
    const frame = requestAnimationFrame(() => detailCloseRef.current?.focus());
    return () => cancelAnimationFrame(frame);
  }, [selectedRowIndex]);

  useEffect(() => {
    if (selectedRowIndex === null) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      if (document.querySelector('[role="dialog"][aria-modal="true"]')) return;
      event.preventDefault();
      closeRowDetails();
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [closeRowDetails, selectedRowIndex]);

  const toggleSort = (column: string) => {
    setSort((current) => {
      if (!current || current.column !== column) return { column, direction: "asc" };
      if (current.direction === "asc") return { column, direction: "desc" };
      return null;
    });
  };

  return (
    <div className="artifact-viewer-rows">
      <div className="artifact-table-head">
        <div className="artifact-viewer-footnote">
          {t("rows_shown_total", {
            shown: preview.shown_rows ?? rows.length,
            total: preview.total_rows ?? rows.length,
          })}
          {preview.truncated ? ` · ${t("truncated_rows")}` : ""}
        </div>
        <details className="artifact-column-chooser">
          <summary>{t("table_columns")}</summary>
          <div>
            {columns.map((column) => (
              <label key={column}>
                <input
                  type="checkbox"
                  checked={visibleColumns.has(column)}
                  onChange={() => setVisibleColumns((current) => {
                    const next = new Set(current);
                    if (next.has(column) && next.size > 1) next.delete(column);
                    else next.add(column);
                    return next;
                  })}
                />
                <span>{column}</span>
              </label>
            ))}
          </div>
        </details>
      </div>
      <div className="artifact-table-scroll">
        <table>
          <thead>
            <tr>
              {shownColumns.map((column) => (
                <th key={column} aria-sort={sort?.column === column ? (sort.direction === "asc" ? "ascending" : "descending") : "none"}>
                  <button type="button" onClick={() => toggleSort(column)}>
                    <span>{column}</span>
                    <span aria-hidden="true">
                      <CharIcon ch={sort?.column === column ? (sort.direction === "asc" ? "↑" : "↓") : "↕"} aria-hidden />
                    </span>
                  </button>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {sortedRows.map(({ row, sourceIndex }, index) => (
              <tr
                key={sourceIndex}
                ref={(node) => {
                  if (node) rowRefs.current.set(sourceIndex, node);
                  else rowRefs.current.delete(sourceIndex);
                }}
                tabIndex={0}
                onClick={() => setSelectedRowIndex(sourceIndex)}
                onKeyDown={(event) => {
                  if (event.key === "Enter" || event.key === " ") {
                    event.preventDefault();
                    setSelectedRowIndex(sourceIndex);
                  }
                }}
                aria-controls={rowDetailId}
                aria-expanded={selectedRowIndex === sourceIndex}
                aria-label={t("table_open_row", { row: index + 1 })}
              >
                {shownColumns.map((column) => <td key={column}>{String(row[column] ?? "")}</td>)}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {selectedEntry ? (
        <div
          id={rowDetailId}
          className="artifact-row-detail"
          role="dialog"
          aria-modal="false"
          aria-labelledby={`${rowDetailId}-title`}
        >
          <div>
            <strong id={`${rowDetailId}-title`}>
              {t("table_row_details")} · {selectedPosition + 1}/{sortedRows.length}
            </strong>
            <div className="artifact-row-detail-header-actions">
              <button
                type="button"
                disabled={selectedPosition <= 0}
                onClick={() => setSelectedRowIndex(sortedRows[selectedPosition - 1]?.sourceIndex ?? null)}
              >
                {t("table_previous_row")}
              </button>
              <button
                type="button"
                disabled={selectedPosition >= sortedRows.length - 1}
                onClick={() => setSelectedRowIndex(sortedRows[selectedPosition + 1]?.sourceIndex ?? null)}
              >
                {t("table_next_row")}
              </button>
              <button ref={detailCloseRef} type="button" onClick={closeRowDetails}>{t("close")}</button>
            </div>
          </div>
          <dl>
            {columns.map((column) => (
              <div key={column}>
                <dt>{column}</dt>
                <dd>{String(selectedEntry.row[column] ?? "")}</dd>
              </div>
            ))}
          </dl>
        </div>
      ) : null}
    </div>
  );
}

function compareCells(left: unknown, right: unknown): number {
  const leftNumber = typeof left === "number" ? left : Number(left);
  const rightNumber = typeof right === "number" ? right : Number(right);
  if (Number.isFinite(leftNumber) && Number.isFinite(rightNumber)) {
    return leftNumber - rightNumber;
  }
  return String(left ?? "").localeCompare(String(right ?? ""), undefined, {
    numeric: true,
    sensitivity: "base",
  });
}

export function ArtifactPresentationPreview({ text }: { text: string }) {
  const t = useTranslations("artifact_viewer");
  const slides = parseMarkedSections(text, /^--- slide\.(\d+) ---$/gm, "slide");
  if (slides.length === 0) {
    return <div className="artifact-viewer-note">{t("document_no_slide_text")}</div>;
  }
  return (
    <div className="artifact-presentation-preview">
      <div className="artifact-document-status">
        {t("presentation_outline_notice", { count: slides.length })}
      </div>
      <div className="artifact-slide-grid">
        {slides.map((slide, index) => (
          <article key={`${slide.label}:${index}`}>
            <span>{t("presentation_slide", { number: slide.label })}</span>
            <pre>{slide.content || t("document_no_slide_text")}</pre>
          </article>
        ))}
      </div>
    </div>
  );
}

export function ArtifactWorkbookPreview({ text }: { text: string }) {
  const t = useTranslations("artifact_viewer");
  const sheets = parseMarkedSections(text, /^--- sheet: (.+) ---$/gm, "sheet");
  const [activeIndex, setActiveIndex] = useState(0);
  if (sheets.length === 0) {
    return <div className="artifact-viewer-note">{t("document_no_sheet_rows")}</div>;
  }
  const active = sheets[Math.min(activeIndex, sheets.length - 1)]!;
  const lines = active.content.split("\n").filter((line) => line.trim());
  const width = Math.max(0, ...lines.map((line) => line.split("\t").length));
  const columns = Array.from({ length: width }, (_, index) => t("workbook_column", { number: index + 1 }));
  const rows = lines.map((line) => Object.fromEntries(
    line.split("\t").map((value, index) => [columns[index]!, value]),
  ));
  return (
    <div className="artifact-workbook-preview">
      <Tabs value={String(activeIndex)} onValueChange={(value) => setActiveIndex(Number(value))}>
        <TabList className="artifact-sheet-tabs" aria-label={t("workbook_sheets")}>
          {sheets.map((sheet, index) => (
            <Tab
              value={String(index)}
              className={index === activeIndex ? "active" : undefined}
              key={`${sheet.label}:${index}`}
            >
              {sheet.label}
            </Tab>
          ))}
        </TabList>
      </Tabs>
      <div className="artifact-workbook-notice">{t("workbook_sample_notice")}</div>
      <ArtifactRowsPreview
        key={`${active.label}:${activeIndex}`}
        preview={{
          family: "spreadsheet",
          mode: "rows",
          columns,
          rows,
          shown_rows: rows.length,
          total_rows: rows.length,
          truncated: text.includes("… (truncated)"),
        }}
      />
    </div>
  );
}

function parseMarkedSections(
  text: string,
  pattern: RegExp,
  fallbackPrefix: string,
): Array<{ label: string; content: string }> {
  const matches = [...text.matchAll(pattern)];
  if (matches.length === 0) return [];
  return matches.map((match, index) => ({
    label: match[1]?.trim() || `${fallbackPrefix} ${index + 1}`,
    content: text.slice(
      (match.index ?? 0) + match[0].length,
      matches[index + 1]?.index ?? text.length,
    ).trim(),
  }));
}
