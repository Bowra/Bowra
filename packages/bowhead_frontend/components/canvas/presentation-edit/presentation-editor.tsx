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
import { ChangeRow, SaveState, StateBlock, useCanvasCloseGuard } from "@/components/canvas/object";
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
import { canonicalizePresentationSelection } from "@/lib/presentation-selection";
import type { AttachmentRef } from "@/lib/types";
import { toast } from "@/lib/use-toast";
import { fetchPresentationPdf, isPresentationPreviewBusy } from "../artifact-document-preview";
import { ChartPanel, ElementList, TablePanel, TextPanel, UnsupportedPanel, elementLabel, excerpt } from "./inspector";
import { LazyPdfPageCanvas, openPdfDocument, PdfPageCanvas, readPageText, usePdfDocument } from "./pdf-page";
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
  agentRunning?: boolean;
  onReference?: () => void;
  /** Reference one element with its slide in chat (design §6.9); the editor leaves the full view first. */
  onReferenceElement?: (reference: AttachmentRef) => void;
  onExit: (exit: PresentationEditorExit | null) => void;
}>(function PresentationEditor({
  sessionId, artifactId, filename, downloadHref, initialPdfData, agentRunning = false, onReference, onReferenceElement, onExit,
}, ref) {
  const t = useTranslations("presentation_edit");
  const tObject = useTranslations("canvas_object");
  const locale = useLocale();
  const { confirmDiscard, dialog } = useCanvasCloseGuard();

  const [base, setBase] = useState<Base | null>(null);
  const [inventoryError, setInventoryError] = useState<PresentationEditError | null>(null);
  const [inventoryLoading, setInventoryLoading] = useState(true);
  const [render, setRender] = useState<{ artifactId: string; data: ArrayBuffer } | null>(
    initialPdfData ? { artifactId, data: initialPdfData } : null,
  );
  const [renderState, setRenderState] = useState<RenderState>(initialPdfData ? "ready" : "loading");
  const [page, setPage] = useState(1);
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

  const { pdf } = usePdfDocument(render?.data ?? null);
  const inventory = base?.inventory;
  const slideCount = inventory?.slides.length ?? pdf?.numPages ?? 0;
  const slide = inventory?.slides[page - 1];
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

  useEffect(() => {
    if (!focusAfterSelectRef.current) return;
    focusAfterSelectRef.current = false;
    if (!phone) {
      inspectorRef.current?.querySelector<HTMLElement>("[data-autofocus]")?.focus();
      return;
    }
    // The phone editor is a sheet that mounts in a portal after this commit.
    const frame = requestAnimationFrame(() => phoneSheetRef.current?.querySelector<HTMLElement>("[data-autofocus]")?.focus());
    return () => cancelAnimationFrame(frame);
  }, [phone, selectedKey]);

  const select = useCallback((key: string) => {
    const found = findElement(inventory, key);
    if (!found) return;
    setPage(found.slide.index);
    focusAfterSelectRef.current = true;
    setSelectedKey(key);
  }, [inventory]);

  const clearSelection = useCallback(() => {
    const key = selectedKey;
    // Move focus before the panel unmounts, so it returns to the element instead of falling to the page.
    const target = key ? rootRef.current?.querySelector<HTMLElement>(`[data-element-key="${CSS.escape(key)}"]`) : null;
    if (target) target.focus();
    setSelectedKey(null);
    if (key && !target) {
      requestAnimationFrame(() => rootRef.current?.querySelector<HTMLElement>(`[data-list-key="${CSS.escape(key)}"]`)?.focus());
    }
  }, [selectedKey]);

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
      exitWith(close || after ? { artifactId, close, ...(after ? { then: after } : {}) } : null);
      return;
    }
    exitWith({
      artifactId: target,
      pdfData: render?.artifactId === target && renderState === "ready" ? render.data : undefined,
      close,
      edit: next?.edit,
      ...(after ? { then: after } : {}),
    });
  }, [artifactId, base?.artifactId, confirmDiscard, dirty, exitWith, filename, render, renderState, saved]);

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

  const save = useCallback(async () => {
    if (!base || !pending.length || saving || invalid || conflict) return;
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
      resetEditing();
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
    if (key) {
      const found = findElement(value, key);
      if (found) {
        setPage(found.slide.index);
        focusAfterSelectRef.current = true;
        setSelectedKey(key);
      }
    }
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

  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    const target = event.target as HTMLElement;
    const typing = target.matches("input, textarea, [contenteditable='true']");
    const mod = event.metaKey || event.ctrlKey;
    if (event.key === "Escape" && !event.defaultPrevented) {
      if (target.closest("[role='dialog']") !== rootRef.current?.closest("[role='dialog']")) return;
      event.preventDefault();
      event.stopPropagation();
      if (selectedKey) clearSelection();
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
    }
  };

  const editingMode = !saved;
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
          <Notice tone="info">
            {saved.result.new_file
              ? t("saved_summary_new_file", { count: saved.changes.length, filename: saved.result.filename })
              : t("saved_summary", { count: saved.changes.length, version: saved.result.version_no, previous })}
          </Notice>
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
      if (selected.kind === "text") {
        return <TextPanel element={selected} page={slide.index} pending={pending} refusals={refusals} fieldToken={fieldToken}
          onChange={(paragraph, text) => updateText(selected, slide.slide_id, paragraph, text)} onUndo={undo} onBack={back}
          onReference={referenceElement(selected, slide)} />;
      }
      if (selected.kind === "table" && phone && (selected.rows?.[0]?.length ?? 0) > PHONE_TABLE_COLUMNS) {
        return <UnsupportedPanel element={selected} page={slide.index} fieldToken={fieldToken} downloadHref={downloadHref} phone={phone}
          title={t("phone_wide_table_title")} body={t("phone_wide_table_body", { cols: selected.rows?.[0]?.length ?? 0 })}
          onReference={onReference ? () => { void requestExit(true, undefined, onReference); } : undefined}
          onReferenceElement={referenceElement(selected, slide)} onBack={back} />;
      }
      if (selected.kind === "table") {
        return <TablePanel element={selected} page={slide.index} pending={pending} refusals={refusals} fieldToken={fieldToken}
          onChange={(row, col, text) => updateCell(selected, slide.slide_id, row, col, text)} onUndo={undo} onBack={back}
          onReference={referenceElement(selected, slide)} />;
      }
      if (selected.kind === "chart") {
        return <ChartPanel element={selected} page={slide.index} slide={slide} pending={pending} drafts={chartDrafts[selected.key]}
          invalid={chartInvalid[selected.key] ?? []} locale={locale} fieldToken={fieldToken}
          onDraft={(series, category, text) => updateChart(selected, slide.slide_id, series, category, text)}
          onUndo={undo} onBack={back} onSelect={select} onReference={referenceElement(selected, slide)} />;
      }
    }
    return <ElementList slide={slide} pending={pending} fieldToken={fieldToken} onSelect={select}
      keyboardHint={t("keyboard_hint", { undo: undoKey })} />;
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
                return <span key={pendingEditId(row.edit)}>{described.location}：「{excerpt(described.text, fieldToken, 60)}」</span>;
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

  const overlayFor = (slideOf: InventorySlide | undefined) => slideOf ? slideOf.elements.map((element) => {
    if (!element.box) return null;
    const [left, top, width, height] = element.box;
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
    const label = `${t(`kind_${element.kind}`)}：${elementLabel(element, t, fieldToken)}${element.editable ? "" : ` · ${t("unsupported_badge")}`}${edits.length ? ` · ${t("unsaved_badge")}` : ""}`;
    const callout = edits.length ? (() => {
      const first = edits[0];
      if (first.kind === "chart") return t("callout_chart");
      if (first.kind === "cell") return t("callout_cells", { count: edits.length });
      return t("callout_text", { text: excerpt(elementText(element, edits), fieldToken, 18) });
    })() : null;
    return (
      <button
        key={element.key}
        type="button"
        className={styles.hit}
        style={style}
        data-element-key={element.key}
        data-editable={element.editable || undefined}
        data-pending={edits.length ? true : undefined}
        data-selected={selectedKey === element.key || undefined}
        aria-label={label}
        aria-pressed={selectedKey === element.key}
        onClick={() => select(element.key)}
      >
        {callout ? <span className={styles.callout} aria-hidden="true">{callout}</span> : null}
      </button>
    );
  }) : null;
  const stageOverlay = pageWidth > 0 ? overlayFor(slide) : null;

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
      {renderState === "loading" ? <StateBlock tone="progress" title={saved ? (saved.result.new_file ? t("updating_new_file") : t("updating_region", { version: saved.result.version_no })) : t("loading_page")} />
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
              <Button className={styles.phoneEditPage} onClick={() => { setPage(number); setSelectedKey(null); setSheetPage(number); }}>
                {t("phone_edit_page", { page: number, count: editable })}
              </Button>
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
            <ElementList slide={sheetSlide} pending={pending} fieldToken={fieldToken} onSelect={select} hideHeading />
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
          <strong title={filename}>{filename}</strong>
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
        <nav className={styles.rail} aria-label={t("pages")}>
          <span className={styles.railCount}>{t("page_count", { count: slideCount })}</span>
          <ol className={styles.railList}>
            {Array.from({ length: slideCount }, (_, index) => index + 1).map((number) => (
              <li key={number}>
                <button type="button" className={styles.railPage} aria-current={number === page ? "page" : undefined}
                  aria-label={`${t("page_label", { page: number })}${pendingPages.has(number) ? ` · ${t("unsaved_badge")}` : ""}`}
                  onClick={() => { setPage(number); setSelectedKey(null); }}>
                  <PdfPageCanvas pdf={pdf} pageNumber={number} width={104} className={styles.railCanvas} />
                  <span className={styles.railNumber}>{number}</span>
                  {pendingPages.has(number) ? <span className={styles.railDot} aria-hidden="true" /> : null}
                </button>
              </li>
            ))}
          </ol>
        </nav>
        <div className={styles.stageColumn}>
          <div className={styles.pageSwitcher}>
            <Button size="sm" variant="ghost" aria-label={t("previous_page")} disabled={page <= 1} onClick={() => { setPage(page - 1); setSelectedKey(null); }}>
              <ChevronLeft size={14} aria-hidden="true" />
            </Button>
            <span>{t("page_of", { page, count: slideCount })}</span>
            <Button size="sm" variant="ghost" aria-label={t("next_page")} disabled={page >= slideCount} onClick={() => { setPage(page + 1); setSelectedKey(null); }}>
              <ChevronRight size={14} aria-hidden="true" />
            </Button>
          </div>
          <div className={styles.stage} ref={stageRef}>
            {showPage ? (
              <div className={styles.page} style={{ width: pageWidth, height: Math.round(pageWidth / aspect) }} data-editing={editingMode || undefined}>
                <PdfPageCanvas pdf={pdf} pageNumber={page} width={pageWidth} label={`${filename} · ${t("page_label", { page })}`} className={styles.pageCanvas} />
                <div className={styles.overlay}>{stageOverlay}</div>
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
        <div className={styles.footer}>
          <SaveBar
            dirty={dirty}
            saving={saving}
            failed={Boolean(saveError && !conflict && saveError.code !== "edit_refused")}
            onSave={() => { void save(); }}
            onDiscard={() => { resetEditing(); setSelectedKey(null); }}
            saveDisabled={invalid || Boolean(conflict)}
            saveLabel={saveError && !conflict && saveError.code !== "edit_refused" ? tObject("retry_save") : saveLabel}
            discardLabel={conflict ? t("discard_mine") : t("discard")}
            message={saveMessage}
          />
        </div>
      ) : null}
      {phoneSheets}
      {dialog}
    </div>
  );
});
