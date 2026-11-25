"use client";

import {
  forwardRef,
  useCallback,
  useEffect,
  useImperativeHandle,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent,
  type MouseEvent as ReactMouseEvent,
} from "react";
import { useLocale, useTranslations } from "next-intl";
import { Lightbulb as LucideLightbulb } from "lucide-react";

import { ChevronLeft, ChevronRight, X } from "@/components/icons";
import { ErrorNotice } from "@/components/error-notice";
import { LucideGlyph } from "@/components/icon-refresh";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { useNarrowViewport } from "@/components/ui/sheet";
import { Notice } from "@/components/ui/notice";
import { ReviewLine } from "@/components/ui/review-line";
import { SaveBar } from "@/components/ui/save-bar";
import { canvasCloseGuardOptions, ChangeRow, SaveState, StateBlock, useCanvasCloseGuard } from "@/components/canvas/object";
import { useConfirmDialog } from "@/components/ui/confirm-dialog";
import { appPath } from "@/lib/base-path";
import { copyTextWithFallback } from "@/lib/clipboard";
import { formatDateTime, formatTime } from "@/lib/locale";
import {
  countFieldTokens,
  fetchPresentationInventory,
  formatChartValue,
  matchPendingEdits,
  parseChartValue,
  pendingEditId,
  PresentationEditError,
  removePendingEdit,
  savePresentationEdits,
  measureTextOverflow,
  spillsFurther,
  toWireEdits,
  upsertPendingEdit,
  type InventoryElement,
  type InventorySlide,
  type LatestVersionFacts,
  type PendingEdit,
  type PresentationInventory,
  type PresentationSaveResult,
  type ReapplyRow,
} from "@/lib/presentation-edit";
import { canonicalizePresentationSelection, presentationSlideSelection } from "@/lib/presentation-selection";
import { locateTable, locateText, type TableLayout, type TextLayout } from "@/lib/presentation-layout";
import type { AttachmentRef } from "@/lib/types";
import { toast } from "@/lib/use-toast";
import { fetchPresentationPdf, isPresentationPreviewBusy } from "../artifact-document-preview";
import { ChartGrid, ChartPanel, ElementList, TablePanel, TextPanel, UnsupportedPanel, elementLabel, excerpt } from "./inspector";
import { ChartPopover, InPlaceLayer, usePageLayout, type CellMove, type DrawnPage, type InPlaceTarget } from "./in-place";
import { LazyPdfPageCanvas, openPdfDocument, PdfPageCanvas, readPageText, usePdfDocument } from "./pdf-page";
import { isComposing } from "./keys";
import styles from "./presentation-editor.module.css";

export interface PresentationEditorExit {
  /** The file the viewer should show next: the saved version, or the version the person moved to. */
  artifactId: string;
  /** That file's rendered PDF, when the editor already holds it. */
  pdfData?: ArrayBuffer;
  /** Close the full view too, not only edit mode. */
  close: boolean;
  /** Open the next file in edit mode (an upload's edited copy). */
  edit?: boolean;
  /** Runs once the viewer shows the next file, such as adding a chat reference after the view settles. */
  then?: () => void;
  /** The page the person was on, so the viewer shows it again (design §13.1). */
  page?: number;
}

export interface PresentationEditorHandle {
  /** Leave edit mode, asking first when there are unsaved edits. */
  requestExit: (close: boolean) => void;
}

type RenderState = "loading" | "ready" | "failed" | "busy";

interface Base {
  artifactId: string;
  versionNo: number;
  inventory: PresentationInventory;
}

interface SavedState {
  result: PresentationSaveResult;
  changes: PendingEdit[];
  previousVersion: number;
  inventory: PresentationInventory;
  previousPdf: ArrayBuffer | null;
  spills: Array<{ key: string; page: number }> | null;
}

interface ConflictState {
  latest: LatestVersionFacts;
  rows: ReapplyRow[] | null;
  inventory: PresentationInventory | null;
  failed: boolean;
}

type ChartDrafts = Record<string, Array<Array<string | undefined>>>;

const GUIDE_KEY = "bowhead.presentationEdit.guideDismissed";
/** On phones a table wider than this is read-only (design §2, 2026-11-24). */
const PHONE_TABLE_COLUMNS = 3;

function readGuideDismissed(): boolean {
  try { return window.localStorage.getItem(GUIDE_KEY) === "1"; } catch { return false; }
}

function pdfHref(artifactId: string): string {
  return appPath(`/api/v1/files/${encodeURIComponent(artifactId)}/representations/pptx-pdf`);
}

function isMac(): boolean {
  return typeof navigator !== "undefined" && /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent);
}

function findElement(inventory: PresentationInventory | undefined, key: string | null): { slide: InventorySlide; element: InventoryElement } | null {
  if (!inventory || !key) return null;
  for (const slide of inventory.slides) {
    const element = slide.elements.find((item) => item.key === key);
    if (element) return { slide, element };
  }
  return null;
}

function elementText(element: InventoryElement, edits: ReadonlyArray<PendingEdit>): string {
  return (element.paragraphs ?? []).map((paragraph, index) => {
    const edit = edits.find((item) => item.kind === "text" && item.key === element.key && item.paragraph === index);
    return edit && edit.kind === "text" ? edit.text : paragraph.text;
  }).join("\n");
}

/** Edited text elements whose text now lies outside their box, and did not before (design §6.8). */
async function findSpills(saved: SavedState, newPdf: ArrayBuffer): Promise<Array<{ key: string; page: number }>> {
  if (!saved.previousPdf) return [];
  const keys = [...new Set(saved.changes.filter((edit) => edit.kind === "text").map((edit) => edit.key))];
  const targets = keys.flatMap((key) => {
    const found = findElement(saved.inventory, key);
    return found?.element.box ? [{ key, page: found.slide.index, element: found.element, box: found.element.box }] : [];
  });
  if (!targets.length) return [];
  const before = await openPdfDocument(saved.previousPdf);
  const after = await openPdfDocument(newPdf).catch((error: unknown) => { before.close(); throw error; });
  try {
    const spills: Array<{ key: string; page: number }> = [];
    for (const target of targets) {
      if (target.page > after.pdf.numPages || target.page > before.pdf.numPages) continue;
      const [oldItems, newItems] = await Promise.all([readPageText(before.pdf, target.page), readPageText(after.pdf, target.page)]);
      const token = saved.inventory.field_token;
      const overflowAfter = measureTextOverflow(newItems, target.box, elementText(target.element, saved.changes), token);
      const overflowBefore = measureTextOverflow(oldItems, target.box, elementText(target.element, []), token);
      if (spillsFurther(overflowBefore, overflowAfter)) spills.push({ key: target.key, page: target.page });
    }
    return spills;
  } finally {
    before.close();
    after.close();
  }
}

export const PresentationEditor = forwardRef<PresentationEditorHandle, {
  sessionId: string;
  artifactId: string;
  filename: string;
  downloadHref: string;
  initialPdfData?: ArrayBuffer;
  /** The page the person was viewing when editing started. */
  initialPage?: number;
  agentRunning?: boolean;
  onReference?: () => void;
  /** Reference one element with its slide in chat (design §6.9); the editor leaves the full view first. */
  onReferenceElement?: (reference: AttachmentRef) => void;
  onExit: (exit: PresentationEditorExit | null) => void;
}>(function PresentationEditor({
  sessionId, artifactId, filename, downloadHref, initialPdfData, initialPage, agentRunning = false, onReference, onReferenceElement, onExit,
}, ref) {
  const t = useTranslations("presentation_edit");
  const tObject = useTranslations("canvas_object");
  const locale = useLocale();
  const { confirmDiscard, dialog } = useCanvasCloseGuard();
  const { confirm, dialog: discardDialog } = useConfirmDialog();
  // The page rail exists only where it shows (1200 px and wider); its thumbnails draw as they near view.
  const [wideRail, setWideRail] = useState(() => typeof window !== "undefined" && typeof window.matchMedia === "function" && window.matchMedia("(min-width: 1200px)").matches);
  useEffect(() => {
    if (typeof window.matchMedia !== "function") return;
    const query = window.matchMedia("(min-width: 1200px)");
    const update = () => setWideRail(query.matches);
    update();
    query.addEventListener("change", update);
    return () => query.removeEventListener("change", update);
  }, []);
  const savedNoticeRef = useRef<HTMLDivElement>(null);
  /** Set by a save: once the saved summary is on screen, it takes focus (the save bar went away). */
  const focusSavedRef = useRef(false);

  const [base, setBase] = useState<Base | null>(null);
  const [inventoryError, setInventoryError] = useState<PresentationEditError | null>(null);
  const [inventoryLoading, setInventoryLoading] = useState(true);
  const [render, setRender] = useState<{ artifactId: string; data: ArrayBuffer } | null>(
    initialPdfData ? { artifactId, data: initialPdfData } : null,
  );
  const [renderState, setRenderState] = useState<RenderState>(initialPdfData ? "ready" : "loading");
  const [page, setPage] = useState(() => Math.max(1, initialPage ?? 1));
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const [pending, setPending] = useState<PendingEdit[]>([]);
  const [chartDrafts, setChartDrafts] = useState<ChartDrafts>({});
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<PresentationEditError | null>(null);
  const [refusals, setRefusals] = useState<Record<string, string>>({});
  const [conflict, setConflict] = useState<ConflictState | null>(null);
  const [reapplied, setReapplied] = useState<{ version: number; applied: number; manual: ReapplyRow[] } | null>(null);
  const [saved, setSaved] = useState<SavedState | null>(null);
  const [showSavedChanges, setShowSavedChanges] = useState(false);
  const [guideDismissed, setGuideDismissed] = useState(true);
  const [stageSize, setStageSize] = useState({ width: 0, height: 0 });
  const phone = useNarrowViewport(true);
  const [sheetPage, setSheetPage] = useState<number | null>(null);
  const [phoneWidth, setPhoneWidth] = useState(0);
  const phoneBodyRef = useRef<HTMLDivElement>(null);
  const phoneSheetRef = useRef<HTMLDivElement>(null);

  const rootRef = useRef<HTMLDivElement>(null);
  const stageRef = useRef<HTMLDivElement>(null);
  const inspectorRef = useRef<HTMLElement>(null);
  const aliveRef = useRef(true);
  const pdfAbortRef = useRef<AbortController | null>(null);
  const inventoryAbortRef = useRef<AbortController | null>(null);
  const focusAfterSelectRef = useRef(false);
  // In-place editing on the desktop stage (design §13.3).
  const [drawn, setDrawn] = useState<DrawnPage | null>(null);
  const [inPlace, setInPlace] = useState<InPlaceTarget | null>(null);
  /** Set while code moves focus to a hit, so that this focus does not select it. */
  const quietFocusRef = useRef(false);
  const pageRef = useRef<HTMLDivElement>(null);
  /** A character typed to open an inspector field (an element not located on the render). */
  const typedRef = useRef<string | null>(null);
  /** The table cell clicked to open the inspector grid. */
  const [focusCell, setFocusCell] = useState<readonly [number, number] | null>(null);
  const focusHitRef = useRef<string | null>(null);
  const [focusTick, setFocusTick] = useState(0);

  const { pdf } = usePdfDocument(render?.data ?? null);
  const inventory = base?.inventory;
  const slideCount = inventory?.slides.length ?? pdf?.numPages ?? 0;
  const slide = inventory?.slides[page - 1];
  useEffect(() => {
    if (slideCount > 0 && page > slideCount) setPage(slideCount);
  }, [page, slideCount]);
  const fieldToken = inventory?.field_token ?? "￼";
  const undoKey = isMac() ? "⌘Z" : "Ctrl+Z";

  useEffect(() => {
    aliveRef.current = true;
    setGuideDismissed(readGuideDismissed());
    return () => {
      aliveRef.current = false;
      pdfAbortRef.current?.abort();
      inventoryAbortRef.current?.abort();
    };
  }, []);

  const loadRender = useCallback((id: string, onReady?: (data: ArrayBuffer) => void) => {
    pdfAbortRef.current?.abort();
    const controller = new AbortController();
    pdfAbortRef.current = controller;
    setRenderState("loading");
    fetchPresentationPdf(pdfHref(id), controller.signal).then((data) => {
      if (!aliveRef.current || controller.signal.aborted) return;
      setRender({ artifactId: id, data });
      setRenderState("ready");
      onReady?.(data);
    }, (error: unknown) => {
      if (!aliveRef.current || controller.signal.aborted) return;
      setRenderState(isPresentationPreviewBusy(error) ? "busy" : "failed");
    });
  }, []);

  const loadInventory = useCallback(async (id: string): Promise<PresentationInventory | null> => {
    inventoryAbortRef.current?.abort();
    const controller = new AbortController();
    inventoryAbortRef.current = controller;
    setInventoryLoading(true);
    setInventoryError(null);
    try {
      const value = await fetchPresentationInventory({ sessionId, artifactId: id, signal: controller.signal });
      if (!aliveRef.current || controller.signal.aborted) return null;
      setBase({ artifactId: value.artifact_id, versionNo: value.version_no, inventory: value });
      return value;
    } catch (error) {
      if (!aliveRef.current || controller.signal.aborted) return null;
      setInventoryError(error instanceof PresentationEditError ? error : new PresentationEditError("network"));
      return null;
    } finally {
      if (aliveRef.current && !controller.signal.aborted) setInventoryLoading(false);
    }
  }, [sessionId]);

  useEffect(() => {
    void loadInventory(artifactId);
    if (!initialPdfData) loadRender(artifactId);
    // The editor opens on one file; later bases are loaded by the actions that move to them.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useLayoutEffect(() => {
    const stage = phone ? phoneBodyRef.current : stageRef.current;
    if (!stage) return;
    const measure = () => {
      if (phone) setPhoneWidth(Math.max(0, stage.clientWidth - 32));
      else setStageSize({ width: stage.clientWidth, height: stage.clientHeight });
    };
    measure();
    if (typeof ResizeObserver !== "function") return;
    const observer = new ResizeObserver(measure);
    observer.observe(stage);
    return () => observer.disconnect();
  }, [phone]);

  const aspect = inventory ? inventory.slide_width / Math.max(1, inventory.slide_height) : 16 / 9;
  const pageWidth = Math.max(0, Math.floor(Math.min(stageSize.width - 32, (stageSize.height - 32) * aspect)));

  const selected = slide?.elements.find((element) => element.key === selectedKey) ?? null;

  const pageLayout = usePageLayout(pdf, !phone && !saved ? drawn : null);
  const located = useMemo(() => {
    const texts = new Map<string, TextLayout>();
    const tables = new Map<string, TableLayout>();
    if (!pageLayout || !slide || pageLayout.page !== slide.index) return { texts, tables };
    for (const element of slide.elements) {
      if (!element.editable) continue;
      if (element.kind === "text") {
        const layout = locateText(element, pageLayout.glyphs, fieldToken);
        if (layout) texts.set(element.key, layout);
      } else if (element.kind === "table") {
        // A table whose rows cannot all be found keeps the inspector grid (spike §10: 98 % found).
        const layout = locateTable(element, pageLayout.glyphs);
        if (layout && layout.located >= 0.9) tables.set(element.key, layout);
      }
    }
    return { texts, tables };
  }, [fieldToken, pageLayout, slide]);
  /** The element is edited on the slide itself: desktop, editing, and its text found on the render. */
  const editsInPlace = useCallback((element: InventoryElement | null | undefined) => Boolean(
    element && element.editable && !phone && !saved && !conflict
      && (element.kind === "chart" ? Boolean(element.chart) : located.texts.has(element.key) || located.tables.has(element.key)),
  ), [conflict, located, phone, saved]);

  useEffect(() => {
    if (focusHitRef.current) {
      const key = focusHitRef.current;
      focusHitRef.current = null;
      focusHitQuietly(key);
      return;
    }
    if (!focusAfterSelectRef.current) return;
    focusAfterSelectRef.current = false;
    // Focus the field with the caret at the end, adding the character that opened it.
    const open = (field: HTMLElement | null | undefined) => {
      if (!field) return;
      field.focus();
      if (!(field instanceof HTMLInputElement || field instanceof HTMLTextAreaElement)) return;
      const typed = typedRef.current;
      typedRef.current = null;
      if (typed) {
        const setter = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(field), "value")?.set;
        setter?.call(field, field.value + typed);
        field.dispatchEvent(new Event("input", { bubbles: true }));
      }
      const end = field.value.length;
      field.setSelectionRange(end, end);
    };
    if (!phone) {
      open(inspectorRef.current?.querySelector<HTMLElement>("[data-autofocus]"));
      return;
    }
    // The phone editor is a sheet that mounts in a portal after this commit.
    const frame = requestAnimationFrame(() => open(phoneSheetRef.current?.querySelector<HTMLElement>("[data-autofocus]")));
    return () => cancelAnimationFrame(frame);
  }, [phone, selectedKey, focusCell, focusTick]);

  /** Focus a hit without selecting it (focus that the keyboard moves there selects). */
  const focusHitQuietly = useCallback((key: string) => {
    const hit = rootRef.current?.querySelector<HTMLElement>(`[data-element-key="${CSS.escape(key)}"]`);
    if (!hit) return null;
    quietFocusRef.current = true;
    hit.focus();
    quietFocusRef.current = false;
    return hit;
  }, []);

  const select = useCallback((key: string, cell?: [number, number] | null) => {
    const found = findElement(inventory, key);
    if (!found) return;
    const element = found.element;
    setPage(found.slide.index);
    setInPlace(null);
    setFocusCell(null);
    setSelectedKey(key);
    if (phone || found.slide.index !== page) {
      focusAfterSelectRef.current = true;
      return;
    }
    // A selected text box or located table keeps its field focused, hidden, so typing and input
    // methods land from the first key (design §13.4). A text box types at the end of its last
    // paragraph; a table in the clicked cell, or its first editable one.
    if (editsInPlace(element) && element.kind === "text") {
      const paragraphs = located.texts.get(key)?.paragraphs ?? [];
      const last = paragraphs[paragraphs.length - 1]?.index;
      if (last !== undefined) {
        setInPlace({ kind: "text", key, paragraph: last, armed: true });
        return;
      }
    }
    if (editsInPlace(element) && element.kind === "table") {
      const target = cell && element.rows?.[cell[0]]?.[cell[1]]?.editable ? cell : firstEditableCell(element);
      if (target) {
        setInPlace({ kind: "cell", key, row: target[0], col: target[1], armed: true });
        return;
      }
    }
    // Other elements, and those edited in the inspector, keep focus on their hit: Enter, typing or a
    // second click starts editing, so Enter never lands in an inspector field as a new line.
    focusHitRef.current = key;
  }, [editsInPlace, inventory, located, page, phone]);

  const clearSelection = useCallback(() => {
    const key = selectedKey;
    setInPlace(null);
    // Move focus before the panel unmounts, so it returns to the element instead of falling to the page.
    const target = key ? focusHitQuietly(key) : null;
    setSelectedKey(null);
    if (key && !target) {
      requestAnimationFrame(() => rootRef.current?.querySelector<HTMLElement>(`[data-list-key="${CSS.escape(key)}"]`)?.focus());
    }
  }, [focusHitQuietly, selectedKey]);

  const clearRefusal = (id: string) => setRefusals((current) => {
    if (!(id in current)) return current;
    const next = { ...current };
    delete next[id];
    return next;
  });

  const updateText = (element: InventoryElement, slideId: number, paragraph: number, text: string) => {
    const expect = element.paragraphs?.[paragraph]?.text ?? "";
    setPending((current) => upsertPendingEdit(current, { kind: "text", key: element.key, slideId, paragraph, expect, text }));
    clearRefusal(pendingEditId({ kind: "text", key: element.key, paragraph }));
  };

  const updateCell = (element: InventoryElement, slideId: number, row: number, col: number, text: string) => {
    const expect = element.rows?.[row]?.[col]?.text ?? "";
    setPending((current) => upsertPendingEdit(current, { kind: "cell", key: element.key, slideId, row, col, expect, text }));
    clearRefusal(pendingEditId({ kind: "cell", key: element.key, row, col }));
  };

  const updateChart = (element: InventoryElement, slideId: number, series: number, category: number, text: string) => {
    const chart = element.chart;
    if (!chart) return;
    setChartDrafts((current) => {
      const grid = (current[element.key] ?? chart.series.map(() => chart.categories.map(() => undefined))).map((row) => [...row]);
      grid[series][category] = text;
      return { ...current, [element.key]: grid };
    });
    const parsed = parseChartValue(text, chart.series[series]?.format, locale);
    if (parsed === undefined) return;
    setPending((current) => {
      const existing = current.find((edit): edit is Extract<PendingEdit, { kind: "chart" }> => edit.kind === "chart" && edit.key === element.key);
      const values = (existing?.values ?? chart.series.map((item) => item.values)).map((row) => [...row]);
      values[series][category] = parsed;
      return upsertPendingEdit(current, { kind: "chart", key: element.key, slideId, expect: chart, values });
    });
    clearRefusal(pendingEditId({ kind: "chart", key: element.key }));
  };

  const undo = useCallback((id: string) => {
    setPending((current) => removePendingEdit(current, id));
    if (id.startsWith("chart:")) {
      const key = id.slice("chart:".length);
      setChartDrafts((current) => {
        const next = { ...current };
        delete next[key];
        return next;
      });
    }
  }, []);

  const undoLast = useCallback(() => {
    const last = pending[pending.length - 1];
    if (last) undo(pendingEditId(last));
  }, [pending, undo]);

  const chartInvalid = useMemo(() => {
    const result: Record<string, boolean[][]> = {};
    for (const [key, grid] of Object.entries(chartDrafts)) {
      const element = findElement(inventory, key)?.element;
      const chart = element?.chart;
      if (!chart) continue;
      result[key] = grid.map((row, s) => row.map((text) => text !== undefined && parseChartValue(text, chart.series[s]?.format, locale) === undefined));
    }
    return result;
  }, [chartDrafts, inventory, locale]);
  const hasInvalidChart = Object.values(chartInvalid).some((grid) => grid.some((row) => row.some(Boolean)));
  const hasBrokenField = pending.some((edit) => edit.kind !== "chart" && countFieldTokens(edit.text, fieldToken) !== countFieldTokens(edit.expect, fieldToken));
  const invalid = hasInvalidChart || hasBrokenField;
  // Typed input counts as unsaved even while it is not a valid edit yet.
  const dirty = pending.length > 0 || hasInvalidChart;

  const resetEditing = useCallback(() => {
    setPending([]);
    setChartDrafts({});
    setRefusals({});
    setSaveError(null);
    setConflict(null);
    setReapplied(null);
  }, []);

  const exitWith = useCallback((exit: PresentationEditorExit | null) => {
    pdfAbortRef.current?.abort();
    inventoryAbortRef.current?.abort();
    onExit(exit);
  }, [onExit]);

  const requestExit = useCallback(async (close: boolean, next?: { artifactId: string; edit?: boolean }, after?: () => void) => {
    if (dirty && !(await confirmDiscard(filename))) return;
    const current = saved ? saved.result.artifact_id : base?.artifactId ?? artifactId;
    const target = next?.artifactId ?? current;
    if (target === artifactId && !next?.edit) {
      exitWith({ artifactId, close, page, ...(after ? { then: after } : {}) });
      return;
    }
    exitWith({
      artifactId: target,
      pdfData: render?.artifactId === target && renderState === "ready" ? render.data : undefined,
      close,
      edit: next?.edit,
      // Another file (an upload's copy, a newer version) starts at its first page.
      ...(next ? {} : { page }),
      ...(after ? { then: after } : {}),
    });
  }, [artifactId, base?.artifactId, confirmDiscard, dirty, exitWith, filename, page, render, renderState, saved]);

  useImperativeHandle(ref, () => ({ requestExit: (close: boolean) => { void requestExit(close); } }), [requestExit]);

  const startConflict = useCallback(async (latest: LatestVersionFacts, edits: PendingEdit[]) => {
    setConflict({ latest, rows: null, inventory: null, failed: false });
    try {
      const value = await fetchPresentationInventory({ sessionId, artifactId: latest.artifact_id });
      if (!aliveRef.current) return;
      setConflict((current) => current && { ...current, rows: matchPendingEdits(edits, value), inventory: value });
    } catch {
      if (aliveRef.current) setConflict((current) => current && { ...current, failed: true });
    }
  }, [sessionId]);

  useEffect(() => {
    if (!saved || !focusSavedRef.current || !savedNoticeRef.current) return;
    focusSavedRef.current = false;
    savedNoticeRef.current.focus();
  });

  /** Focus the page itself: where focus rests when the control that held it goes away. */
  const focusStage = useCallback(() => {
    requestAnimationFrame(() => stageRef.current?.focus());
  }, []);

  const save = useCallback(async () => {
    if (!base || !pending.length || saving || invalid || conflict) return;
    // The save button is disabled while saving; keep focus in the editor instead of on the page body.
    if (rootRef.current?.querySelector(".ui-savebar")?.contains(document.activeElement)) stageRef.current?.focus();
    setSaving(true);
    setSaveError(null);
    setRefusals({});
    const edits = pending;
    try {
      const result = await savePresentationEdits({ sessionId, artifactId: base.artifactId, edits: toWireEdits(edits) });
      if (!aliveRef.current) return;
      const previousPdf = render?.artifactId === base.artifactId ? render.data : null;
      const state: SavedState = { result, changes: edits, previousVersion: base.versionNo, inventory: base.inventory, previousPdf, spills: null };
      setSaved(state);
      setShowSavedChanges(false);
      setSelectedKey(null);
      setInPlace(null);
      resetEditing();
      // The save bar goes away: focus the saved summary, which says what was saved.
      focusSavedRef.current = true;
      loadRender(result.artifact_id, (data) => {
        findSpills(state, data).then((spills) => {
          if (aliveRef.current) setSaved((current) => current && current.result.artifact_id === result.artifact_id ? { ...current, spills } : current);
        }, () => {
          // The check is advice only: a failed check shows nothing (design §6.8).
          if (aliveRef.current) setSaved((current) => current && current.result.artifact_id === result.artifact_id ? { ...current, spills: [] } : current);
        });
      });
    } catch (error) {
      if (!aliveRef.current) return;
      const failure = error instanceof PresentationEditError ? error : new PresentationEditError("network");
      setSaveError(failure);
      if (failure.code === "version_target_not_latest" && failure.latest) void startConflict(failure.latest, edits);
      if (failure.code === "edit_refused") {
        const next: Record<string, string> = {};
        for (const item of failure.refused) {
          const edit = item.index === null ? undefined : edits[item.index];
          if (edit) next[pendingEditId(edit)] = item.code;
        }
        setRefusals(next);
      }
    } finally {
      if (aliveRef.current) setSaving(false);
    }
  }, [base, conflict, invalid, loadRender, pending, render, resetEditing, saving, sessionId, startConflict]);

  const reapply = () => {
    if (!conflict?.inventory || !conflict.rows) return;
    const applicable = conflict.rows.filter((row) => row.applicable).map((row) => row.edit);
    const manual = conflict.rows.filter((row) => !row.applicable);
    const latest = conflict.inventory;
    setBase({ artifactId: latest.artifact_id, versionNo: latest.version_no, inventory: latest });
    setPending(applicable);
    setChartDrafts({});
    setRefusals({});
    setSaveError(null);
    setConflict(null);
    setSelectedKey(null);
    setReapplied({ version: latest.version_no, applied: applicable.length, manual });
    loadRender(latest.artifact_id);
  };

  const continueEditing = async (key?: string) => {
    if (!saved) return;
    const value = await loadInventory(saved.result.artifact_id);
    if (!value || !aliveRef.current) return;
    setSaved(null);
    const found = key ? findElement(value, key) : null;
    if (found && key) {
      setPage(found.slide.index);
      focusAfterSelectRef.current = true;
      setSelectedKey(key);
      return;
    }
    // The saved panel's button goes away with it.
    focusStage();
  };

  /** Discards every unsaved change; more than one is confirmed first, since it cannot be undone. */
  const discardAll = async () => {
    if (pending.length > 1) {
      const ok = await confirm({ ...canvasCloseGuardOptions(tObject, filename), description: t("discard_confirm_body", { count: pending.length }) });
      if (!ok) return;
    }
    resetEditing();
    setSelectedKey(null);
    setInPlace(null);
    focusStage();
  };

  const referenceElement = (element: InventoryElement, slideOf: InventorySlide) => {
    const file = base?.inventory.file;
    if (!onReferenceElement || !base || !file) return undefined;
    const selection = canonicalizePresentationSelection({
      version: 1,
      artifactId: base.artifactId,
      slideId: slideOf.slide_id,
      slideIndex: slideOf.index,
      elementKey: element.key,
      elementName: elementLabel(element, t, fieldToken).slice(0, 200),
      elementKind: element.kind,
    });
    if (!selection) return undefined;
    const reference: AttachmentRef = {
      fileId: base.artifactId, filename: file.filename, mime: file.mime, size: file.size, referenceKind: "result", presentationSelection: selection,
    };
    return () => { void requestExit(true, undefined, () => onReferenceElement(reference)); };
  };

  const referencePage = (slideOf: InventorySlide | undefined) => {
    const file = base?.inventory.file;
    if (!onReferenceElement || !base || !file || !slideOf) return undefined;
    const selection = presentationSlideSelection(base.artifactId, slideOf.index, slideOf.slide_id);
    if (!selection) return undefined;
    const reference: AttachmentRef = {
      fileId: base.artifactId, filename: file.filename, mime: file.mime, size: file.size, referenceKind: "result", presentationSelection: selection,
    };
    return () => { void requestExit(true, undefined, () => onReferenceElement(reference)); };
  };

  const describeEdit = useCallback((edit: PendingEdit, source: PresentationInventory | undefined): { location: string; text: string } => {
    const found = findElement(source, edit.key);
    const pageLabel = t("page_label", { page: found?.slide.index ?? 1 });
    const name = found ? elementLabel(found.element, t, fieldToken) : edit.key;
    if (edit.kind === "text") return { location: `${pageLabel} · ${name}`, text: edit.text };
    if (edit.kind === "cell") return { location: `${pageLabel} · ${name} · ${t("cell_label", { row: edit.row + 1, col: edit.col + 1 })}`, text: edit.text };
    const values = edit.expect.series.map((series, index) => `${series.name}: ${edit.values[index].map((value) => formatChartValue(value, series.format, locale)).join(", ")}`);
    return { location: `${pageLabel} · ${name}`, text: values.join("; ") };
  }, [fieldToken, locale, t]);

  const copyMyText = async (edits: ReadonlyArray<PendingEdit>) => {
    const text = edits.map((edit) => {
      const described = describeEdit(edit, inventory);
      return `${described.location}\n${described.text.split(fieldToken).join("#")}`;
    }).join("\n\n");
    const ok = await copyTextWithFallback(text, t("copy_prompt"));
    if (ok) toast({ kind: "info", text: t("copied") });
  };

  const firstEditableCell = (element: InventoryElement): [number, number] | null => {
    const rows = element.rows ?? [];
    for (let row = 0; row < rows.length; row += 1) for (let col = 0; col < rows[row].length; col += 1) if (rows[row][col]?.editable) return [row, col];
    return null;
  };

  /** Starts editing a selected element: on the slide when it is located there, else in the inspector. */
  const beginEdit = (element: InventoryElement, options: { paragraph?: number; cell?: [number, number] | null; typed?: string } = {}) => {
    if (!element.editable || saved || conflict) return;
    if (selectedKey !== element.key) {
      setSelectedKey(element.key);
      setPage(findElement(inventory, element.key)?.slide.index ?? page);
    }
    if (editsInPlace(element)) {
      if (element.kind === "chart") {
        setInPlace({ kind: "chart", key: element.key });
        return;
      }
      if (element.kind === "text") {
        const paragraphs = located.texts.get(element.key)?.paragraphs ?? [];
        const paragraph = options.paragraph ?? (options.typed ? paragraphs[paragraphs.length - 1]?.index : paragraphs[0]?.index);
        if (paragraph !== undefined) setInPlace({ kind: "text", key: element.key, paragraph, typed: options.typed });
        return;
      }
      if (element.kind === "table") {
        const cell = options.cell && element.rows?.[options.cell[0]]?.[options.cell[1]]?.editable ? options.cell : firstEditableCell(element);
        if (cell) setInPlace({ kind: "cell", key: element.key, row: cell[0], col: cell[1], typed: options.typed });
        return;
      }
    }
    // Not located on the render: the inspector's field opens with the caret at the end.
    typedRef.current = options.typed ?? null;
    setFocusCell(element.kind === "table" && options.cell ? options.cell : null);
    focusAfterSelectRef.current = true;
    setFocusTick((tick) => tick + 1);
  };

  const closeInPlace = useCallback(() => {
    setInPlace((current) => {
      // Text and cells return to the selection with their field kept, hidden and focused, so typing
      // again continues there (design §13.4); a chart's data returns focus to the chart.
      if (current && current.kind !== "chart") return { ...current, typed: undefined, armed: true };
      if (current) focusHitRef.current = current.key;
      return null;
    });
    // Back to the selection: focus returns to the element on the slide.
    setFocusTick((tick) => tick + 1);
  }, []);

  const moveCell = (element: InventoryElement, row: number, col: number, move: CellMove) => {
    const rows = element.rows ?? [];
    const width = rows[0]?.length ?? 0;
    let r = row;
    let c = col;
    for (let step = 0; step < rows.length * width; step += 1) {
      if (move === "down") r += 1;
      else if (move === "up") r -= 1;
      else if (move === "next") { c += 1; if (c >= width) { c = 0; r += 1; } }
      else { c -= 1; if (c < 0) { c = width - 1; r -= 1; } }
      if (r < 0 || r >= rows.length) break;
      if (rows[r][c]?.editable) {
        setInPlace({ kind: "cell", key: element.key, row: r, col: c });
        return;
      }
    }
    closeInPlace();
  };

  /** Where on the slide a pointer event landed, in fractions of the page. */
  const pointOnPage = (event: ReactMouseEvent): { x: number; y: number } | null => {
    const rect = pageRef.current?.getBoundingClientRect();
    if (!rect || !rect.width || !rect.height) return null;
    return { x: (event.clientX - rect.left) / rect.width, y: (event.clientY - rect.top) / rect.height };
  };

  const clickedTarget = (element: InventoryElement, point: { x: number; y: number } | null) => {
    if (!point) return {};
    if (element.kind === "text") {
      const paragraphs = located.texts.get(element.key)?.paragraphs ?? [];
      const hit = paragraphs.find((paragraph) => point.y >= paragraph.top - paragraph.size * 0.3 && point.y <= paragraph.bottom + paragraph.size * 0.3)
        ?? [...paragraphs].sort((a, b) => Math.abs((a.top + a.bottom) / 2 - point.y) - Math.abs((b.top + b.bottom) / 2 - point.y))[0];
      return hit ? { paragraph: hit.index } : {};
    }
    if (element.kind === "table") {
      const table = located.tables.get(element.key);
      if (!table) return {};
      const col = table.xs.findIndex((x, index) => index < table.xs.length - 1 && point.x >= x && point.x <= table.xs[index + 1]);
      const row = table.ys.findIndex((y, index) => index < table.ys.length - 1 && point.y >= y && point.y <= table.ys[index + 1]);
      return row >= 0 && col >= 0 ? { cell: [row, col] as [number, number] } : {};
    }
    return {};
  };

  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    // Keys inside an input method's composition (Esc cancels it, arrows pick) are not the editor's.
    if (isComposing(event)) return;
    const target = event.target as HTMLElement;
    // A selection's hidden field is not typing: ⌘Z and PageUp/PageDown act on the editor.
    const typing = target.matches("input, textarea, [contenteditable='true']") && !target.dataset.armed;
    const mod = event.metaKey || event.ctrlKey;
    if (event.key === "Escape" && !event.defaultPrevented) {
      if (target.closest("[role='dialog']") !== rootRef.current?.closest("[role='dialog']")) return;
      event.preventDefault();
      event.stopPropagation();
      if (inPlace && !(inPlace.kind !== "chart" && inPlace.armed)) closeInPlace();
      else if (selectedKey) clearSelection();
      else void requestExit(false);
      return;
    }
    if (mod && !event.shiftKey && !event.altKey && event.key.toLowerCase() === "s") {
      event.preventDefault();
      void save();
      return;
    }
    if (mod && !event.shiftKey && !event.altKey && event.key.toLowerCase() === "z" && !typing && !saved) {
      event.preventDefault();
      undoLast();
      return;
    }
    if ((event.key === "PageUp" || event.key === "PageDown") && !typing && !phone) {
      event.preventDefault();
      const next = Math.min(slideCount, Math.max(1, page + (event.key === "PageDown" ? 1 : -1)));
      if (next !== page) {
        setPage(next);
        setSelectedKey(null);
        setInPlace(null);
        // The focused element belongs to the page that is leaving: keep focus in the editor so the
        // next PageUp or PageDown still turns the page.
        requestAnimationFrame(() => {
          const railPage = rootRef.current?.querySelector<HTMLElement>(`[data-rail-page="${next}"]`);
          if (railPage && railPage.offsetParent !== null) railPage.focus();
          else stageRef.current?.focus();
        });
      }
    }
  };

  const editingMode = !saved;
  /** Each changed element's number: the outline on the slide and the change list share it. */
  const numbers = useMemo(() => {
    const map = new Map<string, number>();
    for (const edit of pending) if (!map.has(edit.key)) map.set(edit.key, map.size + 1);
    return map;
  }, [pending]);
  const pendingPages = new Set(pending.map((edit) => findElement(inventory, edit.key)?.slide.index ?? 0));
  const savedKeys = saved ? [...new Set(saved.changes.map((edit) => edit.key))] : [];
  const newFile = base?.inventory.edit_mode === "new_file";

  const latestTime = (latest: LatestVersionFacts) => {
    const date = new Date(latest.created_at);
    if (Number.isNaN(date.getTime())) return "";
    const today = new Date();
    return date.toDateString() === today.toDateString()
      ? formatTime(date, locale)
      : formatDateTime(date, locale, { dateStyle: "short", timeStyle: "short" });
  };

  const conflictMessage = (latest: LatestVersionFacts) => {
    const values = { base: base?.versionNo ?? 1, version: latest.version_no, time: latestTime(latest), request: latest.request_excerpt ?? "" };
    if (latest.produced_by === "user") return t("conflict_user", values);
    return latest.request_excerpt ? t("conflict_agent", values) : t("conflict_agent_no_request", values);
  };

  const saveErrorMessage = (error: PresentationEditError): string => {
    if (error.code === "version_target_not_latest" && error.latest) return conflictMessage(error.latest);
    if (error.code === "results_scope_unavailable" || error.code === "presentation_not_editable") return t("error_permission");
    if (error.code === "edit_refused") return t("error_refused", { count: Math.max(1, error.refused.length) });
    if (error.code === "presentation_unreadable") return t("error_unreadable");
    if (error.code === "too_large") return t("error_too_large");
    if (error.code === "parse_unavailable" || error.code === "file_unavailable" || error.status === 503) return t("error_unavailable");
    return t("save_failed");
  };

  const inventoryErrorBlock = (error: PresentationEditError) => {
    if (error.code === "version_target_not_latest" && error.latest) {
      const latest = error.latest;
      return (
        <StateBlock tone="error" title={t("not_latest_title", { version: latest.version_no })}
          body={t("not_latest_body", { version: latest.version_no })}
          primaryAction={{ label: t("open_latest", { version: latest.version_no }), onClick: () => { void requestExit(false, { artifactId: latest.artifact_id }); } }} />
      );
    }
    const body = error.code === "presentation_unreadable" ? t("error_unreadable")
      : error.code === "too_large" ? t("error_too_large")
        : error.code === "results_scope_unavailable" || error.code === "presentation_not_editable" || error.code === "presentation_edit_unavailable" ? t("inventory_not_editable")
          : t("inventory_failed_body");
    return (
      <>
        <StateBlock tone="error" title={t("inventory_failed_title")} body={body}
          primaryAction={{ label: t("retry"), onClick: () => { void loadInventory(base?.artifactId ?? artifactId); } }}
          secondaryActions={[{ label: t("download"), onClick: () => { window.open(downloadHref, "_blank", "noopener"); } }]} />
        <ErrorNotice message={body} surface="task" diagnostics={error.diagnostics} liveRole="status" />
      </>
    );
  };

  const statusLine = saved
    ? `${saved.result.new_file ? t("saved_new_file", { filename: saved.result.filename }) : t("saved_version", { version: saved.result.version_no })}${renderState === "loading" ? ` · ${t("updating_preview")}` : ""}`
    : !base
      ? inventoryError
        ? inventoryError.code === "version_target_not_latest" && inventoryError.latest
          ? t("not_latest_title", { version: inventoryError.latest.version_no })
          : t("inventory_failed_title")
        : t("loading_inventory")
      : `${newFile ? t("editing_new_file") : t("editing_version", { version: base.versionNo })}${dirty ? "" : ` · ${t("no_changes_yet")}`}`;

  const inspector = (() => {
    if (inventoryError && !saved) return inventoryErrorBlock(inventoryError);
    if (!base || (inventoryLoading && !saved)) return <StateBlock tone="progress" title={t("loading_inventory")} />;
    if (saved) {
      const previous = saved.previousVersion;
      return (
        <div className={styles.panel}>
          <div ref={savedNoticeRef} tabIndex={-1} className={styles.savedNotice}>
          <Notice tone="info">
            {saved.result.new_file
              ? t("saved_summary_new_file", { count: saved.changes.length, filename: saved.result.filename })
              : t("saved_summary", { count: saved.changes.length, version: saved.result.version_no, previous })}
          </Notice>
          </div>
          {renderState === "failed" || renderState === "busy" ? (
            <ErrorNotice
              message={t(renderState === "busy" ? "render_busy" : "render_failed", { version: saved.result.version_no })}
              surface="task"
              action={{ kind: "retry", label: t("retry_preview") }}
              onAction={() => loadRender(saved.result.artifact_id, (data) => {
                findSpills(saved, data).then((spills) => { if (aliveRef.current) setSaved((current) => current && { ...current, spills }); }, () => undefined);
              })}
            />
          ) : null}
          {(saved.spills ?? []).map((spill) => {
            const found = findElement(saved.inventory, spill.key);
            return (
              <ReviewLine key={spill.key} actions={[{ label: t("go_edit"), onSelect: () => { void continueEditing(spill.key); } }]}>
                {t("spill", { page: spill.page, name: found ? excerpt(elementText(found.element, saved.changes), fieldToken) : "" })}
              </ReviewLine>
            );
          })}
          <div className={styles.savedActions}>
            <Button onClick={() => setShowSavedChanges((value) => !value)} aria-expanded={showSavedChanges}>
              {t(showSavedChanges ? "hide_changes" : "view_changes")}
            </Button>
            <Button variant="primary" onClick={() => { void continueEditing(); }} loading={inventoryLoading}>{t("edit_again")}</Button>
          </div>
          {showSavedChanges ? (
            <section className={styles.changes}>
              {saved.changes.map((edit) => {
                const described = describeEdit(edit, saved.inventory);
                const before = edit.kind === "chart" ? describeEdit({ ...edit, values: edit.expect.series.map((series) => series.values) }, saved.inventory).text : edit.expect;
                return (
                  <ChangeRow key={pendingEditId(edit)} location={described.location}
                    before={before ? { kind: "text", text: excerpt(before, fieldToken, 160) } : { kind: "empty" }}
                    after={described.text ? { kind: "text", text: excerpt(described.text, fieldToken, 160) } : { kind: "empty" }} />
                );
              })}
            </section>
          ) : null}
          {inventoryError ? <ErrorNotice message={t("inventory_failed_body")} surface="task" diagnostics={inventoryError.diagnostics} /> : null}
        </div>
      );
    }
    if (conflict) {
      const { latest, rows } = conflict;
      const applicable = rows?.filter((row) => row.applicable).length ?? 0;
      return (
        <div className={styles.panel}>
          <ErrorNotice message={conflictMessage(latest)} surface="task" />
          <section className={styles.changes} aria-label={t("conflict_rows_title", { count: pending.length })}>
            <h4 className={styles.changesTitle}>{t("conflict_rows_title", { count: pending.length })}</h4>
            {rows === null && !conflict.failed ? <p className={styles.panelHint} role="status">{t("conflict_checking", { version: latest.version_no })}</p> : null}
            {conflict.failed ? <p className={styles.panelHint} role="status">{t("conflict_check_failed", { version: latest.version_no })}</p> : null}
            {(rows ?? pending.map((edit) => ({ edit, applicable: false, latestText: null }))).map((row) => {
              const described = describeEdit(row.edit, inventory);
              const before = row.edit.kind === "chart" ? "" : row.edit.expect;
              return (
                <ChangeRow key={pendingEditId(row.edit)} location={described.location}
                  badge={rows ? <Badge tone={row.applicable ? "positive" : "warning"} size="sm">{t(row.applicable ? "conflict_applicable" : "conflict_manual")}</Badge> : undefined}
                  before={before ? { kind: "text", text: excerpt(before, fieldToken, 120) } : undefined}
                  after={{ kind: "text", text: excerpt(described.text, fieldToken, 120) }}
                  rows={rows ? [{
                    label: t("conflict_note_label", { version: latest.version_no }),
                    value: row.applicable ? t("conflict_applicable_note", { version: latest.version_no })
                      : row.latestText !== null ? t("conflict_manual_note", { version: latest.version_no, text: excerpt(row.latestText, fieldToken, 60) })
                        : t("conflict_manual_missing", { version: latest.version_no }),
                  }] : undefined} />
              );
            })}
          </section>
          <div className={styles.conflictActions}>
            <Button variant="primary" disabled={!rows || applicable === 0} onClick={reapply}>
              {t("reapply", { version: latest.version_no, count: applicable })}
            </Button>
            <p className={styles.panelHint}>{t("reapply_note", { version: latest.version_no, next: latest.version_no + 1 })}</p>
            <Button onClick={() => { void copyMyText(pending); }}>{t("copy_text")}</Button>
            <Button onClick={() => { void requestExit(false, { artifactId: latest.artifact_id }); }}>{t("open_latest", { version: latest.version_no })}</Button>
          </div>
        </div>
      );
    }
    if (!slide) return null;
    if (selected) {
      const back = clearSelection;
      if (!selected.editable) {
        return <UnsupportedPanel element={selected} page={slide.index} fieldToken={fieldToken} downloadHref={downloadHref} phone={phone}
          onReference={onReference ? () => { void requestExit(true, undefined, onReference); } : undefined}
          onReferenceElement={referenceElement(selected, slide)} onBack={back} />;
      }
      const inPlaceHere = editsInPlace(selected);
      if (selected.kind === "text") {
        return <TextPanel element={selected} page={slide.index} pending={pending} refusals={refusals} fieldToken={fieldToken}
          onChange={(paragraph, text) => updateText(selected, slide.slide_id, paragraph, text)} onUndo={undo} onBack={back}
          onReference={referenceElement(selected, slide)} inPlace={inPlaceHere} sheet={phone} />;
      }
      if (selected.kind === "table") {
        return <TablePanel element={selected} page={slide.index} pending={pending} refusals={refusals} fieldToken={fieldToken}
          onChange={(row, col, text) => updateCell(selected, slide.slide_id, row, col, text)} onUndo={undo} onBack={back}
          onReference={referenceElement(selected, slide)} inPlace={inPlaceHere} sheet={phone}
          rowForm={phone && (selected.rows?.[0]?.length ?? 0) > PHONE_TABLE_COLUMNS} focusCell={focusCell} />;
      }
      if (selected.kind === "chart") {
        return <ChartPanel element={selected} page={slide.index} slide={slide} pending={pending} drafts={chartDrafts[selected.key]}
          invalid={chartInvalid[selected.key] ?? []} locale={locale} fieldToken={fieldToken}
          onDraft={(series, category, text) => updateChart(selected, slide.slide_id, series, category, text)}
          onUndo={undo} onBack={back} onSelect={select} onReference={referenceElement(selected, slide)} inPlace={inPlaceHere} sheet={phone} />;
      }
    }
    return <ElementList slide={slide} pending={pending} numbers={numbers} fieldToken={fieldToken} onSelect={select}
      onReferencePage={referencePage(slide)} keyboardHint={t("keyboard_hint", { undo: undoKey })} />;
  })();

  const notices = editingMode && base && !inventoryError ? (
    <>
      {!guideDismissed ? (
        <div className="rc-guide" role="status">
          <LucideGlyph icon={LucideLightbulb} size={14} aria-hidden="true" />
          <span className="rc-guide-text">{t(phone ? "guide_text_touch" : "guide_text")}</span>
          <button type="button" className="rc-guide-dismiss" onClick={() => {
            setGuideDismissed(true);
            try { window.localStorage.setItem(GUIDE_KEY, "1"); } catch { /* Private mode: shown again next time. */ }
          }}>{t("guide_dismiss")}</button>
        </div>
      ) : null}
      {newFile ? <Notice tone="info">{t("guide_upload", { filename: base.inventory.new_filename ?? filename })}</Notice> : null}
      {newFile && base.inventory.edited_copies[0] ? (
        <div className={styles.guide} role="note">
          <span>{t("edited_copy_exists", { filename: base.inventory.edited_copies[0].filename })}</span>
          <Button size="sm" onClick={() => { void requestExit(false, { artifactId: base.inventory.edited_copies[0].artifact_id, edit: true }); }}>
            {t("open_edited_copy")}
          </Button>
        </div>
      ) : null}
      {agentRunning ? <Notice tone="muted">{t("agent_running")}</Notice> : null}
      {reapplied ? (
        <Notice tone="info">
          {t("reapplied", { version: reapplied.version, count: reapplied.applied })}
          {reapplied.manual.length ? (
            <span className={styles.manualList}>
              {t("reapplied_manual", { count: reapplied.manual.length })}
              {reapplied.manual.map((row) => {
                const described = describeEdit(row.edit, inventory);
                return <span key={pendingEditId(row.edit)}>{described.location}{t("label_separator")}{t("quoted", { text: excerpt(described.text, fieldToken, 60) })}</span>;
              })}
              <Button size="sm" variant="plain" onClick={() => { void copyMyText(reapplied.manual.map((row) => row.edit)); }}>{t("copy_text")}</Button>
            </span>
          ) : null}
        </Notice>
      ) : null}
      {saveError && !conflict ? (
        <ErrorNotice
          message={saveErrorMessage(saveError)}
          surface="task"
          diagnostics={saveError.diagnostics}
          action={saveError.code === "results_scope_unavailable" || saveError.code === "presentation_not_editable" ? { kind: "retry", label: t("copy_text") } : undefined}
          onAction={() => { void copyMyText(pending); }}
        />
      ) : null}
    </>
  ) : null;

  /** An element's name on the slide: kind, text (the new text when changed), and its unsaved number. */
  const hitLabel = (element: InventoryElement) => {
    const edits = pending.filter((edit) => edit.key === element.key);
    const number = numbers.get(element.key);
    // The name carries the new text, so a screen reader hears what the painted text shows.
    const name = element.kind === "text" && edits.length ? excerpt(elementText(element, edits), fieldToken) : elementLabel(element, t, fieldToken);
    return `${t(`kind_${element.kind}`)}${t("label_separator")}${name}${element.editable ? "" : ` · ${t("unsupported_badge")}`}${number ? ` · ${t("unsaved_numbered", { number })}` : ""}`;
  };
  const overlayFor = (slideOf: InventorySlide | undefined, onStage = false) => slideOf ? slideOf.elements.map((element) => {
    if (!element.box) return null;
    const [left, top, width] = element.box;
    // A table's hit area follows its rendered rows, which can run past the file's box.
    const table = onStage ? located.tables.get(element.key) : undefined;
    const height = table ? table.ys[table.ys.length - 1] - top : element.box[3];
    const style = { left: `${left * 100}%`, top: `${top * 100}%`, width: `${width * 100}%`, height: `${height * 100}%` };
    if (saved) {
      const number = savedKeys.indexOf(element.key);
      if (number < 0) return null;
      return (
        <span key={element.key} className={styles.updating} style={style} data-ready={renderState === "ready" || undefined}>
          <span className={styles.updatingBadge} aria-hidden="true">{number + 1}</span>
        </span>
      );
    }
    const edits = pending.filter((edit) => edit.key === element.key);
    const number = numbers.get(element.key);
    const label = hitLabel(element);
    const isSelected = selectedKey === element.key;
    const armedHere = onStage && inPlace?.key === element.key && inPlace.kind !== "chart" && Boolean(inPlace.armed);
    return (
      <button
        key={element.key}
        type="button"
        className={styles.hit}
        style={style}
        data-element-key={element.key}
        data-editable={element.editable || undefined}
        data-pending={edits.length ? true : undefined}
        data-selected={isSelected || undefined}
        data-editing={(inPlace?.key === element.key && !(inPlace.kind !== "chart" && inPlace.armed)) || undefined}
        // While its hidden field holds focus, the field stands for the element: no second Tab stop or name.
        aria-hidden={armedHere || undefined}
        tabIndex={armedHere ? -1 : undefined}
        aria-label={label}
        aria-pressed={isSelected}
        onFocus={(event) => {
          // Tab selects, as in slide tools: a box reached by keyboard takes typing and input methods
          // at once (design §13.4). Focus that code moves here, or a mouse press, does not select.
          if (!onStage || quietFocusRef.current) return;
          let keyboard = false;
          try { keyboard = event.currentTarget.matches(":focus-visible"); } catch { keyboard = false; }
          // Coming back by keyboard to the selection re-arms its hidden field as well.
          if (keyboard && (!isSelected || inPlace?.key !== element.key)) select(element.key);
        }}
        onClick={(event) => {
          // A click selects; a click on the selection starts editing where it landed (design §13.3).
          if (!onStage || !isSelected) {
            select(element.key, onStage ? clickedTarget(element, pointOnPage(event)).cell : null);
            return;
          }
          if (element.editable) beginEdit(element, clickedTarget(element, pointOnPage(event)));
        }}
        onKeyDown={(event) => {
          if (!onStage || !element.editable || event.metaKey || event.ctrlKey || event.altKey || isComposing(event)) return;
          if (event.key === "Enter") {
            event.preventDefault();
            beginEdit(element);
          } else if (event.key.length === 1 && event.key !== " " && (element.kind !== "chart" || /[0-9.,%-]/.test(event.key))) {
            // Space stays the button's own key (it selects, then starts editing), never a typed space.
            event.preventDefault();
            beginEdit(element, { typed: event.key });
          }
        }}
      >
        {number ? <span className={styles.pendingNumber} aria-hidden="true">{number}</span> : null}
      </button>
    );
  }) : null;
  const stageOverlay = pageWidth > 0 ? overlayFor(slide, true) : null;
  const pageHeight = Math.round(pageWidth / aspect);
  const inPlaceElement = inPlace ? slide?.elements.find((element) => element.key === inPlace.key) : undefined;
  const chartPopover = inPlace?.kind === "chart" && inPlaceElement?.chart && inPlaceElement.box && slide ? (() => {
    const box = inPlaceElement.box;
    const popoverWidth = 320;
    const right = (box[0] + box[2]) * pageWidth + 12;
    const left = right + popoverWidth <= pageWidth + 8 ? right : Math.max(8, box[0] * pageWidth - popoverWidth - 12);
    return (
      <ChartPopover label={t("chart_popover_label", { name: elementLabel(inPlaceElement, t, fieldToken) })} left={left} top={Math.max(8, box[1] * pageHeight)}
        width={popoverWidth} title={elementLabel(inPlaceElement, t, fieldToken)}
        meta={t("chart_meta", { page: slide.index, series: inPlaceElement.chart.series.length, categories: inPlaceElement.chart.categories.length })}
        hint={t("chart_hint")} doneLabel={t("done")} onClose={closeInPlace}
        onLeave={() => setInPlace((current) => current?.kind === "chart" ? null : current)}>
        <ChartGrid element={inPlaceElement} pending={pending} drafts={chartDrafts[inPlaceElement.key]} invalid={chartInvalid[inPlaceElement.key] ?? []}
          locale={locale} fieldToken={fieldToken} onDraft={(series, category, text) => updateChart(inPlaceElement, slide.slide_id, series, category, text)} />
      </ChartPopover>
    );
  })() : null;
  // A selection's hidden field is named like the element on the slide; while editing, a cell by its place.
  const inPlaceLabel = (element: InventoryElement, target: InPlaceTarget) => target.kind !== "chart" && target.armed ? hitLabel(element) : target.kind === "cell"
    ? `${elementLabel(element, t, fieldToken)} · ${t("cell_label", { row: target.row + 1, col: target.col + 1 })}${target.row > 0 && element.rows?.[0]?.[target.col]?.text ? ` · ${excerpt(element.rows[0][target.col].text, fieldToken, 24)}` : ""}`
    : `${t(`kind_${element.kind}`)}${t("label_separator")}${elementLabel(element, t, fieldToken)}`;

  const showPage = Boolean(pdf) && pageWidth > 0 && (renderState === "ready" || (Boolean(saved) && renderState === "loading"));
  const renderVisible = Boolean(pdf) && (renderState === "ready" || (Boolean(saved) && renderState === "loading"));
  const undoElement = (key: string) => {
    setPending((current) => current.filter((edit) => edit.key !== key));
    setChartDrafts((current) => {
      if (!(key in current)) return current;
      const next = { ...current };
      delete next[key];
      return next;
    });
  };
  const stageState = (
    <div className={styles.stageState}>
      {renderState === "loading" || (renderState === "ready" && !pdf) ? <StateBlock tone="progress" title={saved && renderState === "loading" ? (saved.result.new_file ? t("updating_new_file") : t("updating_region", { version: saved.result.version_no })) : t("loading_page")} />
        : renderState === "busy" || renderState === "failed" ? (
          <StateBlock tone="error" title={t(renderState === "busy" ? "render_busy_title" : "render_failed_title")}
            body={t("render_failed_body")}
            primaryAction={{ label: t("retry_preview"), onClick: () => loadRender(saved?.result.artifact_id ?? base?.artifactId ?? artifactId) }}
            secondaryActions={[{ label: t("download"), onClick: () => { window.open(downloadHref, "_blank", "noopener"); } }]} />
        ) : null}
    </div>
  );
  // Phones (design §7.3): the slides are a list; each slide opens a sheet of its elements, and an element opens its editor as a sheet.
  const phoneInline = inventoryError || !base || inventoryLoading || saved || conflict ? inspector : null;
  const phoneBody = (
    <div className={styles.phoneBody} ref={phoneBodyRef}>
      {notices}
      {phoneInline}
      {renderVisible && phoneWidth > 0 ? Array.from({ length: slideCount }, (_, index) => index + 1).map((number) => {
        const slideOf = inventory?.slides[number - 1];
        const editable = slideOf?.elements.filter((element) => element.editable).length ?? 0;
        const changed = slideOf ? new Set(pending.filter((edit) => edit.slideId === slideOf.slide_id).map((edit) => edit.key)).size : 0;
        return (
          <section key={number} className={styles.phonePage} aria-label={t("page_label", { page: number })}>
            <div className={styles.page} style={{ width: phoneWidth, height: Math.round(phoneWidth / aspect) }} data-editing={editingMode || undefined}>
              <LazyPdfPageCanvas pdf={pdf} pageNumber={number} width={phoneWidth} label={`${filename} · ${t("page_label", { page: number })}`} className={styles.pageCanvas} />
              <div className={styles.overlay}>{overlayFor(slideOf)}</div>
            </div>
            {editingMode && !conflict && slideOf && base && !inventoryLoading ? (
              <div className={styles.phonePageActions}>
                <Button className={styles.phoneEditPage} onClick={() => { setPage(number); setSelectedKey(null); setSheetPage(number); }}>
                  {t("phone_edit_page", { page: number, count: editable })}
                </Button>
                {referencePage(slideOf) ? (
                  <Button variant="secondary" className={styles.phoneReferencePage} onClick={referencePage(slideOf)}
                    aria-label={t("reference_page_label", { page: number })}>
                    {t("reference_page")}
                  </Button>
                ) : null}
              </div>
            ) : null}
            {changed ? <p className={styles.phonePending}>{t("phone_page_pending", { count: changed })}</p> : null}
          </section>
        );
      }) : stageState}
    </div>
  );
  const sheetSlide = sheetPage !== null ? inventory?.slides[sheetPage - 1] : undefined;
  const phoneSheets = phone && editingMode && !conflict && base ? (
    <>
      {sheetSlide && !selected ? (
        <Dialog presentation="sheet" layer="top" title={t("page_list_title", { page: sheetSlide.index })} closeLabel={t("close")}
          onClose={() => setSheetPage(null)}>
          <div className={styles.phoneSheet}>
            <ElementList slide={sheetSlide} pending={pending} numbers={numbers} fieldToken={fieldToken} onSelect={select} hideHeading />
          </div>
        </Dialog>
      ) : null}
      {selected ? (
        <Dialog presentation="sheet" layer="top" title={elementLabel(selected, t, fieldToken)} closeLabel={t("close")}
          onClose={() => setSelectedKey(null)}
          footer={(
            <div className={styles.phoneSheetFooter}>
              <Button disabled={!pending.some((edit) => edit.key === selected.key) && !chartDrafts[selected.key]}
                onClick={() => { undoElement(selected.key); setSelectedKey(null); }}>
                {t("undo_here")}
              </Button>
              <Button variant="primary" onClick={() => { setSelectedKey(null); setSheetPage(null); }}>{t("done")}</Button>
            </div>
          )}>
          <div className={styles.phoneSheet} ref={phoneSheetRef}>{inspector}</div>
        </Dialog>
      ) : null}
    </>
  ) : null;
  const saveLabel = newFile ? t("save_new_file") : t("save_version");
  const saveMessage = conflict ? t("conflict_bar", { version: conflict.latest.version_no })
    : saving ? t("saving")
      : saveError && saveError.code !== "edit_refused" ? t("save_failed")
        : invalid ? t("invalid_pending")
          : t("pending_count", { count: pending.length });

  return (
    <div ref={rootRef} className={styles.editor} onKeyDown={onKeyDown} data-testid="presentation-editor">
      <div className={`artifact-viewer-head ${styles.head}`}>
        <div className="artifact-viewer-title">
          <h2 className={styles.title}><strong title={filename}>{filename}</strong></h2>
          <span role="status">{statusLine}</span>
        </div>
        <div className={styles.headActions}>
          {dirty || saving || (saveError && dirty) ? <SaveState state={saving ? "saving" : saveError && !conflict ? "failed" : "dirty"} /> : null}
          {saved && renderState === "ready" ? <Badge tone="positive" size="sm">{t("preview_updated")}</Badge> : null}
          <Button onClick={() => { void requestExit(false); }}>{t("exit")}</Button>
          <button type="button" className={styles.closeButton} aria-label={t("close")} title={t("close")} onClick={() => { void requestExit(true); }}>
            <X size={13} aria-hidden="true" />
          </button>
        </div>
      </div>
      {phone ? phoneBody : <div className={styles.body}>
        {wideRail ? <nav className={styles.rail} aria-label={t("pages")}>
          <span className={styles.railCount}>{t("page_count", { count: slideCount })}</span>
          <ol className={styles.railList}>
            {Array.from({ length: slideCount }, (_, index) => index + 1).map((number) => (
              <li key={number}>
                <button type="button" className={styles.railPage} aria-current={number === page ? "page" : undefined}
                  aria-label={`${t("page_label", { page: number })}${pendingPages.has(number) ? ` · ${t("unsaved_badge")}` : ""}`}
                  data-rail-page={number}
                  tabIndex={number === page ? 0 : -1}
                  onKeyDown={(event) => {
                    // The rail is one Tab stop; the arrow keys, Home and End move between pages.
                    const next = event.key === "ArrowDown" || event.key === "ArrowRight" ? number + 1
                      : event.key === "ArrowUp" || event.key === "ArrowLeft" ? number - 1
                        : event.key === "Home" ? 1 : event.key === "End" ? slideCount : null;
                    if (next === null || next < 1 || next > slideCount) return;
                    event.preventDefault();
                    setPage(next);
                    setSelectedKey(null);
                    setInPlace(null);
                    requestAnimationFrame(() => rootRef.current?.querySelector<HTMLElement>(`[data-rail-page="${next}"]`)?.focus());
                  }}
                  onClick={() => { setPage(number); setSelectedKey(null); setInPlace(null); }}>
                  <LazyPdfPageCanvas pdf={pdf} pageNumber={number} width={104} aspectRatio={aspect} className={styles.railCanvas} />
                  <span className={styles.railNumber}>{number}</span>
                  {pendingPages.has(number) ? <span className={styles.railDot} aria-hidden="true" /> : null}
                </button>
              </li>
            ))}
          </ol>
        </nav> : null}
        <div className={styles.stageColumn}>
          <div className={styles.pageSwitcher}>
            <Button size="sm" variant="ghost" aria-label={t("previous_page")} disabled={page <= 1} onClick={() => { setPage(page - 1); setSelectedKey(null); }}>
              <ChevronLeft size={14} aria-hidden="true" />
            </Button>
            <span aria-live="polite">{t("page_of", { page, count: slideCount })}</span>
            <Button size="sm" variant="ghost" aria-label={t("next_page")} disabled={page >= slideCount} onClick={() => { setPage(page + 1); setSelectedKey(null); }}>
              <ChevronRight size={14} aria-hidden="true" />
            </Button>
          </div>
          <div className={styles.stage} ref={stageRef} tabIndex={-1} role="group" aria-label={t("page_of", { page, count: slideCount })}>
            {showPage ? (
              <div ref={pageRef} className={styles.page} style={{ width: pageWidth, height: pageHeight }} data-editing={editingMode || undefined}
                data-layout-ready={pageLayout?.page === page || undefined}>
                <PdfPageCanvas pdf={pdf} pageNumber={page} width={pageWidth} label={`${filename} · ${t("page_label", { page })}`} className={styles.pageCanvas}
                  onDrawn={(canvas) => setDrawn((current) => ({ page, canvas, stamp: (current?.stamp ?? 0) + 1 }))} />
                {pageLayout && slide && editingMode && !conflict ? (
                  <InPlaceLayer elements={slide.elements} layout={pageLayout} texts={located.texts} tables={located.tables} pending={pending}
                    page={{ width: pageWidth, height: pageHeight }} editing={inPlace?.kind === "chart" ? null : inPlace} fieldToken={fieldToken}
                    labelFor={inPlaceLabel}
                    onText={(element, paragraph, text) => updateText(element, slide.slide_id, paragraph, text)}
                    onCell={(element, row, col, text) => updateCell(element, slide.slide_id, row, col, text)}
                    onMove={moveCell} onClose={closeInPlace}
                    onStart={() => setInPlace((current) => current && current.kind !== "chart" && current.armed ? { ...current, armed: false } : current)}
                    onDisarm={() => setInPlace((current) => current && current.kind !== "chart" && current.armed ? null : current)}
                    onTabAway={() => { if (inPlace) focusHitQuietly(inPlace.key); }}
                    armedHint={t(inPlace?.kind === "cell" ? "in_place_table_hint" : "in_place_text_hint")} />
                ) : null}
                <div className={styles.overlay}>{stageOverlay}</div>
                {chartPopover}
                {saved && renderState === "loading" ? (
                  <span className={styles.updatingStatus} role="status">{saved.result.new_file ? t("updating_new_file") : t("updating_region", { version: saved.result.version_no })}</span>
                ) : null}
              </div>
            ) : stageState}
          </div>
        </div>
        <aside className={styles.inspector} ref={inspectorRef} aria-label={t("inspector")}>
          {notices}
          {inspector}
        </aside>
      </div>}
      {editingMode ? (
        <div className={styles.footer} data-covered={(phone && Boolean(sheetSlide || selected)) || undefined}>
          <SaveBar
            dirty={dirty}
            saving={saving}
            failed={Boolean(saveError && !conflict && saveError.code !== "edit_refused")}
            onSave={() => { void save(); }}
            onDiscard={() => { void discardAll(); }}
            saveDisabled={invalid || Boolean(conflict)}
            saveLabel={saveError && !conflict && saveError.code !== "edit_refused" ? tObject("retry_save") : saveLabel}
            discardLabel={conflict ? t("discard_mine") : t("discard")}
            message={saveMessage}
          />
        </div>
      ) : null}
      {phoneSheets}
      {dialog}
      {discardDialog}
    </div>
  );
});
