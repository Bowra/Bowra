"use client";

/**
 * Files page v2: personal asset library (PRD v2 §4.2, Files Page Redesign mockup).
 *
 * The old page exposed the artifact table directly: uploaded PPTs and query-produced rows_*.csv
 * shared one table, with intermediate data making up 80%+ of rows. v2 splits `kind` into three tabs:
 *   "资产" (default, recognizable assets) · "运行数据" (agent byproducts, count always visible) · "回收站"
 *
 * Consolidate four taxonomies into two primary and one auxiliary: category badge + file type + business-category dropdown;
 * raw source/origin enums are no longer user-facing (the Canvas Data tab still consumes origin).
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import clsx from "clsx";
import { useTranslations } from "next-intl";

import { PageFrame, PageHeader } from "@/components/shell/page-frame";
import { FilterMenuButton } from "@/components/ui/filter-menu-button";
import { CategoryBadge } from "@/components/files/category-badge";
import {
  FilesDialogs,
  FilesToasts,
  type FilesDialog,
  type FilesToast,
  type KbScope,
} from "@/components/files/file-dialogs";
import { FileDrawer } from "@/components/files/file-drawer";
import { KbColumn, kbFileHref } from "@/components/files/kb-column";
import { RowActionsMenu } from "@/components/ui/row-actions-menu";
import { Tab, TabList, Tabs } from "@/components/ui/tabs";
import { appPath } from "@/lib/base-path";
import { handoffAttachmentsToNewChat } from "@/lib/composer-handoff";
import { fetchMe } from "@/lib/auth-api";
import { ListTime } from "@/components/ui/list-time";
import {
  MIME_FAMILIES,
  SORT_OPTIONS,
  type AssetClass,
  type FilesTab,
  assetClassOf,
  fmtSize,
  kindsForTab,
  assetClassQuery,
  midTruncate,
} from "@/lib/files-taxonomy";
import {
  FILE_CATEGORIES,
  FileInKbError,
  type FileDTO,
  deleteFile,
  fetchFilesUsage,
  fileCategoryLabel,
  listFiles,
  promoteFileToKb,
  purgeFiles,
  removeFileFromKb,
  restoreFile,
  uploadFile,
} from "@/lib/api";
import { fileError } from "@/lib/file-error";
import { kbFileSettling } from "@/lib/kb-file-status";
import { useSpaceSelection } from "@/lib/use-space";
import { toast as globalToast } from "@/lib/use-toast";
import {
  Check as LucideCheck,
  FileImage,
  FilePen,
  FileText,
  FileType,
  Files,
  Paperclip,
  Presentation,
  Sheet,
  type LucideIcon,
} from "lucide-react";
import { LucideGlyph } from "@/components/icon-refresh";
import { CharIcon } from "@/components/char-icon";

const TABS: FilesTab[] = ["assets", "run", "trash"];
const CLASS_CHIPS: (AssetClass | "all")[] = ["all", "upload", "result", "export", "kb"];
const PAGE_SIZE = 50;
/** Silently refresh status at this interval while the knowledge-base column has queued/processing rows. */
const KB_POLL_MS = 5000;

/** Uploading row (mockup PENDING): actual progress comes from the presign path; small files use indeterminate progress. */
type PendingUpload = {
  localId: string;
  name: string;
  pct: number;
  error?: string;
  ctrl: AbortController;
  /** Original File: "重试" must reupload it directly instead of reopening the picker and requiring reselection. */
  file: File;
};

const FAMILY_ICON: Record<string, string> = {
  all: "∗", presentation: "📊", pdf: "📄", spreadsheet: "📈",
  document: "📝", text: "📃", image: "🖼", other: "📎",
};

/** Lucide glyph per file family (the emoji above is the glyph's accessible character). */
const FAMILY_LUCIDE: Record<string, LucideIcon> = {
  all: Files, presentation: Presentation, pdf: FileText, spreadsheet: Sheet,
  document: FilePen, text: FileType, image: FileImage, other: Paperclip,
};

function CheckIcon() {
  return (
    <LucideGlyph icon={LucideCheck} size={10} aria-hidden />
  );
}

export default function FilesPage() {
  const t = useTranslations("files_page");

  const [tab, setTab] = useState<FilesTab>("assets");
  const [items, setItems] = useState<FileDTO[]>([]);
  const [total, setTotal] = useState(0);
  const [sizeTotal, setSizeTotal] = useState(0);
  const [counts, setCounts] = useState<Record<FilesTab, number>>({ assets: 0, run: 0, trash: 0 });
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  /** null = identity has not arrived (show a team-card placeholder in the add-to-knowledge-base dialog without briefly showing a lock). */
  const [canPromoteToTeam, setCanPromoteToTeam] = useState<boolean | null>(null);
  const { activeId: activeSpaceId, items: spaces } = useSpaceSelection();
  const activeSpaceName = spaces.find((w) => w.id === activeSpaceId)?.name ?? null;

  // Filters (category chips / business category are secondary frontend filters; the backend already fetches the tab's kind set)
  const [qInput, setQInput] = useState("");
  const [q, setQ] = useState("");
  const [cls, setCls] = useState<AssetClass | "all">("all");
  const [family, setFamily] = useState<string>("all");
  const [biz, setBiz] = useState<string>("all");
  const [sort, setSort] = useState<(typeof SORT_OPTIONS)[number]>("created_desc");
  const [offset, setOffset] = useState(0);

  const [sel, setSel] = useState<string[]>([]);
  const [openId, setOpenId] = useState<string | null>(null);
  const [dlg, setDlg] = useState<FilesDialog | null>(null);
  const [toasts, setToasts] = useState<FilesToast[]>([]);
  const [flashId, setFlashId] = useState<string | null>(null);
  const [pending, setPending] = useState<PendingUpload[]>([]);
  /** P4 usage split: bytes for assets / run data. Display both separately so users know which group to clean up. */
  const [usage, setUsage] = useState<{ assets: number; run_data: number } | null>(null);
  const uploading = pending.some((p) => !p.error);
  const uploadRef = useRef<HTMLInputElement>(null);
  const focusHandled = useRef(false);

  const pushToast = useCallback((text: string, action?: FilesToast["action"]) => {
    const id = Math.random().toString(36).slice(2);
    setToasts((l) => [...l, { id, text, action }]);
    if (action) {
      // Give linked notices enough time for users to click
      setTimeout(() => setToasts((l) => l.filter((x) => x.id !== id)), 8000);
    } else {
      setTimeout(() => setToasts((l) => l.filter((x) => x.id !== id)), 3200);
    }
  }, []);

  const fetchList = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      // The server filters asset-tab categories and business categories before pagination (FR7: all knowledge-base files must be discoverable here)
      const classQuery = tab === "assets" ? assetClassQuery(cls) : null;
      const r = await listFiles({
        q: q || undefined,
        kind: classQuery ? classQuery.kind : kindsForTab(tab),
        in_kb: classQuery?.in_kb,
        category: tab === "assets" && biz !== "all" ? biz : undefined,
        // Render the file-type dropdown only on the assets tab. Checking `tab !== "trash"` would keep the previous
        // tab's pdf selection active on run data, hiding half the list without any visible control
        // explaining why. Match cls / biz scope: apply only to assets.
        mime_family: tab === "assets" && family !== "all" ? [family] : undefined,
        // Fetch trash with include_deleted; kind filtering is irrelevant because deleted items from every tab appear here
        source: tab === "trash" ? ["all"] : undefined,
        sort,
        limit: PAGE_SIZE,
        offset,
        include_deleted: tab === "trash",
      });
      setItems(r.items);
      setTotal(r.total);
      setSizeTotal(r.size_total_bytes);
      // Do **not** backfill counts here: this request includes search/type filters, so r.total is a filtered count.
      // Assigning it to counts would make the current badge filtered and the other two unfiltered, mixing meanings
      // within one row and undermining the run-data count's reassurance that data has not been lost.
      // Only refreshCounts (unfiltered) updates badges; rerun it after every addition/deletion.
    } catch (e) {
      setError(t(fileError(e, "load")));
      setItems([]);
    } finally {
      setLoading(false);
    }
  }, [tab, q, family, sort, offset, cls, biz, t]);

  useEffect(() => { void fetchList(); }, [fetchList]);

  // The server provides knowledge-base status: poll while any row is queued/processing, then stop automatically.
  const kbSettling = items.some((f) => f.kb && kbFileSettling(f.kb));
  useEffect(() => {
    if (!kbSettling || tab === "trash") return;
    const h = setTimeout(() => { void fetchList(); }, KB_POLL_MS);
    return () => clearTimeout(h);
  }, [kbSettling, tab, items, fetchList]);

  /** Keep all three tab counts visible; the run-data count reassures users that data is still present, just tucked away. */
  const refreshCounts = useCallback(async () => {
    const probe = async (tb: FilesTab) => {
      try {
        const r = await listFiles({
          kind: kindsForTab(tb),
          source: tb === "trash" ? ["all"] : undefined,
          limit: 1,
          include_deleted: tb === "trash",
        });
        return r.total;
      } catch {
        return 0;
      }
    };
    const [a, r, tr] = await Promise.all([probe("assets"), probe("run"), probe("trash")]);
    setCounts({ assets: a, run: r, trash: tr });
  }, []);
  useEffect(() => { void refreshCounts(); }, [refreshCounts]);

  const refreshUsage = useCallback(async () => {
    try { setUsage(await fetchFilesUsage()); } catch { setUsage(null); }
  }, []);
  useEffect(() => { void refreshUsage(); }, [refreshUsage]);

  useEffect(() => {
    let active = true;
    void fetchMe()
      .then((me) => {
        if (!active) return;
        setCanPromoteToTeam(
          !!me.is_team_admin ||
            me.team_role === "owner" ||
            me.team_role === "team_admin",
        );
      })
      .catch(() => {
        if (active) setCanPromoteToTeam(false);
      });
    return () => {
      active = false;
    };
  }, []);

  useEffect(() => {
    const h = setTimeout(() => setQ(qInput.trim()), 300);
    return () => clearTimeout(h);
  }, [qInput]);

  useEffect(() => { setOffset(0); setSel([]); }, [tab, q, cls, family, biz, sort]);

  /** Deep link ?focus= must work across tabs (PRD §4.2.5): the target is often in run data. */
  useEffect(() => {
    if (focusHandled.current) return;
    const focus = new URLSearchParams(window.location.search).get("focus");
    if (!focus) return;
    focusHandled.current = true;
    void (async () => {
      for (const tb of TABS) {
        try {
          const r = await listFiles({
            q: focus,
            kind: kindsForTab(tb),
            source: tb === "trash" ? ["all"] : undefined,
            limit: 1,
            include_deleted: tb === "trash",
          });
          if (r.items.some((it) => it.file_id === focus)) {
            setTab(tb);
            setQInput(focus);
            setFlashId(focus);
            setOpenId(focus);
            setTimeout(() => setFlashId(null), 2600);
            return;
          }
        } catch { /* Try the next tab */ }
      }
    })();
  }, []);

  const gotoKb = useCallback(() => { window.location.assign(appPath("/knowledge/")); }, []);

  /**
   * "在新会话中使用": start a new chat with this **existing** artifact (without reuploading bytes).
   * Share the lib/composer-handoff channel with the knowledge-base drawer's "在对话中使用".
   */
  const useInNewChat = useCallback((f: FileDTO) => {
    handoffAttachmentsToNewChat([{ fileId: f.file_id, filename: f.filename, size: f.size, mime: f.mime }]);
  }, []);

  // Undo Toast actions may run up to 8 seconds later: read current tabs and filters from refs when refreshing.
  const refreshAfterUndoRef = useRef(async () => {});
  refreshAfterUndoRef.current = async () => {
    await fetchList();
    await refreshCounts();
    await refreshUsage();
  };

  const doDelete = useCallback(async (ids: string[], blockedInput: number) => {
    setDlg(null);
    let blocked = blockedInput;
    const names = new Map(items.map((i) => [i.file_id, i.filename]));
    const ok: string[] = [];
    let kbRejected = 0;
    for (const id of ids) {
      try {
        await deleteFile(id);
        ok.push(id);
      } catch (e) {
        // Backend D2 safeguard: frontend in_kb may be stale (the file was just added to the knowledge base elsewhere)
        if (e instanceof FileInKbError) kbRejected += 1;
        /* Other individual failures do not interrupt the batch */
      }
    }
    // A single deletion hits the backend safeguard → show the guidance dialog instead of failing silently
    if (ok.length === 0 && kbRejected === 1 && ids.length === 1) {
      const f = items.find((i) => i.file_id === ids[0]);
      if (f) {
        setDlg({ kind: "del-kb", name: midTruncate(f.filename, 28), onGoto: gotoKb });
        return;
      }
    }
    blocked += kbRejected;
    setSel([]);
    setOpenId(null);
    await fetchList();
    await refreshCounts();
    await refreshUsage();
    // Behavior Batch B: deletions offer the global Undo Toast (Menus Spec section 5, at least 8 seconds, pause on hover),
    // which calls the restore endpoint and refreshes the list, counts, and usage.
    const undoToast = (text: string) => {
      // Files still in trash awaiting undo: retain the notice after partial restore failure; the next click retries only failures
      let pending = [...ok];
      globalToast({
        kind: "info",
        undo: true,
        text,
        action: {
          label: t("toast.undo"),
          onAction: async () => {
            const targets = pending;
            const results = await Promise.allSettled(targets.map((id) => restoreFile(id)));
            pending = targets.filter((_, index) => results[index].status === "rejected");
            await refreshAfterUndoRef.current();
            if (pending.length === targets.length) {
              globalToast({ kind: "error", text: t("toast.undo_failed") });
              // Retain the undo notice (resume the timer) so the user can retry
              throw new Error("files_restore_failed");
            }
            if (pending.length > 0) {
              globalToast({
                kind: "error",
                text: t("toast.undo_partial", { restored: targets.length - pending.length, failed: pending.length }),
              });
              throw new Error("files_restore_partial");
            }
            globalToast({ kind: "info", text: t("toast.undone") });
          },
        },
      });
    };
    // If all deletions fail without knowledge-base blocking, say "删除失败", not "已删除 0 个文件"
    // (the latter sounds successful, so users will not retry)
    if (ok.length === 0 && blocked === 0) pushToast(t("toast.delete_failed", { count: ids.length }));
    else if (ok.length === 0 && blocked > 0) pushToast(t("toast.all_blocked", { count: blocked }));
    else if (blocked > 0) undoToast(t("toast.deleted_partial", { count: ok.length, blocked }));
    else if (ok.length === 1) undoToast(t("toast.deleted", { name: midTruncate(names.get(ok[0]) ?? "", 24) }));
    else undoToast(t("toast.deleted_n", { count: ok.length }));
  }, [fetchList, gotoKb, items, pushToast, refreshCounts, refreshUsage, t]);

  const askDelete = useCallback((f: FileDTO) => {
    // D2: knowledge-base files cannot be deleted here; offer guidance without a bypass.
    if (f.in_kb) {
      setDlg({ kind: "del-kb", name: midTruncate(f.filename, 28), onGoto: gotoKb });
      return;
    }
    // Behavior Batch B (user decision 2026-09-28): delete immediately without confirmation and show undo.
    // For files referenced by reports, still explain the consequence first (report downloads will break); this is not routine confirmation.
    if (!f.has_canvas_ref) {
      void doDelete([f.file_id], 0);
      return;
    }
    // Report/canvas references → warn but permit deletion (informed consent); otherwise show a lightweight confirmation
    setDlg({
      kind: f.has_canvas_ref ? "del-ref" : "del",
      name: midTruncate(f.filename, 28),
      onConfirm: () => void doDelete([f.file_id], 0),
    });
  }, [doDelete, gotoKb]);

  const askAddToKb = useCallback((f: FileDTO) => {
    if (!f.kb_promotable) {
      pushToast(t("kb.kb_unsupported"));
      return;
    }
    setDlg({
      kind: "kb-add",
      name: f.filename,
      onConfirm: (scope: KbScope) => {
        setDlg(null);
        void (async () => {
          try {
            const added = await promoteFileToKb(f.file_id, scope);
            await fetchList();
            // FR7.4: after adding, link to processing progress in the knowledge base (open the matching scope and file drawer)
            pushToast(t("kb.kb_added", { name: midTruncate(f.filename, 24) }), {
              label: t("kb_col.view_progress"),
              href: kbFileHref({ file_id: added.file_id, scope, workspace_id: added.workspace_id }),
            });
          } catch (e) {
            const msg = (e as Error).message;
            const messageKey = {
              unsupported: "kb.kb_unsupported",
              space_required: "kb.kb_space_required",
              forbidden: "kb.kb_forbidden",
              kb_embedding_not_configured: "kb.kb_embedding_not_configured",
              promotion_failed: "kb.kb_failed",
              knowledge_ingestion_admission_closed: "kb.kb_write_closed",
              knowledge_destination_mismatch: "kb.kb_destination_mismatch",
              knowledge_operation_removed: "kb.kb_action_removed",
              knowledge_operation_intent_conflict: "kb.kb_write_conflict",
              knowledge_membership_conflict: "kb.kb_write_conflict",
            }[msg] ?? "kb.kb_failed";
            pushToast(t(messageKey));
          }
        })();
      },
    });
  }, [fetchList, pushToast, t]);

  const askRemoveFromKb = useCallback((f: FileDTO) => {
    setDlg({
      kind: "kb-remove",
      name: midTruncate(f.filename, 28),
      onConfirm: () => {
        setDlg(null);
        void (async () => {
          try {
            await removeFileFromKb(f.file_id);
            await fetchList();
            pushToast(t("kb.kb_removed", { name: midTruncate(f.filename, 24) }));
          } catch (e) {
            const msg = (e as Error).message;
            const messageKey = {
              space_required: "kb.kb_remove_space_required",
              forbidden: "kb.kb_remove_forbidden",
              removal_failed: "kb.kb_remove_failed",
            }[msg] ?? "kb.kb_remove_failed";
            pushToast(t(messageKey));
          }
        })();
      },
    });
  }, [fetchList, pushToast, t]);

  /** P4 permanent deletion. The server only deletes files owned by the user and already in trash; knowledge-base references block deletion
   *  (returning kb_blocked). Report that count to users instead of silently deleting fewer files. */
  const doPurge = useCallback(async (ids: string[]) => {
    setDlg(null);
    try {
      const r = await purgeFiles(ids);
      setSel([]);
      setOpenId(null);
      await fetchList();
      await refreshCounts();
      await refreshUsage();
      if (r.kb_blocked > 0) pushToast(t("toast.purged_partial", { count: r.purged, blocked: r.kb_blocked }));
      else pushToast(t("toast.purged", { count: r.purged }));
    } catch (e) {
      pushToast(t(fileError(e, "purge")));
    }
  }, [fetchList, pushToast, refreshCounts, refreshUsage, t]);

  const askPurge = useCallback((ids: string[]) => {
    const blocked = ids.filter((id) => items.find((f) => f.file_id === id)?.in_kb).length;
    setDlg({ kind: "purge", count: ids.length, blocked, onConfirm: () => void doPurge(ids) });
  }, [doPurge, items]);

  const doRestore = useCallback(async (f: FileDTO) => {
    try {
      await restoreFile(f.file_id);
      setOpenId(null);
      await fetchList();
      await refreshCounts();
      pushToast(t("toast.restored", { name: midTruncate(f.filename, 24) }));
    } catch (e) {
      pushToast(t(fileError(e, "restore")));
    }
  }, [fetchList, pushToast, refreshCounts, refreshUsage, t]);

  const onUpload = useCallback(async (file: File) => {
    const localId = Math.random().toString(36).slice(2);
    const ctrl = new AbortController();
    setPending((l) => [...l, { localId, name: file.name, pct: 0, ctrl, file }]);
    try {
      await uploadFile(file, {
        signal: ctrl.signal,
        onProgress: (pct) =>
          setPending((l) => l.map((x) => (x.localId === localId ? { ...x, pct } : x))),
      });
      setPending((l) => l.filter((x) => x.localId !== localId));
      await fetchList();
      await refreshCounts();
      await refreshUsage();
    } catch (e) {
      if (ctrl.signal.aborted) {
        setPending((l) => l.filter((x) => x.localId !== localId));
        return;
      }
      // Keep failures at the top of the list with a reason and retry/remove (mockup errrow), rather than a disappearing toast
      setPending((l) => l.map((x) => (
        x.localId === localId
          ? { ...x, error: t(fileError(e, "upload")) }
          : x
      )));
    }
  }, [fetchList, refreshCounts, refreshUsage, t]);

  /** "重试" reuploads the original file. Previously, `uploadRef.click()` labeled the action as retry
   *  but actually reopened the picker for reselection, contradicting the label. */
  const retryUpload = useCallback((p: PendingUpload) => {
    setPending((l) => l.filter((x) => x.localId !== p.localId));
    void onUpload(p.file);
  }, [onUpload]);

  const rows = items;

  const kbBlocked = useMemo(
    () => sel.filter((id) => rows.find((f) => f.file_id === id)?.in_kb).length,
    [sel, rows],
  );
  const openRow = rows.find((f) => f.file_id === openId) ?? items.find((f) => f.file_id === openId) ?? null;
  const allOn = rows.length > 0 && rows.every((f) => sel.includes(f.file_id));
  const page = Math.floor(offset / PAGE_SIZE) + 1;
  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));

  const stat =
    tab === "trash"
      ? t("stat_trash", { count: counts.trash })
      : tab === "run"
        ? t("stat_run", { size: fmtSize(sizeTotal), count: total })
        : t("stat_assets", { size: fmtSize(sizeTotal), count: total });
  // P4 usage split (PRD §4.6): the current tab's size does not tell users which group to clean up. Show assets and run
  // data **together**, revealing the actual breakdown, e.g. my PPT takes 30MB and the agent's intermediate
  // data takes 168MB. Provide visibility without imposing a hard quota.
  const usageLine = usage
    ? t("usage_split", { assets: fmtSize(usage.assets), run: fmtSize(usage.run_data) })
    : null;
  // One storage summary in the top bar (116 dogfood 1.5.25 showed "89.2 MB total · 582 assets | Assets 89.2 MB ·
  // Run data 1.08 GB"): the split already names both groups' sizes and the tabs carry the counts, so the per-tab
  // size line only stands in until the split loads, and the trash keeps its own count line.
  const topbarSummary = tab === "trash" ? stat : usageLine ?? stat;

  const toggle = (id: string) => setSel((s) => (s.includes(id) ? s.filter((x) => x !== id) : [...s, id]));

  const body = () => {
    if (loading && !items.length) {
      return (
        <div className="sk" aria-busy>
          {[62, 44, 71, 38, 55].map((w, i) => (
            <div className="sk-row" key={i}>
              <div className="sk-b legacy-files-sk-icon" />
              <div className="sk-b" style={{ width: `${w}%` }} />
              <div className="sk-b legacy-files-sk-meta" />
            </div>
          ))}
        </div>
      );
    }
    if (error) {
      return (
        <div className="f-state">
          <p><b>{t("state.error_title")}</b>{t("state.error_body")}</p>
          <button type="button" className="btn sec" onClick={() => void fetchList()}>{t("state.retry")}</button>
        </div>
      );
    }
    if (!rows.length && q) {
      return (
        <div className="f-state">
          <p><b>{t("state.no_match_title", { q })}</b>{t("state.no_match_body")}</p>
          <button type="button" className="btn sec" onClick={() => setQInput("")}>{t("state.clear_search")}</button>
        </div>
      );
    }
    if (!rows.length) {
      return (
        <div className="f-state">
          {tab === "assets" && (
            <>
              <p><b>{t("state.empty_assets_title")}</b>{t("state.empty_assets_body")}</p>
              <button type="button" className="btn pri" disabled={uploading} onClick={() => uploadRef.current?.click()}>
                {uploading ? t("upload_uploading") : t("upload_btn")}
              </button>
            </>
          )}
          {tab === "run" && <p><b>{t("state.empty_run_title")}</b>{t("state.empty_run_body")}</p>}
          {tab === "trash" && <p><b>{t("state.empty_trash_title")}</b>{t("state.empty_trash_body")}</p>}
        </div>
      );
    }

    return (
      <>
        <div className="f-head">
          <button
            type="button"
            className={clsx("ck", { on: allOn })}
            aria-label={t("bulk.selected", { count: sel.length })}
            onClick={() => setSel(allOn ? [] : rows.map((f) => f.file_id))}
          >
            <CheckIcon />
          </button>
          <span />
          <span>{t("table.filename")}</span>
          <span>{tab === "assets" ? t("filter.category") : tab === "run" ? t("source_session") : t("trash_label")}</span>
          {tab === "assets" && <span>{t("table.kb")}</span>}
          <span className="num">{t("table.size")}</span>
          <span>{t("table.uploaded")}</span>
          {tab === "assets" && <span className="col-wide">{t("table.last_used")}</span>}
          {tab === "assets" && <span className="num col-wide">{t("table.usage")}</span>}
          <span />
        </div>
        <div className="f-scroll">
          {pending.map((p) => (
            <div className={clsx("f-row pend", { errrow: !!p.error })} key={p.localId}>
              <span />
              <span className="ficon" aria-hidden><CharIcon ch="📎" aria-hidden /></span>
              <div className="cellname"><span className="fname">{midTruncate(p.name)}</span></div>
              <div className="pendcell">
                {p.error ? (
                  <span className="perr">{t("upload_failed", { reason: p.error })}</span>
                ) : (
                  <>
                    <span className="prog"><i style={{ width: `${p.pct || 8}%` }} /></span>
                    <span className="mono dim">{p.pct ? `${p.pct}%` : t("upload_uploading")}</span>
                  </>
                )}
              </div>
              <div className="f-acts legacy-files-acts--pinned">
                {p.error ? (
                  <>
                    <button type="button" className="kbmanage" onClick={() => retryUpload(p)}>{t("state.retry")}</button>
                    <button
                      type="button"
                      className="iact"
                      aria-label={t("bulk.cancel")}
                      onClick={() => setPending((l) => l.filter((x) => x.localId !== p.localId))}
                    ><CharIcon ch="✕" aria-hidden /></button>
                  </>
                ) : (
                  <button
                    type="button"
                    className="iact"
                    aria-label={t("bulk.cancel")}
                    onClick={() => p.ctrl.abort()}
                  ><CharIcon ch="✕" aria-hidden /></button>
                )}
              </div>
            </div>
          ))}
          {rows.map((f) => {
            const on = sel.includes(f.file_id);
            return (
              <div
                key={f.file_id}
                data-file-id={f.file_id}
                className={clsx("f-row", { "sel-on": on, flash: flashId === f.file_id })}
                tabIndex={0}
                onClick={() => setOpenId(f.file_id)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") setOpenId(f.file_id);
                  if (e.key === "Delete" || e.key === "Backspace") {
                    e.preventDefault();
                    if (tab === "trash") void doRestore(f);
                    else askDelete(f);
                  }
                }}
              >
                <button
                  type="button"
                  className={clsx("ck", { on })}
                  aria-label={f.filename}
                  onClick={(e) => { e.stopPropagation(); toggle(f.file_id); }}
                >
                  <CheckIcon />
                </button>
                <span className="ficon" aria-hidden><CharIcon ch={FAMILY_ICON[f.mime_family] ?? "📎"} icon={FAMILY_LUCIDE[f.mime_family] ?? Paperclip} aria-hidden /></span>
                <div className="cellname">
                  <span className="fname" title={f.filename}>{midTruncate(f.filename)}</span>
                  {f.trust.startsWith("export_copy_legacy_") ? (
                    <span className="sugchip" title={t("export_state.hint")}>{t("export_state.badge")}</span>
                  ) : null}
                </div>
                {tab === "assets" && (
                  <div className="cellcat">
                    <CategoryBadge
                      cls={assetClassOf(f)}
                      onRemove={f.in_kb ? () => askRemoveFromKb(f) : undefined}
                    />
                  </div>
                )}
                {tab === "assets" && (
                  <div className="cellkb">
                    <KbColumn file={f} onAdd={() => askAddToKb(f)} />
                  </div>
                )}
                {tab === "run" && (
                  f.session_id ? (
                    <a
                      className="srclink"
                      href={appPath(`/?session=${encodeURIComponent(f.session_id)}`)}
                      title={f.session_title ?? f.session_id}
                      onClick={(e) => e.stopPropagation()}
                    >{f.session_title || f.session_id}</a>
                  ) : <span className="srclink dim">—</span>
                )}
                {tab === "trash" && <span className="remain">{t("remain_days", { days: 30 })}</span>}
                {/* display:contents: transparent on desktop, so children participate directly in grid columns;
                    on mobile, use a secondary flex row to align size · time · count together. */}
                <div className="f-meta">
                  <span className="num">{fmtSize(f.size)}</span>
                  <ListTime className="dim f-time" value={f.created_at} />
                  {tab === "assets" && <ListTime className="dim f-time col-wide" value={f.last_used_at} />}
                  {tab === "assets" && <span className="num dim col-wide">{f.usage_count}</span>}
                </div>
                <div className="f-acts" onClick={(e) => e.stopPropagation()} role="presentation">
                  <RowActionsMenu
                    label={t("more_actions_for", { name: f.filename })}
                    actions={
                      tab === "trash"
                        ? [
                            { id: "restore", label: t("ops.restore"), onSelect: () => void doRestore(f) },
                            { id: "purge", label: t("ops.purge"), danger: true, onSelect: () => askPurge([f.file_id]) },
                          ]
                        : [
                            { id: "download", label: t("ops.download"), href: appPath(`/api/v1/files/${encodeURIComponent(f.file_id)}`), newTab: true },
                            { id: "use-in-chat", label: t("use_in_chat"), onSelect: () => useInNewChat(f) },
                            // D2: knowledge-base files have no delete action on this page
                            ...(f.in_kb ? [] : [{ id: "delete", label: t("ops.delete"), danger: true, confirms: f.has_canvas_ref, onSelect: () => askDelete(f) }]),
                          ]
                    }
                  />
                </div>
              </div>
            );
          })}
        </div>
        <div className="f-pager">
          <button type="button" className="pgb" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}><CharIcon ch="←" /></button>
          <span>{t("page_indicator", { page, total: totalPages })}</span>
          <button type="button" className="pgb" disabled={offset + PAGE_SIZE >= total} onClick={() => setOffset(offset + PAGE_SIZE)}><CharIcon ch="→" /></button>
        </div>
      </>
    );
  };

  return (
    // PageFrame (Header, Language & Paging Spec §2, §6 #15): the standard header and the 1120 column; the
    // existing underline tabs stay. The column is a flex column so the file list keeps scrolling inside .f-scroll.
    <PageFrame
      title={t("title")}
      mainClassName="v2-main files-main"
      className="files-frame"
      topbarMeta={
        <span className="meta">
          {topbarSummary === usageLine && usageLine ? (
            <span className="usage-split" data-testid="files-usage">{usageLine}</span>
          ) : (
            topbarSummary
          )}
        </span>
      }
    >
      <PageHeader
        description={(
          <span className="f-lede" data-testid="files-lede">
            {t.rich("lede", {
              kb: (chunks) => <a href={appPath("/knowledge/")}>{chunks}</a>,
            })}
          </span>
        )}
      />

      <Tabs value={tab} onValueChange={(k) => { setTab(k); setOpenId(null); }}>
        <TabList appearance="underline" className="f-tabs">
          {TABS.map((k) => (
            <Tab
              key={k}
              value={k}
              className={clsx("f-tab", { on: tab === k })}
              data-testid={`files-tab-${k}`}
            >
              {t(`tabs.${k}`)}
              <span className="cnt">{counts[k]}</span>
            </Tab>
          ))}
        </TabList>
      </Tabs>

      <div className="f-tools">
        <div className="f-search">
          <input
            type="search"
            value={qInput}
            onChange={(e) => setQInput(e.target.value)}
            placeholder={t("search_placeholder")}
            aria-label={t("search_placeholder")}
          />
        </div>
        {tab === "assets" && (
          <>
            <div className="f-fgroup">
              <span className="f-flabel">{t("filter.category")}</span>
              <div className="chips">
                {CLASS_CHIPS.map((k) => (
                  <button
                    key={k}
                    type="button"
                    className={clsx("chip", { on: cls === k })}
                    data-testid={`files-class-${k}`}
                    onClick={() => setCls(k)}
                  >
                    {k === "all" ? t("filter.all") : t(`asset_class.${k}`)}
                  </button>
                ))}
              </div>
            </div>
            {/* Native selects → "Label: Value ⌄" menu buttons, like the reports toolbar (Polish Spec 2f). */}
            <FilterMenuButton
              value={family}
              options={MIME_FAMILIES.map((k) => ({ value: k, label: t(`family.${k}`) }))}
              onChange={setFamily}
              format={(value) => t("filter.type_value", { value })}
              menuLabel={t("filter.type")}
              active={family !== "all"}
              testId="files-type-filter"
            />
            <FilterMenuButton
              value={biz}
              options={[
                { value: "all", label: t("filter.all") },
                ...FILE_CATEGORIES.map((c) => ({ value: c as string, label: fileCategoryLabel(c) })),
              ]}
              onChange={setBiz}
              format={(value) => t("filter.biz_value", { value })}
              menuLabel={t("filter.biz")}
              active={biz !== "all"}
              testId="files-category-filter"
            />
          </>
        )}
        <FilterMenuButton<typeof sort>
          value={sort}
          options={SORT_OPTIONS.map((k) => ({ value: k, label: t(`sort.${k}`) }))}
          onChange={setSort}
          format={(value) => t("filter.sort_value", { value })}
          menuLabel={t("filter.sort")}
          active={false}
          testId="files-sort"
        />
        <span className="sp" />
        {tab === "assets" && (
          <button type="button" className="btn pri" disabled={uploading} onClick={() => uploadRef.current?.click()}>
            {uploading ? t("upload_uploading") : t("upload_btn")}
          </button>
        )}
      </div>

      {tab === "run" && <div className="f-hint">{t("run_hint")}</div>}

      <div className={clsx("f-body", { picking: sel.length > 0 })} data-tab={tab}>
        {body()}
      </div>

      <input
        ref={uploadRef}
        type="file"
        className="legacy-files-upload-input"
        multiple
        onChange={(e) => {
          Array.from(e.target.files ?? []).forEach((f) => void onUpload(f));
          e.target.value = "";
        }}
      />

      {openRow && (
        <FileDrawer
          file={openRow}
          tab={tab}
          onClose={() => setOpenId(null)}
          onDelete={() => askDelete(openRow)}
          onRestore={() => void doRestore(openRow)}
          onGotoKb={() => {
            if (openRow.kb) window.location.assign(kbFileHref(openRow.kb));
            else gotoKb();
          }}
          onUseInChat={() => useInNewChat(openRow)}
          onAddToKb={() => askAddToKb(openRow)}
          onRemoveFromKb={() => askRemoveFromKb(openRow)}
          onFileUpdated={(u) => setItems((l) => l.map((x) => (x.file_id === u.file_id ? u : x)))}
        />
      )}

      {sel.length > 0 && (
        <div className="bulkbar">
          <span className="bl">{t("bulk.selected", { count: sel.length })}</span>
          {tab === "trash" ? (
            <>
            <button
              type="button"
              className="btn sec"
              onClick={() => setDlg({
                kind: "restore-bulk",
                count: sel.length,
                onConfirm: () => {
                  setDlg(null);
                  void (async () => {
                    const targets = rows.filter((f) => sel.includes(f.file_id));
                    for (const f of targets) {
                      try { await restoreFile(f.file_id); } catch { /* ignore */ }
                    }
                    setSel([]);
                    await fetchList();
                    await refreshCounts();
                    pushToast(t("toast.restored_n", { count: targets.length }));
                  })();
                },
              })}
            >{t("bulk.restore")}</button>
              <button
                type="button"
                className="btn sec"
                data-testid="purge-bulk"
                onClick={() => askPurge(sel)}
              >{t("bulk.purge")}</button>
            </>
          ) : (
            <button
              type="button"
              className="btn sec"
              onClick={() => {
                // D2: skip knowledge-base files in batch deletion, but report them explicitly in the result toast.
                const run = () => void doDelete(sel.filter((id) => !rows.find((f) => f.file_id === id)?.in_kb), kbBlocked);
                // Behavior Batch B: deletion is undoable without confirmation (still explain consequences for report-referenced files first)
                if (!sel.some((id) => rows.find((f) => f.file_id === id)?.has_canvas_ref)) {
                  run();
                  return;
                }
                setDlg({ kind: "del-bulk", count: sel.length, blocked: kbBlocked, onConfirm: run });
              }}
            >{t("bulk.delete")}</button>
          )}
          <button type="button" className="btn ghost" onClick={() => setSel([])}>{t("bulk.cancel")}</button>
        </div>
      )}

      <FilesDialogs
        dlg={dlg}
        onClose={() => setDlg(null)}
        canPromoteToTeam={canPromoteToTeam}
        spaceName={activeSpaceName}
      />
      <FilesToasts list={toasts} />
    </PageFrame>
  );
}
