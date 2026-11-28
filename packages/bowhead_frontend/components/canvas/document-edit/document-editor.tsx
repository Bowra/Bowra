"use client";

/**
 * In-place editing of a Word result (docx inline edit design §8; canvas Google 式排列 W1–W10) in the
 * editor shell shared with the deck editor. The pages are the version's render; paragraphs are found
 * on them by their text and edited where they are drawn. An edit that fits its paragraph's last line
 * is painted; one that adds a line stays pending with a card in the margin until the save re-renders
 * the pages. Saving writes the next version (an upload: a new file); a newer version refuses the
 * save and 修改记录 lists what can be re-applied. Behind FEATURE_DOCX_INLINE_EDIT.
 *
 * Phones (canvas WP1–WP5) list the pages with 修改第 N 页 and 引用这一页 under each; a tap on a
 * paragraph (44 px areas, the nearest paragraph wins) or a pick from the page's sheet opens the
 * paragraph's sheet, and the save bar at the bottom opens 修改记录.
 */

import { forwardRef, useCallback, useEffect, useImperativeHandle, useLayoutEffect, useMemo, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import { useLocale, useTranslations } from "next-intl";
import {
  Download as LucideDownload,
  Heading as LucideHeading,
  LoaderCircle as LucideLoaderCircle,
  Lock as LucideLock,
  MessageSquareText as LucideMessageSquareText,
  Pencil as LucidePencil,
  Pilcrow as LucidePilcrow,
  RotateCcw as LucideRotateCcw,
  Table2 as LucideTable2,
} from "lucide-react";

import { ErrorNotice } from "@/components/error-notice";
import { LucideGlyph } from "@/components/icon-refresh";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { Notice } from "@/components/ui/notice";
import { useNarrowViewport } from "@/components/ui/sheet";
import { useConfirmDialog } from "@/components/ui/confirm-dialog";
import { canvasCloseGuardOptions, ChangeRow, StateBlock } from "@/components/canvas/object";
import { ToolConfirmationNoticeSlot } from "@/components/chat/tool-confirmation/ToolConfirmationNotice";
import {
  EditorNoticeRow,
  EditorPanel,
  EditorRail,
  EditorShell,
  EditorToolbar,
  EditorTopBar,
  type ShellAction,
  type ShellPanelId,
  type ShellSelection,
  type ShellStatus,
  type ShellZoom,
} from "@/components/canvas/edit-shell/editor-shell";
import { PhoneSaveBar, PhoneTopBar, type PhoneSaveBarProps } from "@/components/canvas/edit-shell/phone-parts";
import { registerSubagentDraftNavigationGuard } from "@/lib/canvas-navigation-guard";
import { copyTextWithFallback } from "@/lib/clipboard";
import {
  changeNumbers,
  DocumentEditError,
  editProblem,
  fetchDocumentInventory,
  matchDocumentEdits,
  removeDocumentEdit,
  saveDocumentEdits,
  toDocumentWireEdits,
  upsertDocumentEdit,
  type DocumentInventory,
  type DocumentPendingEdit,
  type DocumentReapplyRow,
  type DocumentSaveResult,
} from "@/lib/document-edit";
import { canvasMeasure, paintDecision, paragraphPage } from "@/lib/document-layout";
import { formatDateTime, formatTime } from "@/lib/locale";
import type { LatestVersionFacts } from "@/lib/presentation-edit";
import { documentParagraphSelection } from "@/lib/document-selection";
import type { SlideToggleOutcome } from "@/lib/presentation-selection";
import type { AttachmentRef } from "@/lib/types";
import { toast } from "@/lib/use-toast";
import { excerpt } from "../presentation-edit/inspector";
import type { PresentationEditorExit } from "../presentation-edit/presentation-editor";
import { KeysPanelBody, PendingChangeList, type ChangeListRow } from "../presentation-edit/shell-parts";
import { ConflictBody, DocumentOutline, ItemsPanelBody, type ItemRow, type MissingRow, type OutlineHeading } from "./document-panels";
import { DocumentPages, type DocumentEditing, type PageHit } from "./document-pages";
import { useDocumentLayout, useDocumentPages } from "./use-document-layout";
import styles from "./document-editor.module.css";

export interface DocumentEditorHandle {
  requestExit: (close: boolean) => void;
}

interface Base {
  artifactId: string;
  versionNo: number;
  inventory: DocumentInventory;
}

interface SavedState {
  result: DocumentSaveResult;
  changes: DocumentPendingEdit[];
  previousVersion: number;
  inventory: DocumentInventory;
}

interface ConflictState {
  latest: LatestVersionFacts;
  rows: DocumentReapplyRow[] | null;
  inventory: DocumentInventory | null;
  failed: boolean;
}

const GUIDE_KEY = "bowhead.documentEdit.guideDismissed";
const POINTS_TO_PX = 96 / 72;
const GUTTER = 288;

function isMac(): boolean {
  return typeof navigator !== "undefined" && /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent);
}

export const DocumentEditor = forwardRef<DocumentEditorHandle, {
  sessionId: string;
  artifactId: string;
  filename: string;
  downloadHref: string;
  /** The version's pages, when the preview already holds them. */
  initialPdfData?: ArrayBuffer;
  initialPage?: number;
  agentRunning?: boolean;
  /** 「引用整份文件」 in chat, offered where the whole document refuses edits (canvas W8). */
  onReference?: () => void;
  /** 「引用这一段」: one paragraph or cell in the Composer; editing goes on (design §7.4). */
  onReferenceElement?: (reference: AttachmentRef, options?: { quiet?: boolean; stay?: boolean }) => void;
  /** 「引用这一页」: a rendered page toggled in the file's page set in the Composer. */
  onTogglePage?: (file: AttachmentRef, page: number) => SlideToggleOutcome;
  /** The pages of this file the Composer references. */
  referencedPages?: ReadonlyArray<number>;
  fromFullView?: boolean;
  onSaved?: (artifactId: string) => void;
  onExit: (exit: PresentationEditorExit | null) => void;
}>(function DocumentEditor({
  sessionId, artifactId, filename, downloadHref, initialPdfData, initialPage, agentRunning = false, onReference, fromFullView = false, onSaved, onExit,
  onReferenceElement, onTogglePage, referencedPages,
}, ref) {
  const t = useTranslations("document_edit");
  const tShell = useTranslations("edit_shell");
  const tObject = useTranslations("canvas_object");
  const locale = useLocale();
  const { confirm, dialog: confirmDialog } = useConfirmDialog();
  const measure = useMemo(() => canvasMeasure(), []);

  const [base, setBase] = useState<Base | null>(null);
  const [inventoryError, setInventoryError] = useState<DocumentEditError | null>(null);
  const [inventoryLoading, setInventoryLoading] = useState(true);
  const [pending, setPending] = useState<DocumentPendingEdit[]>([]);
  const touchedRef = useRef<string[]>([]);
  const [redoStack, setRedoStack] = useState<Array<{ edit: DocumentPendingEdit; index: number }>>([]);
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const [editing, setEditing] = useState<DocumentEditing>(null);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<DocumentEditError | null>(null);
  const [refused, setRefused] = useState<Record<string, string>>({});
  const [conflict, setConflict] = useState<ConflictState | null>(null);
  const [conflictChoices, setConflictChoices] = useState<Record<string, "mine" | "theirs">>({});
  const [reapplied, setReapplied] = useState<{ version: number; applied: number; manual: DocumentReapplyRow[] } | null>(null);
  const [saved, setSaved] = useState<SavedState | null>(null);
  const [panel, setPanel] = useState<ShellPanelId | null>(null);
  const [panelFocus, setPanelFocus] = useState(true);
  const [stripOpen, setStripOpen] = useState<boolean | null>(null);
  const [zoom, setZoom] = useState<ShellZoom>("fit");
  const [currentPage, setCurrentPage] = useState(Math.max(1, initialPage ?? 1));
  const [guideDismissed, setGuideDismissed] = useState(true);
  const [stageWidth, setStageWidth] = useState(0);
  const [announcement, setAnnouncement] = useState("");
  const phone = useNarrowViewport(true);
  // Phones: the page sheet (修改第 N 页) or the paragraphs not found on the pages.
  const [phoneSheet, setPhoneSheet] = useState<{ kind: "page"; page: number } | { kind: "missing" } | null>(null);
  const rootRef = useRef<HTMLDivElement | null>(null);
  const stageRef = useRef<HTMLDivElement | null>(null);
  const [stageElement, setStageElement] = useState<HTMLDivElement | null>(null);
  const aliveRef = useRef(true);
  const inventoryAbortRef = useRef<AbortController | null>(null);
  const focusSavedRef = useRef(false);
  const anchorRef = useRef<string | null>(null);

  const inventory = base?.inventory ?? null;
  const fieldToken = inventory?.field_token ?? "￼";
  const undoKey = isMac() ? "⌘Z" : "Ctrl+Z";
  const redoKey = isMac() ? "⇧⌘Z" : "Ctrl+Y";
  const pageKeys = isMac() ? "fn+↑ / fn+↓" : "PageUp / PageDown";

  useEffect(() => {
    aliveRef.current = true;
    try { setGuideDismissed(window.localStorage.getItem(GUIDE_KEY) === "1"); } catch { setGuideDismissed(false); }
    return () => {
      aliveRef.current = false;
      inventoryAbortRef.current?.abort();
    };
  }, []);

  const loadInventory = useCallback(async (id: string): Promise<DocumentInventory | null> => {
    inventoryAbortRef.current?.abort();
    const controller = new AbortController();
    inventoryAbortRef.current = controller;
    setInventoryLoading(true);
    setInventoryError(null);
    try {
      const value = await fetchDocumentInventory({ sessionId, artifactId: id, signal: controller.signal });
      if (!aliveRef.current || controller.signal.aborted) return null;
      setBase({ artifactId: value.artifact_id, versionNo: value.version_no, inventory: value });
      return value;
    } catch (error) {
      if (!aliveRef.current || controller.signal.aborted) return null;
      setInventoryError(error instanceof DocumentEditError ? error : new DocumentEditError("network"));
      return null;
    } finally {
      if (aliveRef.current && !controller.signal.aborted) setInventoryLoading(false);
    }
  }, [sessionId]);

  useEffect(() => {
    void loadInventory(artifactId);
    // The editor opens on one file; later bases are loaded by the actions that move to them.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Pages: the base version's, and after a save the new version's (canvas W10).
  const basePages = useDocumentPages(base?.artifactId ?? artifactId, base ? undefined : initialPdfData);
  const savedPages = useDocumentPages(saved ? saved.result.artifact_id : null);
  const layout = useDocumentLayout(basePages.pdf, inventory);
  // After a save the new pages are found by the saved text, so the view can stay on the edited paragraph.
  const savedView = useMemo(() => saved ? {
    paragraphs: saved.inventory.paragraphs.map((paragraph) => ({ ...paragraph, text: saved.changes.find((edit) => edit.key === paragraph.key)?.text ?? paragraph.text })),
    read_only_paragraphs: saved.inventory.read_only_paragraphs,
  } : null, [saved]);
  const savedLayout = useDocumentLayout(savedPages.pdf, savedView);
  const showingSaved = Boolean(saved && savedPages.state === "ready" && savedPages.pdf);
  const pdf = showingSaved ? savedPages.pdf : basePages.pdf;
  const shownLayout = showingSaved ? savedLayout : layout;

  // The stage's width decides the page scale: fit (never past 100 %), 100 % or 150 %.
  useLayoutEffect(() => {
    const stage = stageElement;
    if (!stage) return;
    const update = () => setStageWidth(stage.clientWidth);
    update();
    if (typeof ResizeObserver !== "function") return;
    const observer = new ResizeObserver(update);
    observer.observe(stage);
    return () => observer.disconnect();
  }, [stageElement]);

  const paragraphs = useMemo(() => inventory?.paragraphs ?? [], [inventory]);
  const byKey = useMemo(() => new Map(paragraphs.map((paragraph) => [paragraph.key, paragraph])), [paragraphs]);
  const readOnlyByKey = useMemo(() => new Map((inventory?.read_only_paragraphs ?? []).map((item) => [item.key, item])), [inventory]);
  const numbers = useMemo(() => changeNumbers(pending), [pending]);
  const pendingByKey = useMemo(() => new Map(pending.map((edit) => [edit.key, edit])), [pending]);
  const textOf = useCallback((key: string) => pendingByKey.get(key)?.text ?? byKey.get(key)?.text ?? "", [byKey, pendingByKey]);
  const decisions = useMemo(() => {
    const map = new Map<string, "paint" | "pending">();
    for (const edit of saved ? saved.changes : pending) {
      map.set(edit.key, paintDecision(edit, layout?.paragraphs.get(edit.key), measure, fieldToken));
    }
    return map;
  }, [fieldToken, layout, measure, pending, saved]);

  // Names: a heading by its text; a paragraph by its section and place; a cell by its row and column.
  const names = useMemo(() => {
    const map = new Map<string, { name: string; place: string; kind: string; icon: typeof LucidePilcrow; section: string | null }>();
    let section: string | null = null;
    let ordinal = 0;
    for (const paragraph of paragraphs) {
      if (paragraph.heading_level) {
        section = excerpt(paragraph.text, fieldToken, 24);
        ordinal = 0;
        map.set(paragraph.key, { name: section, place: section, kind: t("kind_heading", { level: paragraph.heading_level }), icon: LucideHeading, section });
        continue;
      }
      if (paragraph.table) {
        const place = t("cell_place", { row: paragraph.table.row + 1, col: paragraph.table.col + 1 });
        map.set(paragraph.key, { name: section ? `${section} · ${place}` : place, place, kind: t("kind_cell"), icon: LucideTable2, section });
        continue;
      }
      ordinal += 1;
      const place = t("paragraph_place", { number: ordinal });
      map.set(paragraph.key, { name: section ? `${section} · ${place}` : place, place, kind: t("kind_body"), icon: LucidePilcrow, section });
    }
    for (const item of inventory?.read_only_paragraphs ?? []) {
      const name = excerpt(item.text, fieldToken, 24);
      map.set(item.key, { name, place: name, kind: t(`kind_${item.part}`), icon: LucideLock, section: null });
    }
    return map;
  }, [fieldToken, inventory, paragraphs, t]);
  const nameOf = (key: string) => names.get(key)?.name ?? key;
  const reasonText = (reason: string | null | undefined) => (reason ? t(`reason_${reason}`) : "");
  const pageOf = (key: string) => (shownLayout ? paragraphPage(shownLayout, key) : null);

  const missingKeys = useMemo(() => new Set(layout?.missing ?? []), [layout]);
  const located = (key: string) => Boolean(layout?.paragraphs.get(key)?.lines.length);
  const editableHere = (key: string) => Boolean(byKey.get(key)?.editable) && !saved && !conflict && !inventory?.document.refusal;

  const touch = (key: string) => {
    touchedRef.current = [...touchedRef.current.filter((item) => item !== key), key];
    setRedoStack((stack) => (stack.length ? [] : stack));
  };
  const change = (key: string, text: string) => {
    const paragraph = byKey.get(key);
    if (!paragraph || !editableHere(key)) return;
    setPending((current) => upsertDocumentEdit(current, { key, expect: paragraph.text, text }));
    touch(key);
    setRefused((current) => {
      if (!(key in current)) return current;
      const next = { ...current };
      delete next[key];
      return next;
    });
  };
  const undoOne = (key: string) => setPending((current) => removeDocumentEdit(current, key));
  const announce = (said: string) => {
    setAnnouncement(said);
    window.setTimeout(() => setAnnouncement((current) => (current === said ? "" : current)), 4000);
  };
  const scrollToKey = useCallback((key: string) => {
    const hit = rootRef.current?.querySelector<HTMLElement>(`[data-hit-key="${CSS.escape(key)}"][data-first]`);
    hit?.scrollIntoView({ block: "center" });
    return hit ?? null;
  }, []);
  const undoLast = () => {
    const keys = new Set(pending.map((edit) => edit.key));
    const key = [...touchedRef.current].reverse().find((item) => keys.has(item)) ?? pending[pending.length - 1]?.key;
    if (!key) return;
    const index = pending.findIndex((edit) => edit.key === key);
    const edit = pending[index];
    undoOne(key);
    touchedRef.current = touchedRef.current.filter((item) => item !== key);
    setRedoStack((stack) => [...stack, { edit, index }]);
    scrollToKey(key);
    announce(t("undo_announce", { name: nameOf(key) }));
  };
  const redoLast = () => {
    const item = redoStack[redoStack.length - 1];
    if (!item) return;
    setRedoStack((stack) => stack.slice(0, -1));
    setPending((current) => {
      const next = current.filter((edit) => edit.key !== item.edit.key);
      next.splice(Math.min(item.index, next.length), 0, item.edit);
      return next;
    });
    touchedRef.current = [...touchedRef.current.filter((entry) => entry !== item.edit.key), item.edit.key];
    scrollToKey(item.edit.key);
    announce(t("redo_announce", { name: nameOf(item.edit.key) }));
  };

  const problems = useMemo(() => new Map(pending.flatMap((edit) => {
    const problem = editProblem(edit, fieldToken);
    return problem ? [[edit.key, problem] as const] : [];
  })), [fieldToken, pending]);
  const invalid = problems.size > 0;
  const dirty = pending.length > 0;
  const newFile = inventory?.edit_mode === "new_file";

  const resetEditing = useCallback(() => {
    setPending([]);
    setRefused({});
    setSaveError(null);
    setConflict(null);
    setReapplied(null);
    setEditing(null);
  }, []);

  const confirmLeave = useCallback(() => confirm({
    ...canvasCloseGuardOptions(tObject, filename),
    title: t("leave_guard_title"),
    description: t("leave_guard_body", { count: Math.max(1, pending.length), filename }),
    confirmLabel: t("leave_guard_confirm"),
  }), [confirm, filename, pending.length, t, tObject]);

  const requestExit = useCallback(async (close: boolean, next?: { artifactId: string; edit?: boolean }) => {
    if (dirty && !(await confirmLeave())) return;
    inventoryAbortRef.current?.abort();
    const current = saved ? saved.result.artifact_id : base?.artifactId ?? artifactId;
    const target = next?.artifactId ?? current;
    if (target === artifactId && !next?.edit) {
      onExit({ artifactId, close, page: currentPage });
      return;
    }
    const pagesData = target === saved?.result.artifact_id ? savedPages.data : target === base?.artifactId ? basePages.data : null;
    onExit({ artifactId: target, pdfData: pagesData ?? undefined, close, edit: next?.edit, ...(next ? {} : { page: currentPage }) });
  }, [artifactId, base?.artifactId, basePages.data, confirmLeave, currentPage, dirty, onExit, saved, savedPages.data]);
  useImperativeHandle(ref, () => ({ requestExit: (close: boolean) => { void requestExit(close); } }), [requestExit]);

  // Leaving any other way (a sidebar link, a waiting confirmation's View, a reload) asks first too.
  const dirtyRef = useRef(dirty);
  dirtyRef.current = dirty;
  const leaveRef = useRef({ confirmLeave, resetEditing });
  leaveRef.current = { confirmLeave, resetEditing };
  useEffect(() => registerSubagentDraftNavigationGuard({
    hasUnsaved: () => dirtyRef.current,
    confirmLeave: async () => {
      if (!dirtyRef.current) return true;
      if (!(await leaveRef.current.confirmLeave())) return false;
      dirtyRef.current = false;
      leaveRef.current.resetEditing();
      return true;
    },
  }), []);
  useEffect(() => {
    if (!dirty) return;
    const beforeUnload = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", beforeUnload);
    return () => window.removeEventListener("beforeunload", beforeUnload);
  }, [dirty]);

  const startConflict = useCallback(async (latest: LatestVersionFacts, edits: DocumentPendingEdit[]) => {
    setConflict({ latest, rows: null, inventory: null, failed: false });
    try {
      const value = await fetchDocumentInventory({ sessionId, artifactId: latest.artifact_id });
      if (!aliveRef.current) return;
      setConflict((current) => current && { ...current, rows: matchDocumentEdits(edits, value), inventory: value });
    } catch {
      if (aliveRef.current) setConflict((current) => current && { ...current, failed: true });
    }
  }, [sessionId]);

  const save = useCallback(async () => {
    if (!base || !pending.length || saving || invalid || conflict) return;
    setSaving(true);
    setSaveError(null);
    setRefused({});
    const edits = pending;
    try {
      const result = await saveDocumentEdits({ sessionId, artifactId: base.artifactId, edits: toDocumentWireEdits(edits) });
      if (!aliveRef.current) return;
      setSaved({ result, changes: edits, previousVersion: base.versionNo, inventory: base.inventory });
      anchorRef.current = edits[0]?.key ?? null;
      onSaved?.(result.artifact_id);
      setSelectedKey(null);
      // A phone returns to the pages, where the save bar says what was saved (canvas WP5).
      if (phone) setPanel(null);
      resetEditing();
      focusSavedRef.current = true;
    } catch (error) {
      if (!aliveRef.current) return;
      const failure = error instanceof DocumentEditError ? error : new DocumentEditError("network");
      setSaveError(failure);
      if (failure.code === "version_target_not_latest" && failure.latest) {
        void startConflict(failure.latest, edits);
        // The one case a panel opens by itself (canvas G5).
        setPanelFocus(false);
        setPanel("changes");
      }
      if (failure.code === "edit_refused") {
        const next: Record<string, string> = {};
        for (const item of failure.refused) {
          const key = item.key ?? (item.index !== null ? edits[item.index]?.key : undefined);
          if (key) next[key] = item.reason ?? item.code;
        }
        setRefused(next);
      }
      if (failure.code === "document_not_editable") void loadInventory(base.artifactId);
    } finally {
      if (aliveRef.current) setSaving(false);
    }
  }, [base, conflict, invalid, loadInventory, onSaved, pending, phone, resetEditing, saving, sessionId, startConflict]);

  // After a save: 「继续修改」 takes focus; once the new pages are in, the view stays on the edited paragraph.
  useEffect(() => {
    if (!saved || !focusSavedRef.current) return;
    const target = rootRef.current?.querySelector<HTMLElement>("[data-shell-action='continue']");
    if (!target) return;
    focusSavedRef.current = false;
    target.focus();
  });
  useEffect(() => {
    if (!showingSaved || !savedLayout || !anchorRef.current) return;
    const key = anchorRef.current;
    anchorRef.current = null;
    requestAnimationFrame(() => scrollToKey(key));
  }, [savedLayout, scrollToKey, showingSaved]);

  const reapply = () => {
    if (!conflict?.inventory || !conflict.rows) return;
    const choice = (row: DocumentReapplyRow) => conflictChoices[row.edit.key];
    const applicable = conflict.rows.flatMap((row) => row.applicable ? [row.edit] : choice(row) === "mine" && row.rebased ? [row.rebased] : []);
    const manual = conflict.rows.filter((row) => !row.applicable && !(choice(row) === "mine" && row.rebased) && choice(row) !== "theirs");
    const latest = conflict.inventory;
    setConflictChoices({});
    setBase({ artifactId: latest.artifact_id, versionNo: latest.version_no, inventory: latest });
    setPending(applicable);
    setRefused({});
    setSaveError(null);
    setConflict(null);
    setSelectedKey(null);
    setEditing(null);
    setReapplied({ version: latest.version_no, applied: applicable.length, manual });
  };

  const continueEditing = async () => {
    if (!saved) return;
    const value = await loadInventory(saved.result.artifact_id);
    if (!value || !aliveRef.current) return;
    setSaved(null);
    requestAnimationFrame(() => stageRef.current?.focus());
  };

  const discardAll = async () => {
    if (pending.length > 1) {
      const ok = await confirm({ ...canvasCloseGuardOptions(tObject, filename), description: t("discard_confirm_body", { count: pending.length }) });
      if (!ok) return;
    }
    const kept = pending.length === 1 ? { pending, base: base?.artifactId } : null;
    resetEditing();
    setSelectedKey(null);
    requestAnimationFrame(() => stageRef.current?.focus());
    if (kept) {
      toast({
        kind: "info",
        text: t("discarded_one"),
        action: {
          label: t("undo_discard"),
          onAction: () => {
            const now = restoreRef.current;
            if (now.saved || now.pending.length || now.base !== kept.base) return;
            setPending(kept.pending);
            touchedRef.current = kept.pending.map((edit) => edit.key);
          },
        },
      });
    }
  };
  const restoreRef = useRef({ saved: false, pending: [] as DocumentPendingEdit[], base: undefined as string | undefined });
  restoreRef.current = { saved: Boolean(saved), pending, base: base?.artifactId };

  const copyMyText = async (edits: ReadonlyArray<DocumentPendingEdit>) => {
    const text = edits.map((edit) => `${nameOf(edit.key)}\n${edit.text.split(fieldToken).join("#")}`).join("\n\n");
    const ok = await copyTextWithFallback(text, t("copy_prompt"));
    if (ok) toast({ kind: "info", text: t("copied") });
  };

  // ── Selection ────────────────────────────────────────────────────────────────────
  const select = (key: string) => {
    setSelectedKey(key);
    // Phones edit in the paragraph's sheet (canvas WP2), wherever the paragraph is.
    if (phone) {
      setEditing(null);
      return;
    }
    if (editableHere(key) && located(key)) {
      setEditing({ key, armed: true });
      return;
    }
    setEditing(null);
    // Not found on the pages: 本页内容 holds its field (canvas W6).
    if (editableHere(key) && missingKeys.has(key)) {
      setPanelFocus(false);
      setPanel("items");
      requestAnimationFrame(() => rootRef.current?.querySelector<HTMLTextAreaElement>(`[data-missing-key="${CSS.escape(key)}"] textarea`)?.focus());
    }
  };
  const goTo = (key: string) => {
    // A phone shows one sheet at a time: 修改记录 gives way to the paragraph's sheet.
    if (phone) setPanel(null);
    select(key);
    requestAnimationFrame(() => scrollToKey(key));
  };
  const clearSelection = () => {
    const key = selectedKey;
    setEditing(null);
    setSelectedKey(null);
    if (key) rootRef.current?.querySelector<HTMLElement>(`[data-hit-key="${CSS.escape(key)}"][data-first]`)?.focus();
  };
  const goPage = (next: number) => {
    const count = pdf?.numPages ?? 1;
    const target = Math.min(count, Math.max(1, next));
    rootRef.current?.querySelector<HTMLElement>(`[data-page="${target}"]`)?.scrollIntoView({ block: "start" });
    setCurrentPage(target);
  };

  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.nativeEvent.isComposing || event.keyCode === 229) return;
    const target = event.target as HTMLElement;
    const typing = target.matches("input, textarea, [contenteditable='true']") && !target.dataset.armed;
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
      return;
    }
    if (mod && !event.altKey && ((event.shiftKey && event.key.toLowerCase() === "z") || (event.ctrlKey && !event.metaKey && event.key.toLowerCase() === "y")) && !typing && !saved) {
      event.preventDefault();
      redoLast();
      return;
    }
    if ((event.key === "PageUp" || event.key === "PageDown") && !typing) {
      event.preventDefault();
      goPage(currentPage + (event.key === "PageDown" ? 1 : -1));
    }
  };

  // ── Geometry ─────────────────────────────────────────────────────────────────────
  const pageCount = pdf?.numPages ?? shownLayout?.pages.length ?? 0;
  const fallbackSize = inventory ? { width: inventory.page.width / 20, height: inventory.page.height / 20 } : { width: 595, height: 842 };
  const pageSizes = shownLayout?.pages.length ? shownLayout.pages : Array.from({ length: Math.max(1, pageCount) }, () => fallbackSize);
  const pageWidthPts = pageSizes[0]?.width ?? fallbackSize.width;
  const available = Math.max(0, stageWidth - 48);
  const pendingCards = pending.some((edit) => decisions.get(edit.key) === "pending" && located(edit.key));
  const zoomFactor = zoom === "150" ? 2 : POINTS_TO_PX;
  const gutter = !phone && pendingCards && available - GUTTER >= pageWidthPts * zoomFactor * (zoom === "fit" ? 0.85 : 1);
  // Phones: the pages fill the width less 16 px each side (canvas WP1).
  const scale = phone ? Math.max(0.2, (stageWidth - 32) / Math.max(1, pageWidthPts)) : zoom === "fit"
    ? Math.min(POINTS_TO_PX, Math.max(0.3, (available - (gutter ? GUTTER : 0)) / Math.max(1, pageWidthPts)))
    : zoomFactor;

  // ── Shell parts ──────────────────────────────────────────────────────────────────
  const refusal = inventory?.document.refusal ?? null;
  const saveLabel = newFile ? t("save_new_file") : t("save_version");
  const editingLabel = newFile ? t("editing_new_file") : t("editing_version", { version: base?.versionNo ?? 1 });
  const dropsText = inventory?.drops_on_save?.length ? t("save_drops_versions", { versions: inventory.drops_on_save.join(t("version_list_separator")) }) : undefined;
  const latestTime = (latest: LatestVersionFacts) => {
    const date = new Date(latest.created_at);
    if (Number.isNaN(date.getTime())) return "";
    return date.toDateString() === new Date().toDateString() ? formatTime(date, locale) : formatDateTime(date, locale, { dateStyle: "short", timeStyle: "short" });
  };
  const conflictMessage = (latest: LatestVersionFacts) => {
    const values = { base: base?.versionNo ?? 1, version: latest.version_no, time: latestTime(latest), request: latest.request_excerpt ?? "" };
    if (latest.produced_by === "user") return t("conflict_user", values);
    return latest.request_excerpt ? t("conflict_agent", values) : t("conflict_agent_no_request", values);
  };
  const saveErrorMessage = (error: DocumentEditError): string => {
    if (error.code === "version_target_not_latest" && error.latest) return conflictMessage(error.latest);
    if (error.code === "results_scope_unavailable" || error.code === "document_not_editable" || error.code === "document_edit_disabled") return t("error_permission");
    if (error.code === "edit_refused") return t("error_refused", { count: Math.max(1, error.refused.length) });
    if (error.code === "no_changes") return t("error_no_changes");
    if (error.code === "document_unreadable") return t("error_unreadable");
    if (error.code === "too_large") return t("error_too_large");
    if (error.code === "parse_unavailable" || error.code === "file_unavailable" || error.status === 503) return t("error_unavailable");
    return t("save_failed");
  };
  const conflictApplicable = conflict?.rows?.filter((row) => row.applicable || (row.rebased && conflictChoices[row.edit.key] === "mine")).length ?? 0;
  const status: ShellStatus = saved
    ? { tone: savedPages.state === "failed" ? "danger" : "positive", icon: savedPages.state === "failed" ? "error" : "saved",
      text: saved.result.new_file ? t("saved_new_file", { filename: saved.result.filename }) : t("saved_version", { version: saved.result.version_no }) }
    : !base ? { tone: inventoryError ? "danger" : "muted", icon: inventoryError ? "error" : undefined, text: inventoryError ? t("inventory_failed_title") : t("loading_inventory") }
      : refusal ? { tone: "muted", icon: undefined, text: t("blocked_status") }
        : conflict ? { tone: "danger", icon: "error", text: t("conflict_bar", { version: conflict.latest.version_no }) }
          : saving ? { tone: "muted", icon: "saving", text: t("saving") }
            : saveError ? { tone: "danger", icon: "error", text: saveErrorMessage(saveError) }
              : invalid ? { tone: "warning", icon: "dirty", text: t("invalid_pending") }
                : dirty ? { tone: "warning", icon: "dirty", text: `${editingLabel} · ${t("pending_count", { count: pending.length })}`, extra: dropsText }
                  : { tone: "muted", text: `${editingLabel} · ${t("no_changes_yet")}` };

  const topBar = (
    <EditorTopBar kind="document" filename={filename} status={status}
      discard={!saved && base && (dirty || conflict) ? { label: conflict ? t("discard_mine") : t("discard"), onClick: () => { void discardAll(); }, disabled: saving } : undefined}
      discardDanger={Boolean(conflict)}
      primary={saved || !base || refusal ? undefined : conflict ? (conflictApplicable > 0 ? {
        label: t("reapply", { version: conflict.latest.version_no, count: conflictApplicable }),
        onClick: reapply,
        disabled: !conflict.rows,
        title: t("reapply_note", { version: conflict.latest.version_no, next: conflict.latest.version_no + 1 }),
      } : undefined) : {
        label: saveError && saveError.code !== "edit_refused" && saveError.code !== "no_changes" ? tObject("retry_save") : saveLabel,
        onClick: () => { void save(); },
        disabled: !dirty || invalid || saving,
        busy: saving,
      }}
      continueEditing={saved ? { label: t("edit_again"), onClick: () => { void continueEditing(); }, busy: inventoryLoading } : undefined}
      onExit={() => { void requestExit(false); }}
      onCloseFullView={fromFullView ? () => { void requestExit(true); } : undefined} />
  );

  const selected = selectedKey ? byKey.get(selectedKey) ?? null : null;
  const selectedReadOnly = selectedKey ? readOnlyByKey.get(selectedKey) ?? null : null;
  const selectedNumber = selectedKey ? numbers.get(selectedKey) : undefined;
  const selectedPending = selectedKey ? decisions.get(selectedKey) === "pending" && pendingByKey.has(selectedKey) : false;
  const selection: ShellSelection | null = selectedKey && (selected || selectedReadOnly) ? {
    icon: names.get(selectedKey)?.icon ?? LucidePilcrow,
    kind: names.get(selectedKey)?.kind ?? "",
    name: nameOf(selectedKey),
    where: selected && !selected.editable ? reasonText(selected.reason)
      : selectedReadOnly ? reasonText(selectedReadOnly.reason)
        : pageOf(selectedKey) ? t("page_label", { page: pageOf(selectedKey) ?? 1 }) : undefined,
    badge: (selected && !selected.editable) || selectedReadOnly ? { tone: "outline", text: t("readonly_badge") }
      : selectedNumber ? { tone: "warning", text: selectedPending ? t("pending_badge_line", { number: selectedNumber }) : t("pending_badge", { number: selectedNumber }) }
        : undefined,
  } : null;
  // References (design §7.4): the file as the version being edited, with a paragraph or the page in view.
  const fileRef: AttachmentRef | null = inventory?.file && base
    ? { fileId: base.artifactId, filename: inventory.file.filename, mime: inventory.file.mime, size: inventory.file.size, referenceKind: "result" }
    : null;
  const pageReferenced = Boolean(referencedPages?.includes(currentPage)) && base?.artifactId === artifactId;
  const referenceParagraph = (key: string) => {
    if (!fileRef || !onReferenceElement || !byKey.has(key)) return undefined;
    const selection = documentParagraphSelection(fileRef.fileId, key, nameOf(key));
    if (!selection) return undefined;
    return () => onReferenceElement({ ...fileRef, documentSelection: selection }, { stay: true });
  };
  const pageAction: ShellAction[] = fileRef && onTogglePage && !refusal && pageCount > 0 ? [{
    key: "page",
    label: t(pageReferenced ? "referenced_page" : "reference_page"),
    ariaLabel: t(pageReferenced ? "referenced_page_label" : "reference_page_label", { page: currentPage }),
    icon: LucideMessageSquareText,
    pressed: pageReferenced,
    onClick: () => { onTogglePage(fileRef, currentPage); },
  }] : [];
  const selectedReference = selectedKey && byKey.has(selectedKey) ? referenceParagraph(selectedKey) : undefined;
  const actions: ShellAction[] = saved || conflict || !base ? [] : !selectedKey ? pageAction : [
    ...(selectedReference ? [{
      key: "reference",
      label: t(byKey.get(selectedKey)?.table ? "reference_cell" : "reference_paragraph"),
      icon: LucideMessageSquareText,
      onClick: selectedReference,
    }] : []),
    ...(pendingByKey.has(selectedKey) ? [{ key: "undo", label: t("undo_here"), icon: LucideRotateCcw, onClick: () => undoOne(selectedKey) }] : []),
    ...((selected && !selected.editable) || selectedReadOnly ? [{ key: "download", label: t("download_file"), icon: LucideDownload, onClick: () => { window.open(downloadHref, "_blank", "noopener"); } }] : []),
  ];
  const hint = !selectedKey ? t("hint_none")
    : (selected && !selected.editable) || selectedReadOnly ? t("hint_readonly")
      : editing?.key === selectedKey && !editing.armed ? t("hint_editing")
        : selectedPending ? t("hint_pending")
          : t("hint_selected");

  const notice = saved || !base || inventoryError || refusal ? null
    : saveError && !conflict && saveError.code !== "edit_refused" ? (
      <div className={styles.stageState} style={{ margin: 0, width: "auto", padding: "8px 16px" }}>
        <ErrorNotice message={saveErrorMessage(saveError)} surface="task" diagnostics={saveError.diagnostics}
          action={saveError.code === "results_scope_unavailable" || saveError.code === "document_not_editable" ? { kind: "retry", label: t("copy_text") } : undefined}
          onAction={() => { void copyMyText(pending); }} />
      </div>
    ) : reapplied ? (
      <EditorNoticeRow action={reapplied.manual.length
        ? <Button size="sm" variant="plain" onClick={() => { void copyMyText(reapplied.manual.map((row) => row.edit)); }}>{t("copy_text")}</Button>
        : undefined}>
        {t("reapplied", { version: reapplied.version, count: reapplied.applied })}
        {reapplied.manual.length ? ` ${t("reapplied_manual", { count: reapplied.manual.length })}` : ""}
      </EditorNoticeRow>
    ) : newFile && inventory?.edited_copies[0] ? (
      <EditorNoticeRow action={(
        <Button size="sm" onClick={() => { void requestExit(false, { artifactId: inventory.edited_copies[0].artifact_id, edit: true }); }}>{t("open_edited_copy")}</Button>
      )}>
        {t(inventory.edited_copies[0].made_by === "ai" ? "edited_copy_exists_ai" : "edited_copy_exists", { filename: inventory.edited_copies[0].filename })}
      </EditorNoticeRow>
    ) : newFile ? <EditorNoticeRow>{t("guide_upload", { filename: inventory?.new_filename ?? filename })}</EditorNoticeRow>
      : agentRunning ? <EditorNoticeRow>{t("agent_running")}</EditorNoticeRow>
        : layout && layout.missing.length ? (
          <EditorNoticeRow action={<Button size="sm" variant="plain" onClick={() => { setPanelFocus(true); setPanel("items"); }}>{t("missing_open")}</Button>}>
            {t("missing_notice", { count: layout.missing.length })}
          </EditorNoticeRow>
        ) : !guideDismissed ? (
          <EditorNoticeRow dismissLabel={t("guide_dismiss")} onDismiss={() => {
            setGuideDismissed(true);
            try { window.localStorage.setItem(GUIDE_KEY, "1"); } catch { /* Private mode: shown again next time. */ }
          }}>
            {t("guide_text", { save: saveLabel })}
          </EditorNoticeRow>
        ) : null;

  // The outline: headings with their level and page; the current section is the last heading above the page in view.
  const headings: OutlineHeading[] = useMemo(() => {
    const list: OutlineHeading[] = [];
    let index = -1;
    for (const paragraph of paragraphs) {
      if (paragraph.heading_level) {
        list.push({ key: paragraph.key, text: excerpt(paragraph.text, fieldToken, 48), level: paragraph.heading_level, page: shownLayout ? paragraphPage(shownLayout, paragraph.key) : null, dirty: false });
        index = list.length - 1;
      } else if (index >= 0 && pendingByKey.has(paragraph.key)) {
        list[index].dirty = true;
      }
    }
    return list;
  }, [fieldToken, paragraphs, pendingByKey, shownLayout]);
  const currentHeading = [...headings].reverse().find((heading) => heading.page !== null && heading.page <= currentPage)?.key ?? headings[0]?.key ?? null;
  const strip = base ? (
    <DocumentOutline headings={headings} currentKey={currentHeading} pageCount={pageCount}
      onSelect={(key) => goTo(key)} onHide={() => setStripOpen(false)} />
  ) : null;

  const hits: PageHit[] = useMemo(() => [
    ...paragraphs.map((paragraph) => ({
      key: paragraph.key,
      editable: paragraph.editable && !refusal,
      number: numbers.get(paragraph.key),
      label: [
        names.get(paragraph.key)?.kind,
        excerpt(pendingByKey.get(paragraph.key)?.text ?? paragraph.text, fieldToken, 60),
        paragraph.editable ? null : t("readonly_badge"),
        numbers.get(paragraph.key) ? t("unsaved_numbered", { number: numbers.get(paragraph.key) ?? 0 }) : null,
      ].filter(Boolean).join(t("label_list_separator")),
    })),
    ...(inventory?.read_only_paragraphs ?? []).map((item) => ({
      key: item.key,
      editable: false,
      label: [t(`kind_${item.part}`), excerpt(item.text, fieldToken, 60), t("readonly_badge")].join(t("label_list_separator")),
    })),
  ], [fieldToken, inventory, names, numbers, paragraphs, pendingByKey, refusal, t]);

  // Phones (canvas WP1, WP5): under each page, 修改第 N 页 and 引用这一页, then what changed on it.
  const savedNumbers = saved ? changeNumbers(saved.changes) : null;
  const referencedOnPhone = (page: number) => Boolean(referencedPages?.includes(page)) && base?.artifactId === artifactId;
  const phonePageAfter = (page: number) => {
    if (!base) return null;
    const editableCount = refusal ? 0 : paragraphs.filter((paragraph) => paragraph.editable && pageOf(paragraph.key) === page).length;
    const changed = (saved ? saved.changes : pending).filter((edit) => pageOf(edit.key) === page);
    const referenced = referencedOnPhone(page);
    return (
      <>
        {!saved && !conflict && !refusal ? (
          <div className={styles.phoneActions}>
            <Button className={styles.phoneEditPage} aria-haspopup="dialog"
              onClick={() => { setCurrentPage(page); setSelectedKey(null); setPhoneSheet({ kind: "page", page }); }}>
              <LucideGlyph icon={LucidePencil} size={18} aria-hidden="true" />
              {t("phone_edit_page", { page })}
              <span className={styles.phoneEditCount}>{t("phone_editable_count", { count: editableCount })}</span>
            </Button>
            {fileRef && onTogglePage ? (
              <Button variant="secondary" className={styles.phoneReference} aria-pressed={referenced}
                aria-label={t(referenced ? "referenced_page_label" : "reference_page_label", { page })}
                onClick={() => { onTogglePage(fileRef, page); }}>
                <LucideGlyph icon={LucideMessageSquareText} size={18} aria-hidden="true" />
                {t(referenced ? "referenced_page" : "reference_page")}
              </Button>
            ) : null}
          </div>
        ) : null}
        {changed.length ? (
          <ul className={styles.phoneChanges} data-saved={saved ? true : undefined} aria-label={t("phone_page_changes", { page })}>
            {changed.map((edit) => {
              const number = (savedNumbers ?? numbers).get(edit.key);
              const place = names.get(edit.key)?.place ?? nameOf(edit.key);
              const lineAdded = decisions.get(edit.key) === "pending";
              return (
                <li key={edit.key}>
                  {number ? <span className={styles.phoneChangeNumber} aria-hidden="true">{number}</span> : null}
                  <span>
                    {saved ? t("phone_saved_line", { place }) : t("phone_change_line", { place, text: excerpt(edit.text, fieldToken, 80) })}
                    {lineAdded ? <span className={styles.phoneChangeNote}>{t(saved ? "phone_saved_note" : "phone_pending_line")}</span> : null}
                  </span>
                </li>
              );
            })}
          </ul>
        ) : null}
      </>
    );
  };

  const pagesNode = (interactive: boolean, before?: ReactNode) => (
    <DocumentPages pdf={pdf} pageCount={pageCount} pageSizes={pageSizes} scale={scale} filename={filename}
      layout={showingSaved ? savedLayout : layout} readOnlyLines={(showingSaved ? savedLayout : layout)?.readOnly ?? new Map()}
      hits={showingSaved ? [] : hits} pending={showingSaved ? [] : saved ? saved.changes : pending} decisions={decisions}
      selectedKey={selectedKey} editing={interactive ? editing : null} gutter={gutter} fieldToken={fieldToken} interactive={interactive && !(phone && conflict)}
      pageLabel={(page) => t("page_label", { page })}
      fieldLabel={(key) => [names.get(key)?.kind, nameOf(key)].filter(Boolean).join(t("label_list_separator"))}
      armedHint={t("armed_hint")}
      gutterCopy={{ badge: (number) => t("pending_badge", { number }), changeTo: t("change_to"), note: t("pending_note") }}
      textOf={textOf}
      onSelect={(key) => select(key)}
      onStart={() => setEditing((current) => current && { ...current, armed: false })}
      onChange={change}
      onClose={() => setEditing((current) => current && { key: current.key, armed: true })}
      onDisarm={() => setEditing((current) => (current?.armed ? null : current))}
      onTabAway={() => setEditing(null)}
      onVisiblePage={setCurrentPage}
      touch={phone}
      pageAfter={phone ? phonePageAfter : undefined}
      before={before} />
  );

  const pagesReady = Boolean(pdf) && (basePages.state === "ready" || showingSaved);
  const stage = (
    <>
      {refusal ? (
        <div className={styles.blocked}>
          <div aria-hidden="true">{pagesReady ? pagesNode(false) : null}</div>
          <section className={styles.blockedCard} role="alertdialog" aria-labelledby="document-blocked-title" aria-describedby="document-blocked-body">
            <LucideGlyph icon={LucideLock} size={20} aria-hidden="true" />
            <h2 id="document-blocked-title" className={styles.blockedTitle} tabIndex={-1}>{t(`blocked_title_${refusal}`)}</h2>
            <p id="document-blocked-body" className={styles.blockedBody}>{t(`blocked_body_${refusal}`)}</p>
            <div className={styles.blockedActions}>
              {/* Change tracking: editing in Word keeps the tracked changes, so download comes first (decision 2026-11-27). */}
              <Button variant="primary" onClick={() => { window.open(downloadHref, "_blank", "noopener"); }}>
                <LucideGlyph icon={LucideDownload} size={14} aria-hidden="true" />{t("download_file")}
              </Button>
              {onReference ? (
                <Button onClick={() => { onExit({ artifactId, close: true, then: onReference }); }}>
                  <LucideGlyph icon={LucideMessageSquareText} size={14} aria-hidden="true" />{t("reference_file")}
                </Button>
              ) : null}
              <Button variant="plain" onClick={() => { void requestExit(false); }}>{tShell("exit")}</Button>
            </div>
          </section>
        </div>
      ) : (
        <div className={styles.stage} ref={(element) => { stageRef.current = element; setStageElement(element); }} tabIndex={-1} role="group"
          aria-label={t("stage_label", { page: currentPage, count: pageCount })}>
          {inventoryError && !base ? (
            <div className={styles.stageState}>
              <StateBlock tone="error" title={t("inventory_failed_title")}
                body={inventoryError.code === "results_scope_unavailable" || inventoryError.code === "document_not_editable" ? t("inventory_not_editable")
                  : inventoryError.code === "document_edit_disabled" ? t("inventory_disabled") : t("inventory_failed_body")}
                primaryAction={{ label: t("retry"), onClick: () => { void loadInventory(artifactId); } }}
                secondaryActions={[{ label: t("download_file"), onClick: () => { window.open(downloadHref, "_blank", "noopener"); } }]} />
            </div>
          ) : basePages.state === "failed" || basePages.state === "busy" ? (
            <div className={styles.stageState}>
              <StateBlock tone="error" title={t(basePages.state === "busy" ? "pages_busy_title" : "pages_failed_title")} body={t("pages_failed_body")}
                primaryAction={{ label: t("retry"), onClick: basePages.reload }}
                secondaryActions={[{ label: t("download_file"), onClick: () => { window.open(downloadHref, "_blank", "noopener"); } }]} />
            </div>
          ) : !pagesReady || !base ? (
            <div className={styles.stageState}><StateBlock tone="progress" title={t("loading_pages")} /></div>
          ) : pagesNode(!saved, saved && !showingSaved ? (
            <span className={styles.statusPill} role="status">
              <LucideGlyph icon={LucideLoaderCircle} size={14} aria-hidden="true" />
              {savedPages.state === "failed" ? t("saved_pages_failed", { version: saved.result.version_no }) : t("saved_pages_pending", { version: saved.result.version_no })}
            </span>
          ) : undefined)}
        </div>
      )}
      <ToolConfirmationNoticeSlot active />
    </>
  );

  // 修改记录 (canvas W3), 本页内容 (W6, W7), 快捷键.
  const changeRows: ChangeListRow[] = pending.map((edit) => {
    const problem = problems.get(edit.key);
    const refusal = refused[edit.key];
    return {
      id: edit.key,
      number: numbers.get(edit.key) ?? 0,
      page: pageOf(edit.key) ?? 1,
      name: nameOf(edit.key),
      where: names.get(edit.key)?.kind ?? "",
      before: excerpt(edit.expect, fieldToken, 160),
      after: excerpt(edit.text, fieldToken, 160),
      error: problem ? t(`problem_${problem}`) : refusal ? (refusal in REASONS ? t("refused_readonly", { reason: reasonText(refusal) }) : t(`refused_${refusal === "text_changed" ? "text_changed" : "other"}`)) : null,
      note: decisions.get(edit.key) === "pending" ? t("pending_note") : null,
      onOpen: () => goTo(edit.key),
      onUndo: () => undoOne(edit.key),
    };
  });
  const closePanel = (id: ShellPanelId) => {
    setPanel(null);
    requestAnimationFrame(() => rootRef.current?.querySelector<HTMLElement>(`[data-rail-panel="${id}"]`)?.focus());
  };
  const itemsOn = (page: number): ItemRow[] => [
    ...paragraphs.filter((paragraph) => pageOf(paragraph.key) === page).map((paragraph): ItemRow => ({
      key: paragraph.key,
      name: nameOf(paragraph.key),
      kind: names.get(paragraph.key)?.kind ?? "",
      editable: paragraph.editable && !refusal,
      number: numbers.get(paragraph.key),
      reason: paragraph.editable ? undefined : reasonText(paragraph.reason),
      current: selectedKey === paragraph.key,
    })),
    ...(inventory?.read_only_paragraphs ?? []).filter((item) => (layout?.readOnly.get(item.key) ?? []).some((line) => line.page === page - 1)).map((item): ItemRow => ({
      key: item.key,
      name: nameOf(item.key),
      kind: t(`kind_${item.part}`),
      editable: false,
      reason: reasonText(item.reason),
      current: selectedKey === item.key,
    })),
  ];
  const pageItems = itemsOn(currentPage);
  const missingRows: MissingRow[] = (layout?.missing ?? []).filter((key) => editableHere(key)).map((key) => ({
    key,
    name: nameOf(key),
    text: textOf(key),
    number: numbers.get(key),
    error: problems.get(key) ? t(`problem_${problems.get(key)}`) : null,
    onChange: (text) => change(key, text),
  }));
  const changesSubtitle = saved ? t("saved_version", { version: saved.result.version_no }) : conflict ? t("changes_conflict", { count: pending.length }) : pending.length ? t("changes_dirty", { count: pending.length }) : undefined;
  const changesFooter = saved ? t("changes_footer_saved", { version: saved.result.version_no })
    : conflict ? t("changes_footer_conflict", { next: conflict.latest.version_no + 1 })
      : !base ? undefined
        : newFile ? t("changes_footer_new_file", { filename: inventory?.new_filename ?? filename })
          : `${t("changes_footer_dirty", { next: base.versionNo + 1, base: base.versionNo })}${dropsText ? `${t("message_separator")}${dropsText}` : ""}`;
  const changesBody = (
    <>
      {saved ? (
        <>
          <Notice tone="info">
            {saved.result.new_file
              ? t("saved_summary_new_file", { count: saved.changes.length, filename: saved.result.filename })
              : t("saved_summary", { count: saved.changes.length, version: saved.result.version_no, previous: saved.previousVersion })}
          </Notice>
          {savedPages.state === "failed" || savedPages.state === "busy" ? (
            <ErrorNotice message={t("saved_pages_failed", { version: saved.result.version_no })} surface="task"
              action={{ kind: "retry", label: t("retry") }} onAction={savedPages.reload} />
          ) : null}
          {saved.changes.map((edit) => (
            <ChangeRow key={edit.key} location={nameOf(edit.key)}
              before={{ kind: "text", text: excerpt(edit.expect, fieldToken, 160) }} after={{ kind: "text", text: excerpt(edit.text, fieldToken, 160) }} />
          ))}
        </>
      ) : conflict ? (
        <ConflictBody message={conflictMessage(conflict.latest)} version={conflict.latest.version_no}
          checking={conflict.rows === null && !conflict.failed} failed={conflict.failed} applicable={conflictApplicable}
          rows={(conflict.rows ?? pending.map((edit): DocumentReapplyRow => ({ edit, applicable: false, latestText: null, rebased: null }))).map((row) => ({
            id: row.edit.key,
            location: nameOf(row.edit.key),
            before: excerpt(row.edit.expect, fieldToken, 120),
            after: excerpt(row.edit.text, fieldToken, 120),
            applicable: row.applicable,
            theirs: row.latestText !== null && !row.applicable ? excerpt(row.latestText, fieldToken, 120) : null,
            canChoose: Boolean(row.rebased),
            choice: conflictChoices[row.edit.key] ?? null,
          }))}
          onChoose={(id, value) => setConflictChoices((current) => ({ ...current, [id]: value }))}
          onCopy={() => { void copyMyText(pending); }}
          onOpenLatest={() => { void requestExit(false, { artifactId: conflict.latest.artifact_id }); }} />
      ) : <PendingChangeList rows={changeRows} />}
    </>
  );
  const panelNode = panel === "changes" ? (
    <EditorPanel id="changes" title={tShell("panel_changes")} focusOnOpen={panelFocus} onClose={() => closePanel("changes")}
      subtitle={changesSubtitle} footer={changesFooter}>
      {changesBody}
    </EditorPanel>
  ) : panel === "items" ? (
    <EditorPanel id="items" title={tShell("panel_items")} focusOnOpen={panelFocus} onClose={() => closePanel("items")}
      subtitle={t("items_subtitle", { page: currentPage, count: pageItems.length })}>
      <ItemsPanelBody items={pageItems} missing={missingRows} onSelect={(key) => goTo(key)} />
    </EditorPanel>
  ) : panel === "keys" ? (
    <EditorPanel id="keys" title={tShell("panel_keys_title")} focusOnOpen={panelFocus} onClose={() => closePanel("keys")}>
      <KeysPanelBody note={t("keys_note")} groups={[
        { title: t("keys_select"), rows: [
          { what: t("keys_select_next"), keys: "Tab" },
          { what: t("keys_start"), keys: "Enter" },
          { what: t("keys_back"), keys: "Esc" },
        ] },
        { title: t("keys_paragraph"), rows: [
          { what: t("keys_finish"), keys: "Enter" },
          { what: t("keys_line_break"), keys: "⇧Enter" },
        ] },
        { title: t("keys_pages"), rows: [
          { what: t("keys_page_turn"), keys: pageKeys },
          { what: t("keys_undo_redo"), keys: `${undoKey} / ${redoKey}` },
          { what: t("keys_save"), keys: isMac() ? "⌘S" : "Ctrl+S" },
        ] },
      ]} />
    </EditorPanel>
  ) : null;

  // ── Phones (canvas WP1–WP5) ──────────────────────────────────────────────────────
  if (phone) {
    const selectedEditable = Boolean(selected && selected.editable && editableHere(selected.key));
    const selectedWhere = (key: string) => {
      const kind = names.get(key)?.kind ?? "";
      const readOnlyPage = (layout?.readOnly.get(key) ?? []).some((line) => line.page === currentPage - 1) ? currentPage
        : ((layout?.readOnly.get(key) ?? [])[0]?.page ?? -1) + 1 || null;
      const page = pageOf(key) ?? readOnlyPage;
      return page ? t("phone_where", { page, kind }) : kind;
    };
    const closeChanges = () => setPanel(null);
    const sheetTitle = (key: string, badge: ReactNode) => (
      <span className={styles.sheetTitle}>{nameOf(key)}{badge}</span>
    );
    const paragraphSheet = selectedKey && selected && selectedEditable ? (() => {
      const key = selectedKey;
      const number = numbers.get(key);
      const problem = problems.get(key);
      const lineAdded = pendingByKey.has(key) && decisions.get(key) === "pending";
      const reference = referenceParagraph(key);
      return (
        <Dialog presentation="sheet" layer="top" closeLabel={t("close")} onClose={() => setSelectedKey(null)}
          title={sheetTitle(key, number ? <Badge tone="warning" size="sm">{t("pending_badge", { number })}</Badge> : null)}
          footer={(
            <div className={styles.sheetFooter}>
              <Button disabled={!pendingByKey.has(key)} onClick={() => { undoOne(key); setSelectedKey(null); }}>{t("undo_here")}</Button>
              <Button variant="primary" onClick={() => { setSelectedKey(null); setPhoneSheet(null); }}>{t("done")}</Button>
            </div>
          )}>
          <div className={styles.sheetBody}>
            <p className={styles.sheetWhere}>{selectedWhere(key)}</p>
            {reference ? (
              <Button variant="secondary" className={styles.sheetReference} onClick={reference}>
                <LucideGlyph icon={LucideMessageSquareText} size={18} aria-hidden="true" />
                {t(selected.table ? "reference_cell" : "reference_paragraph")}
              </Button>
            ) : null}
            <label>
              <span className="sr-only">{[names.get(key)?.kind, nameOf(key)].filter(Boolean).join(t("label_list_separator"))}</span>
              <textarea className={`settings-textarea ${styles.sheetArea}`} rows={5} value={textOf(key)}
                aria-invalid={problem ? true : undefined}
                aria-describedby={`document-sheet-hint${problem ? " document-sheet-problem" : ""}${lineAdded ? " document-sheet-note" : ""}`}
                onChange={(event) => change(key, event.target.value)} />
            </label>
            {problem ? <p id="document-sheet-problem" className="settings-field__error" role="alert">{t(`problem_${problem}`)}</p> : null}
            {lineAdded && number ? <p id="document-sheet-note" className={styles.sheetNote}>{t("phone_pending_field_note", { number })}</p> : null}
            <p id="document-sheet-hint" className={styles.sheetHint}>{t("phone_field_hint")}</p>
          </div>
        </Dialog>
      );
    })() : null;
    // Read-only: headers, footers, notes and body paragraphs that refuse edits (canvas WP4a).
    const readOnlySheet = selectedKey && (selectedReadOnly || (selected && !selectedEditable)) ? (() => {
      const key = selectedKey;
      const reason = reasonText(selectedReadOnly?.reason ?? selected?.reason) || t("readonly_badge");
      const paragraphReference = selected ? referenceParagraph(key) : undefined;
      const readOnlyPage = (layout?.readOnly.get(key) ?? []).some((line) => line.page === currentPage - 1) ? currentPage
        : ((layout?.readOnly.get(key) ?? [])[0]?.page ?? -1) + 1;
      const pageReference = !paragraphReference && fileRef && onTogglePage && readOnlyPage > 0
        ? () => { onTogglePage(fileRef, readOnlyPage); setSelectedKey(null); }
        : undefined;
      return (
        <Dialog presentation="sheet" layer="top" closeLabel={t("close")} onClose={() => setSelectedKey(null)}
          title={sheetTitle(key, <Badge tone="outline" size="sm">{t("readonly_badge")}</Badge>)}>
          <div className={styles.sheetBody}>
            <p className={styles.sheetWhere}>{selectedWhere(key)}</p>
            <p className={styles.sheetReadOnly}>
              <LucideGlyph icon={LucideLock} size={18} aria-hidden="true" />
              <span>{t("phone_readonly_body", { reason })}</span>
            </p>
            {paragraphReference || pageReference ? (
              <Button variant="primary" className={styles.sheetButton} onClick={paragraphReference ?? pageReference}>
                <LucideGlyph icon={LucideMessageSquareText} size={18} aria-hidden="true" />
                {paragraphReference ? t(selected?.table ? "reference_cell" : "reference_paragraph") : t("reference_page")}
              </Button>
            ) : null}
            <Button className={styles.sheetButton} onClick={() => { window.open(downloadHref, "_blank", "noopener"); }}>
              <LucideGlyph icon={LucideDownload} size={18} aria-hidden="true" />{t("download_file")}
            </Button>
          </div>
        </Dialog>
      );
    })() : null;
    const missingItems: ItemRow[] = missingRows.map((row) => ({ key: row.key, name: row.name, kind: names.get(row.key)?.kind ?? "", editable: true, number: row.number, current: false }));
    const pageSheet = phoneSheet && !selectedKey && panel !== "changes" ? (
      <Dialog presentation="sheet" layer="top" closeLabel={t("close")} onClose={() => setPhoneSheet(null)}
        title={phoneSheet.kind === "page" ? t("phone_page_sheet_title", { page: phoneSheet.page }) : t("missing_title", { count: missingItems.length })}>
        <div className={styles.sheetBody}>
          {phoneSheet.kind === "page"
            ? <ItemsPanelBody items={itemsOn(phoneSheet.page)} missing={[]} onSelect={select} intro={t("phone_items_intro")} />
            : <ItemsPanelBody items={missingItems} missing={[]} onSelect={select} intro={t("missing_hint")} />}
        </div>
      </Dialog>
    ) : null;
    const changesSheet = panel === "changes" ? (
      <Dialog presentation="sheet" layer="top" closeLabel={t("close")} onClose={closeChanges}
        title={changesSubtitle ? `${tShell("panel_changes")}${t("phone_title_separator")}${changesSubtitle}` : tShell("panel_changes")}
        footer={saved || !base ? undefined : (
          <div className={styles.sheetFooter}>
            {changesFooter ? <p className={styles.sheetFooterNote}>{changesFooter}</p> : null}
            {conflict ? (
              <>
                <Button variant="danger" onClick={() => { closeChanges(); void discardAll(); }}>{t("discard_mine")}</Button>
                {conflictApplicable > 0 ? (
                  <Button variant="primary" disabled={!conflict.rows} onClick={() => { closeChanges(); reapply(); }}>
                    {t("reapply", { version: conflict.latest.version_no, count: conflictApplicable })}
                  </Button>
                ) : null}
              </>
            ) : dirty ? (
              <>
                <Button disabled={saving} onClick={() => { closeChanges(); void discardAll(); }}>{t("discard")}</Button>
                <Button variant="primary" disabled={invalid || saving} loading={saving} onClick={() => { void save(); }}>
                  {saveError && saveError.code !== "edit_refused" && saveError.code !== "no_changes" ? tObject("retry_save") : saveLabel}
                </Button>
              </>
            ) : null}
          </div>
        )}>
        <div className={styles.sheetBody}>{changesBody}</div>
      </Dialog>
    ) : null;
    const openChanges = () => { setPanelFocus(true); setPanel("changes"); };
    const savedText = saved ? (savedPages.state === "failed" || savedPages.state === "busy"
      ? t("saved_pages_failed", { version: saved.result.version_no })
      : `${saved.result.new_file ? t("saved_new_file", { filename: saved.result.filename }) : t("saved_version", { version: saved.result.version_no })}${t("phone_title_separator")}${t(showingSaved ? "phone_pages_ready" : "phone_pages_pending")}`) : "";
    const bar: PhoneSaveBarProps = saved ? {
      saved: {
        text: savedText,
        tone: savedPages.state === "failed" || savedPages.state === "busy" ? "danger" : "positive",
        action: { label: t("edit_again"), onClick: () => { void continueEditing(); }, busy: inventoryLoading },
      },
    } : !base || refusal ? {} : conflict ? {
      summary: { lead: t("changes_conflict", { count: pending.length }), tone: "danger", onOpen: openChanges },
      discard: { label: t("discard_mine"), onClick: () => { void discardAll(); }, danger: true },
      primary: conflictApplicable > 0 ? { label: t("reapply", { version: conflict.latest.version_no, count: conflictApplicable }), onClick: reapply, disabled: !conflict.rows } : undefined,
    } : dirty ? {
      summary: { lead: t("changes_dirty", { count: pending.length }), tone: saveError && saveError.code !== "no_changes" ? "danger" : "warning", onOpen: openChanges },
      message: invalid ? t("invalid_pending") : undefined,
      discard: { label: t("discard"), onClick: () => { void discardAll(); }, disabled: saving },
      primary: {
        label: saveError && saveError.code !== "edit_refused" && saveError.code !== "no_changes" ? tObject("retry_save") : saveLabel,
        onClick: () => { void save(); },
        disabled: invalid || saving,
        busy: saving,
      },
    } : {};
    const phoneNotice = saved || !base || inventoryError || refusal ? null
      : saveError && !conflict && saveError.code !== "edit_refused" ? notice
        : reapplied || newFile || agentRunning ? notice
          : layout && layout.missing.length ? (
            <EditorNoticeRow action={<Button size="sm" variant="plain" className={styles.sheetButton} onClick={() => setPhoneSheet({ kind: "missing" })}>{t("phone_missing_open")}</Button>}>
              {t("phone_missing_notice", { count: layout.missing.length })}
            </EditorNoticeRow>
          ) : !guideDismissed ? (
            <EditorNoticeRow dismissLabel={t("guide_dismiss")} onDismiss={() => {
              setGuideDismissed(true);
              try { window.localStorage.setItem(GUIDE_KEY, "1"); } catch { /* Private mode: shown again next time. */ }
            }}>
              {t("phone_guide", { save: saveLabel })}
            </EditorNoticeRow>
          ) : null;
    const phoneBody = inventoryError && !base ? (
      <div className={styles.stageState}>
        <StateBlock tone="error" title={t("inventory_failed_title")}
          body={inventoryError.code === "results_scope_unavailable" || inventoryError.code === "document_not_editable" ? t("inventory_not_editable")
            : inventoryError.code === "document_edit_disabled" ? t("inventory_disabled") : t("inventory_failed_body")}
          primaryAction={{ label: t("retry"), onClick: () => { void loadInventory(artifactId); } }}
          secondaryActions={[{ label: t("download_file"), onClick: () => { window.open(downloadHref, "_blank", "noopener"); } }]} />
      </div>
    ) : basePages.state === "failed" || basePages.state === "busy" ? (
      <div className={styles.stageState}>
        <StateBlock tone="error" title={t(basePages.state === "busy" ? "pages_busy_title" : "pages_failed_title")} body={t("pages_failed_body")}
          primaryAction={{ label: t("retry"), onClick: basePages.reload }}
          secondaryActions={[{ label: t("download_file"), onClick: () => { window.open(downloadHref, "_blank", "noopener"); } }]} />
      </div>
    ) : !pagesReady || !base ? (
      <div className={styles.stageState}><StateBlock tone="progress" title={t("loading_pages")} /></div>
    ) : (
      <>
        {refusal ? (
          <section className={styles.phoneBlocked} role="alertdialog" aria-labelledby="document-blocked-title" aria-describedby="document-blocked-body">
            <LucideGlyph icon={LucideLock} size={20} aria-hidden="true" />
            <h2 id="document-blocked-title" className={styles.blockedTitle} tabIndex={-1}>{t(`blocked_title_${refusal}`)}</h2>
            <p id="document-blocked-body" className={styles.blockedBody}>{t(`blocked_body_${refusal}`)}</p>
            <div className={styles.blockedActions}>
              <Button variant="primary" onClick={() => { window.open(downloadHref, "_blank", "noopener"); }}>
                <LucideGlyph icon={LucideDownload} size={14} aria-hidden="true" />{t("download_file")}
              </Button>
              {onReference ? (
                <Button onClick={() => { onExit({ artifactId, close: true, then: onReference }); }}>
                  <LucideGlyph icon={LucideMessageSquareText} size={14} aria-hidden="true" />{t("reference_file")}
                </Button>
              ) : null}
            </div>
          </section>
        ) : null}
        {saved && (savedPages.state === "failed" || savedPages.state === "busy") ? (
          <div className={styles.phoneNotice}>
            <ErrorNotice message={t("saved_pages_failed", { version: saved.result.version_no })} surface="task"
              action={{ kind: "retry", label: t("retry") }} onAction={savedPages.reload} />
          </div>
        ) : null}
        {pagesNode(!saved && !refusal, saved && !showingSaved && savedPages.state !== "failed" && savedPages.state !== "busy" ? (
          <span className={`${styles.statusPill} ${styles.phoneSaved}`} role="status">
            <LucideGlyph icon={LucideLoaderCircle} size={14} aria-hidden="true" />
            {t("saved_pages_pending", { version: saved.result.version_no })}
          </span>
        ) : undefined)}
      </>
    );
    return (
      <>
        <div className={styles.phoneRoot} data-testid="document-editor" data-layout="phone" ref={rootRef} onKeyDown={onKeyDown}>
          <PhoneTopBar filename={filename} status={status} onExit={() => { void requestExit(false); }}
            onCloseFullView={fromFullView ? () => { void requestExit(true); } : undefined} />
          {phoneNotice ? <div className={styles.phoneNotice}>{phoneNotice}</div> : null}
          <div className={styles.phoneBody} ref={(element) => { stageRef.current = element; setStageElement(element); }} tabIndex={-1}
            role="group" aria-label={t("stage_label", { page: currentPage, count: pageCount })}>
            {phoneBody}
          </div>
          <ToolConfirmationNoticeSlot active />
          <PhoneSaveBar {...bar} />
        </div>
        {pageSheet}
        {paragraphSheet}
        {readOnlySheet}
        {changesSheet}
        <span className="sr-only" role="status" aria-live="polite">{announcement}</span>
        {confirmDialog}
      </>
    );
  }

  return (
    <>
      <EditorShell testId="document-editor" rootRef={(element) => { rootRef.current = element; }} onKeyDown={onKeyDown}
        topBar={topBar}
        toolbar={({ stripHidden }) => refusal ? <div /> : (
          <EditorToolbar
            undo={{ label: t("tool_undo", { key: undoKey }), onClick: undoLast, disabled: !pending.length || Boolean(saved) || Boolean(conflict) }}
            redo={{ label: t("tool_redo", { key: redoKey }), onClick: redoLast, disabled: !redoStack.length || Boolean(saved) || Boolean(conflict) }}
            zoom={zoom} onZoom={setZoom}
            pages={stripHidden && pageCount > 0 ? { page: currentPage, count: pageCount, onPrevious: () => goPage(currentPage - 1), onNext: () => goPage(currentPage + 1), onShowStrip: () => setStripOpen(true), label: t("outline") } : undefined}
            selection={selection} pageLabel={t("page_of", { page: currentPage, count: Math.max(1, pageCount) })} actions={actions} hint={hint} compact={stripHidden} />
        )}
        notice={notice}
        strip={strip}
        stage={stage}
        panel={panelNode}
        rail={<EditorRail active={panel} changes={pending.length} conflict={Boolean(conflict)} itemsTitle={t("items_title_rail")}
          onToggle={(id) => { setPanelFocus(true); setPanel((current) => (current === id ? null : id)); }} />}
        stripOpen={stripOpen} onStripOpenChange={setStripOpen} />
      <span className="sr-only" role="status" aria-live="polite">{announcement}</span>
      {confirmDialog}
    </>
  );
});

/** Read-only reasons the engine names (design §9); a refusal with one of them says why. */
const REASONS: Record<string, true> = {
  header_footer: true, note: true, tracked_change: true, field_span: true, content_control: true, text_box: true, merged_continuation: true,
};

