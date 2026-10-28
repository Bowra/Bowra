"use client";
import { readKbFileView, writeKbFileView, type KbFileViewState } from "@/lib/kb-file-view-state";
import { appPath } from "@/lib/base-path";

/**
 * kb-management M1–M5 · Files tab manager.
 *
 * Replaces the legacy toolbar+list for the Files tab. Server-backed:
 *  M1 — scope chips + dual-mode search + status/type/date filters + sort + page-number pagination
 *  M2 — checkbox multi-select + bulk action bar (doc_type / scope / delete) + deferred-delete undo
 *  M4 — parse_failed pill → reparse; extract retry preserved
 *  M5 — scope-change modal (single + bulk), real team-admin gate
 * Wired to the Phase A endpoints (fetchKbFiles paginated/filtered, bulk*, reparse).
 */
import { useLocale, useTranslations } from "next-intl";
import { formatShortDateTime } from "@/lib/locale";
import { useCallback, useEffect, useId, useMemo, useRef, useState } from "react";

import { DialogShell } from "@/components/ui/dialog";
import {
  MenuFloating,
  MenuItemReason,
  useMenu,
  useMenuItem,
  withConfirmEllipsis,
} from "@/components/ui/menu";
import { ApiError } from "@/lib/api-client";
import { handoffAttachmentsToNewChat } from "@/lib/composer-handoff";
import { kbFileError } from "@/lib/kb-file-error";
import { useAppRouter } from "@/lib/use-app-router";
import { useMediaQuery } from "@/lib/use-media-query";
// Non-UI consumers obtain activeId via useSpaceSelection (no intl context needed for activeName);
// it resolves activeId during render and subscribes to space switches across browser tabs.
import { useSpaceSelection } from "@/lib/use-space";
import {
  type KbExtractRowsJobStatus,
  type KbFileDTO,
  type KbFileListResponse,
  type KbListScope,
  type KbScope,
  type KbSearchResultChunk,
  bulkDeleteKbFiles,
  extractRowsKbFile,
  fetchExtractRowsStatusBatch,
  fetchKbFiles,
  formatBytes,
  getKbFile,
  loadKbFileScopePreference,
  reparseKbFile,
  saveKbFileScopePreference,
  searchKb,
} from "@/lib/kb-api";

import { KbDocTypeModal } from "./kb-doctype-modal";
import { KbFileDrawer, kbChunkLocation, kbHighlight } from "./kb-file-drawer";
import { typeMeta } from "./kb-file-types";
import { KbScopeFilter, type KbScopeFilterOption } from "./kb-scope-filter";
import {
  KbFilesControls,
  type SortDir,
  type SortKey,
  type StatusBucket,
} from "./kb-files-controls";
import {
  IconArrowDn,
  IconArrowUp,
  IconCheck,
  IconDots,
  IconFolder,
  IconRetry,
  IconTag,
  IconTrash,
  IconX,
} from "./kb-icons";
import { useKbMe } from "./use-kb-me";
import { KbScopeModal, type ScopeTarget } from "./kb-scope-modal";
import { dismissToast, notify, toast as pushToast } from "@/lib/use-toast";
import { useOptionalAuthSession } from "@/lib/auth/auth-session-context";
import { KbRebuildBatch } from "./kb-rebuild-batch";
import { KbStatusPill, getExtractionState, type ExtractionState } from "./kb-status-pill";
import { kbFailReason, kbFailRetryable, kbFileSettling, kbFileState } from "@/lib/kb-file-status";
import { snippetWindow } from "@/lib/kb-snippet";
import { evictKbChunks } from "@/lib/canvas-pm-adapter/kb-chunk-cache";
import { KbUploadModal } from "./kb-upload-modal";
import {
  Sheet as LucideSheet,
} from "lucide-react";
import { LucideGlyph } from "@/components/icon-refresh";
import { CharIcon, CharIconText } from "@/components/char-icon";

const PAGE_SIZE = 20;
const POLL_MS = 3000;
const SCOPE_IDS: KbScope[] = ["personal", "space", "team"];
// Scope column (scope "All"): inserted after the filename column.
const SCOPE_COL = "96px";

/** True when a list response comes from a server that also lists all scopes at once (it always sends next_cursor). */
function supportsAllScopes(res: KbFileListResponse): boolean {
  return Object.prototype.hasOwnProperty.call(res, "next_cursor");
}
// label resolved per-render via t(`scope_${id}`) (reuses knowledge_base.scope_*)

// status bucket → parse_status values (server `status` csv param)
const STATUS_BUCKET: Record<StatusBucket, string[]> = {
  processing: ["pending", "parsing", "chunking", "embedding", "queued", "awaiting_upload"],
  done: ["done"],
  failed: ["failed"],
};
// mime group → mime_type prefixes (server `mime` csv param, matched LIKE prefix%)
const MIME_PREFIX: Record<string, string[]> = {
  pdf: ["application/pdf"],
  doc: ["application/vnd.openxmlformats-officedocument.wordprocessingml", "application/msword", "text/markdown", "text/plain"],
  sheet: ["application/vnd.openxmlformats-officedocument.spreadsheetml", "application/vnd.ms-excel", "text/csv"],
  slide: ["application/vnd.openxmlformats-officedocument.presentationml", "application/vnd.ms-powerpoint"],
  image: ["image/"],
};
const ORDER_BY: Record<SortKey, string> = { uploadedAt: "uploaded_at", name: "filename", sizeMB: "size_bytes" };

type Translator = (key: string, values?: Record<string, string | number>) => string;

function fmtAgo(iso: string, t: Translator): string {
  const ms = Date.now() - new Date(iso).getTime();
  if (Number.isNaN(ms)) return iso;
  const s = ms / 1000;
  if (s < 90) return t("manager.time.just_now");
  if (s < 3600) return t("manager.time.minutes_ago", { n: Math.round(s / 60) });
  if (s < 86400) return t("manager.time.hours_ago", { n: Math.round(s / 3600) });
  const d = Math.round(s / 86400);
  if (d < 30) return t("manager.time.days_ago", { n: d });
  return t("manager.time.months_ago", { n: Math.round(d / 30) });
}

// Status column 224px (de-DE "Fehlgeschlagen" + retry + row menu), date 120px ("vor 11 Monaten" on one line); hide the "类型" column at ≤ 1280, since filename color blocks already convey type (design 01b·2c).
const COLS = "34px 26px minmax(0,1fr) 96px 84px 120px 224px";
const COLS_NO_TYPE = "34px 26px minmax(0,1fr) 84px 120px 224px";
const NARROW_QUERY = "(max-width: 640px)";
const NO_TYPE_QUERY = "(max-width: 1280px)";

type PerRowJobStatus = KbExtractRowsJobStatus;

function IconGridSmall() {
  return (
    <LucideGlyph icon={LucideSheet} size={13} aria-hidden />
  );
}

/**
 * uploadRequest: supplied when the page header owns Upload (KB left navigation 2b); omit toolbar Upload then.
 * Each +1 opens the upload dialog (>0 on mount also opens); absent = toolbar owns Upload (legacy layout).
 *
 * Deep links ("资料库 › 文件"'s "知识库" column / "查看处理进度" after joining): initialScope selects scope;
 * initialSpaceId switches active space only if in your space list, then opens initialFileId drawer
 * (space-file reads depend on current space). Ignore space IDs absent from the list. initialChunkId (chat KB
 * citation "在知识库中打开") pins and highlights that chunk in the drawer.
 */
export function KbFilesManager({
  viewQuery,
  initialFileId,
  initialChunkId,
  initialScope,
  initialSpaceId,
  uploadRequest,
}: {
  viewQuery?: string;
  initialFileId?: string | null;
  initialChunkId?: string | null;
  initialScope?: KbListScope | null;
  initialSpaceId?: string | null;
  uploadRequest?: number;
} = {}) {
  const t = useTranslations("knowledge_base");
  const router = useAppRouter();
  const toast = notify;
  const retryErrorToastIds = useRef(new Map<string, string>());
  const me = useKbMe();
  const sourceGuard = useOptionalAuthSession()?.me?.capabilities?.kb_use_guard === true;

  // null before space hydration; space-scoped reads must wait, otherwise missing X-Space-Id makes Gateway return 400.
  const {
    activeId: activeSpaceId,
    items: spaceItems,
    loading: spacesLoading,
    setActiveId: setActiveSpaceId,
  } = useSpaceSelection();
  const spaceName = spaceItems.find((w) => w.id === activeSpaceId)?.name ?? null;
  const narrow = useMediaQuery(NARROW_QUERY);
  const hideTypeCol = useMediaQuery(NO_TYPE_QUERY);
  const [scope, setScope] = useState<KbListScope>(initialScope ?? "personal");
  const scopeRef = useRef(scope);
  scopeRef.current = scope;
  const [scopeCounts, setScopeCounts] = useState<Record<KbListScope, number | null>>({ all: null, personal: null, space: null, team: null });
  // Server support for scope=all (feature-detected from list responses); null = not known yet.
  const [allScopes, setAllScopes] = useState<boolean | null>(null);
  // Remembered scope (no scope in the URL): the list waits until the saved choice is known.
  const [scopePrefDone, setScopePrefDone] = useState(false);
  const scopePrefPending = initialScope == null && !scopePrefDone;
  // "All" exists only with a server that supports it; otherwise fall back to personal.
  const allUnavailable = allScopes === false;
  const showScopeCol = scope === "all";
  const baseCols = hideTypeCol ? COLS_NO_TYPE : COLS;
  const cols = showScopeCol ? baseCols.replace("minmax(0,1fr)", `minmax(0,1fr) ${SCOPE_COL}`) : baseCols;
  // Upload needs a concrete scope; from "All" it defaults to the current space (the dialog lets the user change it).
  const uploadDefault: KbScope = scope === "all" ? (activeSpaceId ? "space" : "personal") : scope;
  // Off-page selections ("select all N"): their names and scopes for the scope dialog.
  const selectedMetaRef = useRef(new Map<string, { filename: string; scope: KbScope }>());

  // controls
  const [query, setQuery] = useState("");
  const [deep, setDeep] = useState(false);
  const [fStatus, setFStatus] = useState<Set<StatusBucket>>(new Set());
  const [fTypes, setFTypes] = useState<Set<string>>(new Set());
  const [fDays, setFDays] = useState<number | null>(null);
  const [sortKey, setSortKey] = useState<SortKey>("uploadedAt");
  const [sortDir, setSortDir] = useState<SortDir>("desc");
  const [page, setPage] = useState(1);

  // data
  const [files, setFiles] = useState<KbFileDTO[]>([]);
  // First matching chunk per deeply searched file (used by position labels and drawer pinning).
  const [searchIncomplete, setSearchIncomplete] = useState(false);
  const [searchWarning, setSearchWarning] = useState(false);
  const [hits, setHits] = useState<Record<string, KbSearchResultChunk>>({});
  const [total, setTotal] = useState(0);
  const [hasMore, setHasMore] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  // A failed foreground load (filters, page, scope or retry changed what the list should show) replaces the list
  // with the error card; a failed background refresh (poll, refresh after a mutation) keeps the rows on screen.
  const [errorReplacesList, setErrorReplacesList] = useState(false);
  const [searchTrigger, setSearchTrigger] = useState(0);
  const [jobs, setJobs] = useState<Record<string, PerRowJobStatus>>({});

  // selection + ui
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [menuFor, setMenuFor] = useState<string | null>(null);
  const [drawerFile, setDrawerFile] = useState<KbFileDTO | null>(null);
  const [drawerOpenedWithPointer, setDrawerOpenedWithPointer] = useState(false);
  // Matched chunk / keyword when opening a deep-search hit (pinned drawer highlight).
  const [drawerHit, setDrawerHit] = useState<{ chunk: KbSearchResultChunk; query: string; source?: "citation" } | null>(null);
  // Drawer follows latest polled list data (PRD FR2.2); off-page files (deep links) fall back to their opening snapshot.
  const drawerLive = drawerFile ? (files.find((f) => f.file_id === drawerFile.file_id) ?? drawerFile) : null;
  // Upload-dialog target scope; null = closed.
  const [uploadScope, setUploadScope] = useState<KbScope | null>(null);
  const [docTypeIds, setDocTypeIds] = useState<string[] | null>(null);
  const [scopeModal, setScopeModal] = useState<{ targets: ScopeTarget[]; bulk: boolean } | null>(null);
  const [deleteIds, setDeleteIds] = useState<string[] | null>(null);

  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    if (uploadRequest) setUploadScope(uploadDefault);
  }, [uploadRequest]);

  // Deep-link ?space=: switch only to listed spaces; wait for the list before deciding.
  const deepSpace = initialSpaceId && !spacesLoading && spaceItems.some((w) => w.id === initialSpaceId)
    ? initialSpaceId
    : null;
  // Remember switched space ID, not a boolean: each link switches once; a new link with another space parameter can switch again.
  const deepSpaceSwitched = useRef<string | null>(null);
  useEffect(() => {
    if (!deepSpace || deepSpaceSwitched.current === deepSpace) return;
    deepSpaceSwitched.current = deepSpace;
    if (activeSpaceId !== deepSpace) setActiveSpaceId(deepSpace);
  }, [deepSpace, activeSpaceId, setActiveSpaceId]);
  // Valid space parameter: wait for active-space switch before reading (space-file authorization uses current space); absent/invalid reads directly.
  const deepLinkReady = !initialSpaceId
    ? true
    : spacesLoading
      ? false
      : deepSpace === null || activeSpaceId === deepSpace;

  // Inline notice when deep-link opening fails (deleted file / inaccessible space); no silent return to list.
  const [deepLinkFailed, setDeepLinkFailed] = useState(false);

  // After drawer close (or deletion / deep-link failure), remove file_id / chunk_id from the address bar, retaining other parameters:
  // otherwise refresh reopens it, while repeating the same link does nothing because URL did not change.
  // Use window.location as truth; useSearchParams may lag one update. Router through a ref keeps
  // callback identity stable, so deep-link effect below does not reread files when router reference changes.
  const routerRef = useRef(router);
  routerRef.current = router;
  const clearDeepLinkParams = useCallback(() => {
    if (typeof window === "undefined") return;
    const params = new URLSearchParams(window.location.search);
    if (!params.has("file_id") && !params.has("chunk_id")) return;
    params.delete("file_id");
    params.delete("chunk_id");
    const qs = params.toString();
    routerRef.current.replace(qs ? `/knowledge/?${qs}` : "/knowledge/", { scroll: false });
  }, []);

  const ownNavigation = useRef<string | null>(null);
  const fileTrigger = useRef<HTMLElement | null>(null);
  const previousFileId = useRef(initialFileId);
  const openedFromList = useRef(false);
  const viewState: KbFileViewState = { scope, query, deep, status: [...fStatus], types: [...fTypes], days: fDays, sort: sortKey, order: sortDir, page };
  const commitView = (patch: Partial<KbFileViewState>, replace = false) => {
    if (debounceRef.current) clearTimeout(debounceRef.current);
    const next = { ...viewState, ...patch };
    const params = writeKbFileView(window.location.search, next);
    if (next.scope === "space" && activeSpaceId) params.set("space", activeSpaceId);
    else params.delete("space");
    const key = params.toString();
    if (key === new URLSearchParams(window.location.search).toString()) return;
    ownNavigation.current = key;
    routerRef.current[replace ? "replace" : "push"](`/knowledge/?${key}`, { scroll: false });
  };
  useEffect(() => {
    if (viewQuery === undefined) return;
    if (ownNavigation.current === viewQuery) { ownNavigation.current = null; return; }
    if (debounceRef.current) clearTimeout(debounceRef.current);
    const read = readKbFileView(viewQuery);
    // No scope in the URL (e.g. the sidebar "Knowledge" link): keep the current (restored or chosen) scope.
    const urlScope: KbListScope = read.scope === null
      ? scopeRef.current
      : read.scope;
    const next: KbFileViewState = { ...read, scope: urlScope };
    if (JSON.stringify(next) === JSON.stringify({ ...viewState, status: [...viewState.status].sort(), types: [...viewState.types].sort() })) return;
    setScope(next.scope); setQuery(next.query); setDeep(next.deep);
    setFStatus(new Set(next.status as StatusBucket[])); setFTypes(new Set(next.types)); setFDays(next.days);
    setSortKey(next.sort); setSortDir(next.order); setPage(next.page);
    setSelected(new Set()); setSearchTrigger((n) => n + 1);
  }, [viewQuery]);
  useEffect(() => () => { if (debounceRef.current) clearTimeout(debounceRef.current); }, []);
  useEffect(() => {
    const wasOpen = previousFileId.current;
    previousFileId.current = initialFileId;
    if (initialFileId) return;
    setDrawerFile(null); setDrawerHit(null);
    if (fileTrigger.current?.isConnected) fileTrigger.current.focus({ preventScroll: true });
    else if (wasOpen) document.querySelector<HTMLInputElement>("[data-kb-refined] input[type=search]")?.focus({ preventScroll: true });
  }, [initialFileId]);

  // Remembered scope (no scope in the URL): restore the last choice; the first visit opens the current space.
  // A saved "space" without a selected space falls back to "All".
  useEffect(() => {
    if (!scopePrefPending) return;
    let cancelled = false;
    loadKbFileScopePreference().catch(() => null).then((saved) => {
      if (cancelled) return;
      let next: KbListScope = saved ?? "space";
      if (next === "space" && !activeSpaceId) next = "all";
      if (next !== scopeRef.current) {
        setScope(next);
        commitView({ scope: next, page: 1 }, true);
      }
      setScopePrefDone(true);
    });
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scopePrefPending]);
  // "All" from a URL or a saved choice when the server has no cross-scope list: fall back to personal.
  useEffect(() => {
    if (scope !== "all" || !allUnavailable) return;
    setScope("personal");
    commitView({ scope: "personal", page: 1 }, true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scope, allUnavailable]);

  // deep-link: /knowledge?file_id=X → open drawer (cite popover chain)
  useEffect(() => {
    if (!initialFileId || !deepLinkReady) return;
    let cancelled = false;
    getKbFile(initialFileId).then((dto) => {
      if (cancelled) return;
      setDeepLinkFailed(false);
      setDrawerFile(dto); setDrawerOpenedWithPointer(false);
      if (initialChunkId) {
        setDrawerHit({
          chunk: {
            chunk_id: initialChunkId,
            content: "",
            file_id: dto.file_id,
            file_name: dto.filename,
            page: null,
            section_title: null,
            role: "",
            score: 0,
            scope: dto.scope,
          },
          query: readKbFileView(viewQuery ?? "").query,
          source: readKbFileView(viewQuery ?? "").query ? undefined : "citation",
        });
      }
    }).catch((e) => {
      if (cancelled) return;
      // List loading handles 401 login navigation; explicitly explain other failures (404 deleted / 403 no space access / unlisted space).
      if (e instanceof ApiError && e.status === 401) return;
      setDrawerFile(null); setDrawerHit(null);
      setDeepLinkFailed(true);
      clearDeepLinkParams();
    });
    return () => { cancelled = true; };
  }, [initialFileId, initialChunkId, deepLinkReady, clearDeepLinkParams]);

  const fStatusKey = useMemo(() => [...fStatus].sort().join(","), [fStatus]);
  const fTypesKey = useMemo(() => [...fTypes].sort().join(","), [fTypes]);
  const filtersActive = fStatus.size > 0 || fTypes.size > 0 || fDays !== null || query.trim().length > 0;

  // Keyset cursors for "All" (page number → next_cursor of the page before it). Any change to scope, filters, search,
  // sort or space starts a new list, so the cursors are dropped (a cursor only fits the sort it was issued for).
  const cursorsRef = useRef(new Map<number, string>());
  const cursorKeyRef = useRef("");
  const buildParams = useCallback(() => {
    const statuses = [...fStatus].flatMap((b) => STATUS_BUCKET[b]);
    const prefixes = [...fTypes].flatMap((g) => MIME_PREFIX[g] ?? []);
    const listKey = JSON.stringify([scope, statuses, prefixes, fDays, deep, query.trim(), sortKey, sortDir, activeSpaceId]);
    if (listKey !== cursorKeyRef.current) {
      cursorKeyRef.current = listKey;
      cursorsRef.current.clear();
    }
    const cursor = scope === "all" && page > 1 ? cursorsRef.current.get(page) : undefined;
    return {
      scope,
      ...(cursor ? { cursor } : null),
      offset: cursor ? undefined : (page - 1) * PAGE_SIZE,
      limit: PAGE_SIZE,
      status: statuses.length ? statuses.join(",") : undefined,
      mime: prefixes.length ? prefixes.join(",") : undefined,
      uploaded_after: fDays != null ? new Date(Date.now() - fDays * 86400000).toISOString() : undefined,
      q: !deep && query.trim() ? query.trim() : undefined,
      order_by: ORDER_BY[sortKey] as "uploaded_at" | "filename" | "size_bytes",
      order_dir: sortDir,
    };
  }, [scope, page, fStatus, fTypes, fDays, deep, query, sortKey, sortDir, activeSpaceId]);

  // load-sequence guard: only the most-recently-dispatched reload commits, so a
  // late poll/refresh response can't overwrite a newer scope/filter/page load.
  // Skip polling while a request is in flight (inFlightRef); otherwise server latency above POLL_MS makes each poll mark the previous
  // request stale, discarding every result and freezing the list; even upload/delete refreshes get displaced by the next poll
  // (116 measurement: with processing files present, list stayed unchanged for 5 minutes after upload).
  const loadSeqRef = useRef(0);
  const inFlightRef = useRef(0);
  // Pending deletion during undo: server still returns files through polling / refresh; filter them before updating the list.
  const pendingDeleteIdsRef = useRef<Set<string>>(new Set());
  const withoutPendingDeletes = (rows: KbFileDTO[]) =>
    pendingDeleteIdsRef.current.size === 0
      ? rows
      : rows.filter((row) => !pendingDeleteIdsRef.current.has(row.file_id));
  const reload = useCallback(async ({ poll = false, foreground = false }: { poll?: boolean; foreground?: boolean } = {}) => {
    if (poll && inFlightRef.current > 0) return;
    const seq = ++loadSeqRef.current;
    const current = () => loadSeqRef.current === seq;
    inFlightRef.current += 1;
    try {
      setError(null);
      if (deep && query.trim()) {
        // Deep search stays within current scope (PRD FR6.2); API lacks status/type filters, so apply the same list rules locally.
        const scopeFilter = scope === "all" ? SCOPE_IDS.filter((id) => id !== "space" || activeSpaceId) : [scope];
        const res = await searchKb(query.trim(), { scope_filter: scopeFilter });
        const snip = new Map<string, KbSearchResultChunk>();
        for (const c of res.chunks) if (!snip.has(c.file_id)) snip.set(c.file_id, c);
        const dtos = await Promise.all([...snip.keys()].map((id) => getKbFile(id).catch(() => null)));
        if (!current()) return;
        const statuses = [...fStatus].flatMap((b) => STATUS_BUCKET[b]);
        const prefixes = [...fTypes].flatMap((g) => MIME_PREFIX[g] ?? []);
        const rows = withoutPendingDeletes(dtos.filter((d): d is KbFileDTO => Boolean(d)).filter((d) =>
          (statuses.length === 0 || statuses.includes(d.parse_status))
          && (prefixes.length === 0 || prefixes.some((p) => d.mime_type.startsWith(p))),
        ));
        const filtered = rows.filter((d) => fDays === null || new Date(d.uploaded_at).getTime() >= Date.now() - fDays * 86400000).sort((a, b) => {
          const diff = sortKey === "name" ? a.filename.localeCompare(b.filename) : sortKey === "sizeMB" ? a.size_bytes - b.size_bytes : new Date(a.uploaded_at).getTime() - new Date(b.uploaded_at).getTime();
          return sortDir === "asc" ? diff : -diff;
        });
        setSearchIncomplete(Boolean(res.partial));
        setSearchWarning(res.chunks.some(chunk => Boolean(chunk.source_warning)));
        setFiles(filtered);
        setHits(Object.fromEntries(rows.flatMap((r) => {
          const hit = snip.get(r.file_id);
          return hit ? [[r.file_id, hit]] : [];
        })));
        setTotal(filtered.length);
        setHasMore(false);
      } else {
        const params = buildParams();
        let res: KbFileListResponse;
        try {
          res = await fetchKbFiles(params);
        } catch (e) {
          // A stale or foreign cursor is refused (400): drop the cursors and read the page by offset.
          if (!params.cursor || !(e instanceof ApiError) || e.status !== 400) throw e;
          cursorsRef.current.clear();
          res = await fetchKbFiles({ ...params, cursor: undefined, offset: (page - 1) * PAGE_SIZE });
        }
        if (!current()) return;
        if (scope === "all") {
          // A successful scope=all list is itself proof of server support (the count probe may have failed).
          setAllScopes(true);
          if (res.next_cursor) cursorsRef.current.set(page + 1, res.next_cursor);
        }
        const rows = withoutPendingDeletes(res.files);
        setFiles(rows);
        setHits({});
        setTotal(Math.max(0, res.total - (res.files.length - rows.length)));
        setHasMore(Boolean(res.has_more));
        const maxPage = Math.max(1, Math.ceil(res.total / PAGE_SIZE));
        if (page > maxPage) { setPage(maxPage); commitView({ page: maxPage }, true); }
      }
    } catch (e) {
      if (!current()) return;
      if (e instanceof ApiError && e.status === 401) {
        // token expired mid-session → bounce to login (preserve return path)
        if (typeof window !== "undefined") {
          window.location.replace(appPath(`/login/?next=${encodeURIComponent("/knowledge/")}`));
        }
        return;
      }
      setError(t(kbFileError(e, deep ? "search" : "load")));
      setErrorReplacesList(foreground);
    } finally {
      inFlightRef.current -= 1;
    }
  }, [deep, query, scope, fStatus, fTypes, buildParams, t, fDays, sortKey, sortDir, page, activeSpaceId]);

  // main load on param change (loadSeqRef inside reload guards stale commits)
  useEffect(() => {
    // Wait for the remembered scope, and never request "All" where it is unavailable (the effect above resets it).
    if (scopePrefPending || (scope === "all" && allUnavailable)) return;
    setLoading(true);
    reload({ foreground: true }).finally(() => setLoading(false));
    // activeSpaceId is also a real list dependency: scope=space rows vary by space, and space is hydrated.
    // Without it, switching leaves old-space rows (counts already refreshed, worsening inconsistency).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scope, fStatusKey, fTypesKey, fDays, sortKey, sortDir, page, deep, searchTrigger, activeSpaceId, scopePrefPending, allUnavailable]);

  // scope counts (mount + active-space hydration/switch + after mutations via countsTick)
  const [countsTick, setCountsTick] = useState(0);
  useEffect(() => {
    let cancelled = false;
    // On cold load, active space is not hydrated into space-storage, so `scope=space` lacks X-Space-Id;
    // Gateway correctly returns 400 (kb.py `_require_workspace_context` guard against cross-scope leaks).
    // Previously deps contained only countsTick; catch converted 400 to null and **never retried**, so throughout the visit
    // Visible to space members showed — instead of actual counts (reproduced on 116 production on 2026-07-30),
    // and Document Matrix count badges disappeared too. Depend on activeSpaceId: skip space queries without active space
    // (personal/team still return counts), refetch automatically after hydration and on space switches.
    const ids = activeSpaceId ? SCOPE_IDS : SCOPE_IDS.filter((id) => id !== "space");
    // Also count "All"; its answer tells whether the server supports the cross-scope list
    // (an older server refuses scope=all with 4xx, or answers without next_cursor).
    const allCount: Promise<readonly [KbListScope, number | null]> = fetchKbFiles({ scope: "all", limit: 1 }).then(
          (r) => {
            const supported = supportsAllScopes(r);
            if (!cancelled) setAllScopes(supported);
            return ["all", supported ? r.total : null] as const;
          },
          (e) => {
            if (!cancelled && e instanceof ApiError && e.status >= 400 && e.status < 500) setAllScopes(false);
            return ["all", null] as const;
          },
        );
    Promise.all([
      ...ids.map((id) =>
        fetchKbFiles({ scope: id, limit: 1 }).then((r) => [id, r.total] as const).catch(() => [id, null] as const),
      ),
      allCount,
    ]).then((pairs) => {
      // Merge instead of replacing all: skipped space retains its value (initial null renders — = unknown, not 0).
      if (!cancelled) setScopeCounts((prev) => ({ ...prev, ...Object.fromEntries(pairs) }));
    });
    return () => { cancelled = true; };
  }, [countsTick, activeSpaceId]);

  // auto-poll while any row is parsing (does NOT toggle the loading skeleton)
  useEffect(() => {
    if (deep) return;
    // Stuck files display as failed and no longer warrant polling (FR1 guarantees convergence in finite time).
    const anyParsing = files.some(kbFileSettling);
    if (!anyParsing && !sourceGuard) return;
    const id = window.setInterval(() => { if (document.visibilityState !== "hidden") void reload({ poll: true }); }, anyParsing ? POLL_MS : 30_000);
    return () => window.clearInterval(id);
  }, [files, deep, reload, sourceGuard]);

  // per-row extraction job polling (ported from kb-file-list)
  useEffect(() => {
    const active = files
      .filter((f) => { const s = f.extracted_rows_status; return s === "queued" || s === "running"; })
      .map((f) => f.file_id);
    if (active.length === 0) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const poll = async () => {
      if (cancelled) return;
      try {
        const data = await fetchExtractRowsStatusBatch(active);
        if (cancelled) return;
        // merge (not replace): a file that finished + dropped out of `active`
        // keeps its last job state for the pill instead of being wiped.
        setJobs((prev) => ({ ...prev, ...(data.jobs || {}) }));
        if (Object.values(data.jobs || {}).every((j) => ["succeeded", "failed", "cancelled"].includes(j.status))) return;
      } catch (e) {
        // 4xx (missing space / non-admins cannot view progress, etc.) cannot recover by retrying: stop polling and show
        // the file's own extraction state. Only network errors / 5xx retry on the next tick.
        if (e instanceof ApiError && e.status >= 400 && e.status < 500) return;
      }
      if (!cancelled) timer = setTimeout(poll, POLL_MS);
    };
    void poll();
    return () => { cancelled = true; if (timer) clearTimeout(timer); };
  }, [files.map((f) => `${f.file_id}:${f.extracted_rows_status}`).join("|")]); // eslint-disable-line react-hooks/exhaustive-deps

  // ---- mutators ----
  const resetPage = () => setPage(1);
  const changeScope = (s: KbListScope) => {
    if (s === scope) return;
    setScope(s); commitView({ scope: s, page: 1 }); resetPage(); setSelected(new Set());
    // Remember the choice for the next visit (per account; a failed save only loses the memory).
    void saveKbFileScopePreference(s).catch(() => {});
  };
  const toggleStatus = (v: StatusBucket) => { const n = new Set(fStatus); n.has(v) ? n.delete(v) : n.add(v); setFStatus(n); commitView({ status: [...n], page: 1 }); resetPage(); };
  const toggleType = (v: string) => { const n = new Set(fTypes); n.has(v) ? n.delete(v) : n.add(v); setFTypes(n); commitView({ types: [...n], page: 1 }); resetPage(); };
  const clearStatus = () => { setFStatus(new Set()); commitView({ status: [], page: 1 }); resetPage(); };
  const clearType = () => { setFTypes(new Set()); commitView({ types: [], page: 1 }); resetPage(); };
  // Phone filter sheet: clear only its own groups (status, type, date); keyword, deep search and scope stay.
  const clearSheetFilters = () => { setFStatus(new Set()); setFTypes(new Set()); setFDays(null); commitView({ status: [], types: [], days: null, page: 1 }); resetPage(); };
  const setDays = (v: number | null) => { setFDays(v); commitView({ days: v, page: 1 }); resetPage(); };
  const setSort = (k: SortKey, d: SortDir) => { setSortKey(k); setSortDir(d); commitView({ sort: k, order: d, page: 1 }); resetPage(); };
  const onQuery = (q: string) => {
    setQuery(q);
    if (!deep) {
      if (debounceRef.current) clearTimeout(debounceRef.current);
      debounceRef.current = setTimeout(() => { commitView({ query: q, page: 1 }); setPage(1); setSearchTrigger((n) => n + 1); }, 300);
    }
  };
  const toggleDeep = () => { if (debounceRef.current) clearTimeout(debounceRef.current); commitView({ deep: !deep, page: 1 }); setDeep((d) => !d); setPage(1); setSearchTrigger((n) => n + 1); };
  const submitDeep = () => { if (debounceRef.current) clearTimeout(debounceRef.current); commitView({ query, page: 1 }); setPage(1); setSearchTrigger((n) => n + 1); };
  const clearFilters = () => { if (debounceRef.current) clearTimeout(debounceRef.current); commitView({ status: [], types: [], days: null, query: "", deep: false, page: 1 }); setFStatus(new Set()); setFTypes(new Set()); setFDays(null); setQuery(""); setDeep(false); setPage(1); setSearchTrigger((n) => n + 1); };
  // Two paths after no deep-search hits: change scope (retain keyword / deep search) or turn off deep search and search filenames.
  const clearQuery = () => { if (debounceRef.current) clearTimeout(debounceRef.current); commitView({ query: "", page: 1 }); setQuery(""); setPage(1); setSearchTrigger((n) => n + 1); };
  const searchByName = () => { commitView({ deep: false, page: 1 }); setDeep(false); setPage(1); setSearchTrigger((n) => n + 1); };
  const openUpload = (s: KbScope) => { if (scope !== "all") changeScope(s); setUploadScope(s); };
  const closeDrawer = () => {
    setDrawerFile(null);
    setDrawerHit(null);
    if (openedFromList.current && new URLSearchParams(window.location.search).get("file_id") === drawerFile?.file_id) {
      openedFromList.current = false;
      routerRef.current.back();
    } else clearDeepLinkParams();
  };
  const openFile = (f: KbFileDTO, viaPointer = false) => {
    setDeepLinkFailed(false);
    setDrawerFile(f);
    setDrawerOpenedWithPointer(viaPointer);
    const hit = deep ? hits[f.file_id] : undefined;
    setDrawerHit(hit ? { chunk: hit, query: query.trim() } : null);
    fileTrigger.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    openedFromList.current = true;
    const params = writeKbFileView(window.location.search, viewState);
    params.set("file_id", f.file_id);
    if (hit) params.set("chunk_id", hit.chunk_id); else params.delete("chunk_id");
    if (activeSpaceId) params.set("space", activeSpaceId);
    ownNavigation.current = params.toString();
    routerRef.current.push(`/knowledge/?${params}`, { scroll: false });
  };

  /** User-facing scope name: space uses its name (design 02·A), falling back to "空间" if unavailable. */
  const scopeName = (s: KbListScope) => (s === "all" ? t("scope_filter.all") : s === "space" && spaceName ? spaceName : t(`scope_${s}`));
  const scopeHintFor = (s: KbListScope) => s === "all"
    ? t("scope_filter.hint_all")
    : s === "personal"
      ? t("files_view.hint_personal")
      : s === "space"
        ? t("files_view.hint_space", { name: scopeName("space") })
        : t("files_view.hint_team");
  // Scope filter options: "All" only when the server supports it; space only with a selected space.
  // "All" is also listed while it is the current scope, so a failed probe can never leave the button with another label.
  const scopeOptions: KbScopeFilterOption[] = ([...(allScopes || scope === "all" ? ["all" as const] : []), ...SCOPE_IDS] as KbListScope[])
    .filter((id) => id !== "space" || activeSpaceId || scope === "space")
    .map((id) => ({
      id,
      label: id === "all"
        ? t("scope_filter.all")
        : id === "space"
          ? (spaceName ? t("files_view.space_chip", { name: spaceName }) : t("tab_workspace"))
          : t(id === "personal" ? "tab_personal" : "tab_team"),
      hint: scopeHintFor(id),
      count: scopeCounts[id],
    }));
  /** Short scope name for the scope column and the narrow row meta line (scope "All"). */
  const scopeCell = (f: KbFileDTO) => {
    const s = (f.scope as string) === "workspace" ? "space" : f.scope;
    return s === "personal" ? t("tab_personal") : s === "team" ? t("tab_team") : t("tab_workspace");
  };
  // "在对话中使用" reuses "资料库 › 文件"'s action (new session with file); only files uploaded by the current user in the current space
  // qualify: composer attachment validation accepts only that user's uploads in that space, rejecting other users' files.
  const canUseInChat = (f: KbFileDTO) =>
    kbFileState(f) === "ready" && f.source_health?.state !== "paused" && me != null && f.uploaded_by === me.user_id && f.workspace_id === activeSpaceId;
  const sendToChat = (f: KbFileDTO) =>
    handoffAttachmentsToNewChat([{ fileId: f.artifact_id, filename: f.filename, size: f.size_bytes, mime: f.mime_type }]);
  const drawerScopeLabel = (f: KbFileDTO) => {
    const s = (f.scope as string) === "workspace" ? "space" : f.scope;
    if (s === "personal") return undefined;
    if (s === "space") {
      const name = spaceItems.find((w) => w.id === f.workspace_id)?.name;
      return name ? t("drawer.scope_space_named", { name }) : undefined;
    }
    return undefined;
  };

  const refreshAll = useCallback(() => { setCountsTick((n) => n + 1); return reload(); }, [reload]);
  // Undo Toast callback is created at deletion and runs later; use latest refresh function, otherwise deletion-time
  // scope / filters restore the old scope in the list.
  const refreshAllRef = useRef(refreshAll);
  refreshAllRef.current = refreshAll;

  // Single-file / ZIP completion selects actual upload scope; combine ZIP per-file callbacks.
  const uploadedRef = useRef<KbFileDTO[]>([]);
  const handleUploaded = (dto: KbFileDTO) => {
    uploadedRef.current.push(dto);
    if (uploadedRef.current.length > 1) return;
    queueMicrotask(() => {
      const batch = uploadedRef.current;
      uploadedRef.current = [];
      const targetScope = batch[0].scope;
      // Scope "All" already lists the new files; stay there.
      const nextScope: KbListScope = scopeRef.current === "all" ? "all" : targetScope;
      if (debounceRef.current) clearTimeout(debounceRef.current);
      // Old filters / page may hide newly uploaded files; list effect below refreshes using new conditions.
      setScope(nextScope); setQuery(""); setDeep(false);
      setFStatus(new Set()); setFTypes(new Set()); setFDays(null);
      setSortKey("uploadedAt"); setSortDir("desc"); setPage(1);
      setSelected(new Set()); setSearchTrigger((n) => n + 1);
      setCountsTick((n) => n + 1);
      commitView({ scope: nextScope, query: "", deep: false, status: [], types: [], days: null, sort: "uploadedAt", order: "desc", page: 1 });
      toast("success", batch.length === 1
        ? t("toast.uploaded_one", { name: batch[0].filename })
        : t("toast.uploaded_n", { n: batch.length }));
    });
  };

  // sort via column header
  const headerSort = (k: SortKey) => {
    if (sortKey === k) setSort(k, sortDir === "asc" ? "desc" : "asc");
    else setSort(k, k === "name" ? "asc" : "desc");
  };

  // ---- selection ----
  const toggleSel = (id: string) => setSelected((p) => { const n = new Set(p); n.has(id) ? n.delete(id) : n.add(id); return n; });
  const pageIds = files.map((f) => f.file_id);
  const selOnPage = pageIds.filter((id) => selected.has(id)).length;
  const headState = selOnPage === 0 ? "" : selOnPage === pageIds.length ? "on" : "mixed";
  const toggleSelAll = () => {
    const allSel = pageIds.every((id) => selected.has(id));
    setSelected((p) => { const n = new Set(p); pageIds.forEach((id) => (allSel ? n.delete(id) : n.add(id))); return n; });
  };
  const selectAllMatching = async () => {
    // deep-search results are already fully loaded (no pagination) — select those,
    // NOT a refetch (buildParams drops `q` in deep mode → would select the whole scope).
    if (deep) { setSelected(new Set(files.map((f) => f.file_id))); return; }
    try {
      const res = await fetchKbFiles({ ...buildParams(), cursor: undefined, offset: 0, limit: Math.max(total, 1) });
      selectedMetaRef.current = new Map(res.files.map((f) => [f.file_id, { filename: f.filename, scope: f.scope }]));
      setSelected(new Set(res.files.map((f) => f.file_id)));
    } catch { /* ignore */ }
  };

  // ---- M4 retry ----
  const doReparse = async (f: KbFileDTO) => {
    try {
      await reparseKbFile(f.file_id);
      evictKbChunks(f.file_id);
      const previousError = retryErrorToastIds.current.get(f.file_id);
      if (previousError) dismissToast(previousError);
      retryErrorToastIds.current.delete(f.file_id);
      toast("info", t("toast.requeued_parse", { name: f.filename }));
      void reload();
    } catch (e) {
      const id = pushToast({ kind: "error", text: t("toast.retry_failed", { error: t(kbFileError(e, "reparse")) }) });
      retryErrorToastIds.current.set(f.file_id, id);
    }
  };
  const doExtractRetry = async (f: KbFileDTO) => {
    try {
      await extractRowsKbFile(f.file_id);
      setJobs((p) => ({ ...p, [f.file_id]: { job_id: "pending", status: "queued", total_batches: 0, completed_batches: 0, rows_extracted: 0 } }));
      toast("info", t("toast.requeued_structuring", { name: f.filename }));
    } catch (e) {
      toast("error", t("toast.retry_failed", { error: t(kbFileError(e, "extract")) }));
    }
  };

  // ---- M2 delete (deferred-delete window; no server restore endpoint) ----
  const doDeferredDelete = (ids: string[]) => {
    for (const id of ids) pendingDeleteIdsRef.current.add(id);
    const settle = () => { for (const id of ids) pendingDeleteIdsRef.current.delete(id); };
    setFiles((fs) => fs.filter((f) => !ids.includes(f.file_id)));
    setSelected(new Set());
    closeDrawer();
    // Undo window = Undo Toast lifetime (at least 8 seconds, paused on hover). Delete only when Toast leaves (timeout / manual close),
    // ensuring file exists as long as Undo remains available (I3).
    const commit = async () => {
      try {
        const res = await bulkDeleteKbFiles(ids);
        ids.forEach(evictKbChunks);
        if (res.failed > 0) toast("error", t("toast.deleted_partial", { ok: res.succeeded, failed: res.failed }));
      } catch (e) {
        toast("error", t("toast.delete_failed", { error: t(kbFileError(e, "delete")) }));
      } finally {
        // This refresh restores files whose deletion failed to the list.
        settle();
        void refreshAllRef.current();
      }
    };
    pushToast({
      kind: "success",
      undo: true,
      text: t.rich("toast.deleted_n", { n: ids.length, b: (c) => <b>{c}</b> }),
      action: {
        label: t("toast.undo"),
        onAction: () => {
          settle();
          void refreshAllRef.current();
        },
      },
      onDismiss: () => { void commit(); },
    });
  };

  // ---- scope targets from selection ----
  const targetsFor = (ids: string[]): ScopeTarget[] =>
    ids.map((id) => {
      const f = files.find((x) => x.file_id === id) ?? selectedMetaRef.current.get(id);
      // off-page ids (after "select all N") aren't loaded — fall back to current scope.
      return f
        ? { file_id: id, filename: f.filename, scope: f.scope }
        : { file_id: id, filename: id, scope: uploadDefault };
    });

  // ---- pagination ----
  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const start = total === 0 ? 0 : (page - 1) * PAGE_SIZE + 1;
  const end = Math.min(total, page * PAGE_SIZE);
  // Narrow screens keep one neighbour fewer on each side so the 44px touch buttons stay on one row.
  const pageNums = (() => {
    const out: (number | "…")[] = [];
    const siblings = narrow ? 0 : 1;
    if (pages <= (narrow ? 5 : 7)) { for (let i = 1; i <= pages; i++) out.push(i); return out; }
    out.push(1);
    if (page > siblings + 2) out.push("…");
    for (let i = Math.max(2, page - siblings); i <= Math.min(pages - 1, page + siblings); i++) out.push(i);
    if (page < pages - siblings - 1) out.push("…");
    out.push(pages);
    return out;
  })();

  const scopeEmpty = (scopeCounts[scope] ?? 0) === 0 && !filtersActive;
  const deepActive = deep && query.trim().length > 0;
  // Alternative scope after no deep-search hits: outside space → current space; already in space → personal.
  const altScope: KbScope | null = scope === "all" ? null : scope !== "space" ? (activeSpaceId ? "space" : null) : "personal";

  const emptyState = (() => {
    if (scopeEmpty) {
      if (scope === "all") {
        return (
          <div className="kb-empty" data-testid="kb-empty-all">
            <div className="big">{t("scope_filter.empty_all_title")}</div>
            <div className="sub">{t("scope_filter.empty_all_sub")}</div>
            <div className="kb-empty-acts">
              <button type="button" className="kb-btn kb-btn-primary" onClick={() => openUpload(uploadDefault)}>{t("upload_button")}</button>
            </div>
          </div>
        );
      }
      if (scope === "team") {
        return (
          <div className="kb-empty" data-testid="kb-empty-team">
            <div className="big">{t("files_view.empty_team_title")}</div>
            {me && (
              <>
                <div className="sub">{me.is_team_admin ? t("files_view.empty_team_admin") : t("files_view.empty_team_member")}</div>
                <div className="kb-empty-acts">
                  {me.is_team_admin ? (
                    <button type="button" className="kb-btn kb-btn-primary" onClick={() => openUpload("team")}>{t("files_view.upload_to_team")}</button>
                  ) : (
                    <>
                      <button type="button" className="kb-btn kb-btn-secondary" onClick={() => openUpload("personal")}>{t("files_view.upload_to_personal")}</button>
                      <button type="button" className="kb-btn kb-btn-secondary" disabled={!activeSpaceId} onClick={() => openUpload("space")}>{t("files_view.upload_to_space")}</button>
                    </>
                  )}
                </div>
                {me.is_team_admin && (
                  <div className="explain">
                    <div className="e"><div className="t">{t("scope_personal")}</div><div className="d">{t("files_view.explain_personal")}</div></div>
                    <div className="e"><div className="t">{spaceName ? t("files_view.space_chip", { name: spaceName }) : t("scope_space")}</div><div className="d">{t("files_view.explain_space")}</div></div>
                    <div className="e"><div className="t">{t("scope_team")}</div><div className="d">{t("files_view.explain_team")}</div></div>
                  </div>
                )}
              </>
            )}
          </div>
        );
      }
      const personal = scope === "personal";
      return (
        <div className="kb-empty" data-testid={`kb-empty-${scope}`}>
          <div className="big">{personal ? t("files_view.empty_personal_title") : t("files_view.empty_space_title", { name: scopeName("space") })}</div>
          <div className="sub">{personal ? t("files_view.empty_personal_sub") : t("files_view.empty_space_sub")}</div>
          <div className="kb-empty-acts">
            <button type="button" className="kb-btn kb-btn-primary" disabled={!personal && !activeSpaceId} onClick={() => openUpload(scope)}>
              {t("upload_button")}
            </button>
          </div>
        </div>
      );
    }
    if (deepActive) {
      return (
        <div className="kb-empty" data-testid="kb-empty-deep">
          <div className="big">{t("files_view.deep_empty_title", { scope: scopeName(scope) })}</div>
          <div className="sub">{t("files_view.deep_empty_sub")}</div>
          <div className="kb-empty-acts">
            {altScope && (
              <button type="button" className="kb-btn kb-btn-secondary kb-btn-sm" data-testid="kb-deep-try-scope" onClick={() => changeScope(altScope)}>
                {t("files_view.deep_try_scope", { scope: scopeName(altScope) })}
              </button>
            )}
            <button type="button" className="kb-btn kb-btn-ghost kb-btn-sm" data-testid="kb-deep-by-name" onClick={searchByName}>
              {t("files_view.deep_by_name")}
            </button>
          </div>
        </div>
      );
    }
    return (
      <div className="kb-empty">
        <div className="big">{t("manager.empty.filter_title")}</div>
        <div className="sub">{t("manager.empty.filter_sub")}</div>
        {filtersActive && (
          <div className="kb-empty-acts">
            <button type="button" className="kb-btn kb-btn-secondary kb-btn-sm" onClick={clearFilters}>{t("manager.clear_filters")}</button>
          </div>
        )}
      </div>
    );
  })();

  // A failed load replaces the list area (no empty state, no pager): say what happened and offer a retry
  // that re-runs the current view. Filters stay usable above it.
  const showErrorCard = Boolean(error) && !loading && (errorReplacesList || files.length === 0);
  const errorCard = (
    <div className="kb-loaderr" role="alert" data-testid="kb-error">
      <span className="ic" aria-hidden><IconX /></span>
      <div className="big">{t(deep && query.trim() ? "files_view.search_failed_title" : "files_view.load_failed_title")}</div>
      <div className="sub">{error}</div>
      <div className="kb-empty-acts">
        <button type="button" className="kb-btn kb-btn-secondary kb-btn-sm" data-testid="kb-error-retry" onClick={() => setSearchTrigger((n) => n + 1)}>
          <IconRetry />{t("files_view.load_retry")}
        </button>
      </div>
    </div>
  );

  const skeleton = Array.from({ length: 6 }).map((_, i) => (
    <div key={i} className="kb-skel-row">
      <div className="kb-skel" style={{ width: `${30 + (i * 11) % 50}%` }} />
    </div>
  ));

  return (
    <>
      {/* P2 batch 5: the scope is the toolbar scope filter (KbFilesControls scopeFilter). */}
      <KbFilesControls
        query={query}
        deep={deep}
        fStatus={fStatus}
        fTypes={fTypes}
        fDays={fDays}
        sortKey={sortKey}
        sortDir={sortDir}
        narrow={narrow}
        deepSummary={deepActive && !loading && !error && files.length > 0
          ? t("files_view.deep_count", { scope: scopeName(scope), n: files.length })
          : null}
        onQuery={onQuery}
        onClearQuery={clearQuery}
        onToggleDeep={toggleDeep}
        onSubmitDeep={submitDeep}
        onToggleStatus={toggleStatus}
        onToggleType={toggleType}
        onSetDays={setDays}
        onSetSort={setSort}
        onClear={clearFilters}
        onUpload={uploadRequest === undefined ? () => setUploadScope(uploadDefault) : undefined}
        scopeFilter={<KbScopeFilter scope={scope} options={scopeOptions} onSelect={changeScope} />}
        scopeSheet={{ scope, options: scopeOptions, onSelect: changeScope }}
        resultCount={loading || error ? null : total}
        onClearStatus={clearStatus}
        onClearType={clearType}
        onClearSheet={clearSheetFilters}
      />

      {me && <KbRebuildBatch key={`${me.team_id}:${me.user_id}:${me.workspace_id}`} scopeKey={`${me.team_id}:${me.user_id}:${me.workspace_id}`} selected={[...selected]} canManage={[...selected].every(id => files.find(f => f.file_id === id)?.can_delete === true)} onChanged={refreshAll} />}
      {/* Bulk bar (no multi-select / bulk actions on narrow screens). */}
      {!narrow && selected.size > 0 && (
        <div className="kb-bulkbar" data-testid="kb-bulk-bar">
          <span className="lead">{t.rich("manager.bulk.selected", { n: selected.size, b: (c) => <b>{c}</b> })}</span>
          {selected.size < total && (
            <button type="button" className="selall" onClick={selectAllMatching}>{t("manager.bulk.select_all", { n: total })}</button>
          )}
          <div style={{ flex: 1 }} />
          {/* kb-matrix M2 (PRD §7 flow 1): multi-select → prefill file_ids in new-matrix flow, skipping file selection. */}
          <button
            type="button"
            className="kb-btn kb-btn-primary kb-btn-sm"
            data-testid="kb-open-as-matrix"
            onClick={() => {
              try {
                sessionStorage.setItem("kbMatrixNewFiles", JSON.stringify([...selected]));
              } catch {
                /* sessionStorage unavailable → fall back to manual file selection. */
              }
              router.push("/knowledge?tab=matrix&new=1");
            }}
          >
            <IconGridSmall /> {t("manager.bulk.open_as_matrix")}
          </button>
          <button type="button" className="kb-btn kb-btn-secondary kb-btn-sm" onClick={() => setDocTypeIds([...selected])}><IconTag /> {t("manager.bulk.bind_doctype")}</button>
          <button type="button" className="kb-btn kb-btn-secondary kb-btn-sm" onClick={() => setScopeModal({ targets: targetsFor([...selected]), bulk: true })}><IconFolder /> {t("manager.bulk.change_scope")}</button>
          <button type="button" className="kb-btn kb-btn-danger kb-btn-sm" onClick={() => setDeleteIds([...selected])}><IconTrash /> {t("manager.bulk.delete")}</button>
          <span className="vsep" />
          <button type="button" className="kb-btn kb-btn-ghost kb-btn-sm" onClick={() => setSelected(new Set())}>{t("manager.bulk.clear")}</button>
        </div>
      )}

      {deepActive && !loading && !error && (searchIncomplete || searchWarning) && <p className="kb-notice" role="status">{t(searchIncomplete ? "health.search_partial" : "health.use_warning")}</p>}
      {deepLinkFailed && (
        <div role="alert" className="kb-notice err kb-deeplink-notice" data-testid="kb-deeplink-failed">
          <span>{t("files_view.deep_link_failed")}</span>
          <button type="button" className="kb-x" onClick={() => setDeepLinkFailed(false)} aria-label={t("files_view.deep_link_dismiss")}>
            <IconX />
          </button>
        </div>
      )}

      {error && !loading && !showErrorCard && (
        <p className="kb-notice err" role="status" data-testid="kb-refresh-error">{t("files_view.refresh_failed", { error })}</p>
      )}
      {showErrorCard ? errorCard : narrow ? (
        // Narrow two-row list: filename + size · time, shortest status label at right (design 06).
        <div className="mlist" data-testid="kb-files-list">
          {loading ? skeleton : files.length === 0 ? emptyState : files.map((f) => {
            const tm = typeMeta(f.filename);
            return (
              <button
                key={f.file_id}
                type="button"
                className={`mrow${deep && hits[f.file_id] ? " hit" : ""}`}
                data-testid="kb-files-row"
                data-file-id={f.file_id}
                // detail > 0: a mouse click or tap (keyboard activation reports 0).
                onClick={(e) => openFile(f, e.detail > 0)}
              >
                <span className="kb-ftype" aria-hidden style={{ background: tm.color }}>{tm.tag}</span>
                <span className="mbody">
                  <span className="nm">{f.filename}</span>
                  <span className="mt">{showScopeCol ? `${scopeCell(f)} · ` : null}{formatBytes(f.size_bytes)} · {fmtAgo(f.uploaded_at, t)}</span>
                </span>
                <span className="st">
                  <KbStatusPill
                    sourceHealth={f.source_health}
                    state={pillStateFor(f, jobs[f.file_id]?.status)}
                    pct={kbFileState(f) === "processing" ? f.parse_progress_pct : null}
                    short
                  />
                </span>
              </button>
            );
          })}
        </div>
      ) : (
        <div className="kb-tablewrap" data-testid="kb-files-table">
          <div className="kb-grid kb-th-row" style={{ gridTemplateColumns: cols }}>
            <div className="kb-th">
              <span className={`kb-cb ${headState}`} onClick={toggleSelAll} role="checkbox" aria-label={t("files_view.select_page")} tabIndex={0} onKeyDown={(e) => { if (e.key === " ") { e.preventDefault(); toggleSelAll(); } }} aria-checked={headState === "mixed" ? "mixed" : headState === "on"}>
                {headState === "mixed" ? <span className="kb-cb-mixed" /> : <IconCheck />}
              </span>
            </div>
            <div className="kb-th" />
            <SortableTh label={t("col_filename")} k="name" sortKey={sortKey} sortDir={sortDir} onSort={headerSort} />
            {showScopeCol && <div className="kb-th" data-testid="kb-col-scope">{t("col_scope")}</div>}
            {!hideTypeCol && <div className="kb-th" data-testid="kb-col-type">{t("manager.col_type")}</div>}
            <SortableTh label={t("col_size")} k="sizeMB" sortKey={sortKey} sortDir={sortDir} onSort={headerSort} />
            <SortableTh label={t("col_uploaded_at")} k="uploadedAt" sortKey={sortKey} sortDir={sortDir} onSort={headerSort} />
            <div className="kb-th kb-th-end">{t("col_status")}</div>
          </div>

          {loading ? skeleton : files.length === 0 ? emptyState : (
            files.map((f) => (
              <Row
                key={f.file_id}
                t={t}
                f={f}
                cols={cols}
                showType={!hideTypeCol}
                scopeLabel={showScopeCol ? scopeCell(f) : undefined}
                scopeTitle={showScopeCol ? drawerScopeLabel(f) : undefined}
                selected={selected.has(f.file_id)}
                hit={deep ? hits[f.file_id] : undefined}
                query={deep ? query.trim() : ""}
                job={jobs[f.file_id]}
                menuOpen={menuFor === f.file_id}
                onToggleSel={() => toggleSel(f.file_id)}
                onOpen={(viaPointer) => openFile(f, viaPointer)}
                onMenuOpenChange={(open) => setMenuFor((m) => (open ? f.file_id : m === f.file_id ? null : m))}
                onReparse={() => doReparse(f)}
                onExtractRetry={() => doExtractRetry(f)}
                onBindDocType={() => { setMenuFor(null); setDocTypeIds([f.file_id]); }}
                onChangeScope={() => { setMenuFor(null); setScopeModal({ targets: targetsFor([f.file_id]), bulk: false }); }}
                onDelete={() => { setMenuFor(null); setDeleteIds([f.file_id]); }}
              />
            ))
          )}
        </div>
      )}

      {/* pagination: only when there is more than one page; never on error or empty states */}
      {!deep && !showErrorCard && pages > 1 && (loading || files.length > 0) && (
        <div className="kb-pagination">
          {/* Hide counts while loading: total starts at 0, so a skeleton with "共 0 个文件" would imply no files (FR4.2). */}
          <div>{loading ? null : t.rich(narrow ? "manager.pagination.showing_short" : "manager.pagination.showing", { start, end, total, b: (c) => <b className="kb-pagination-num">{c}</b> })}</div>
          <div style={{ flex: 1 }} />
          {/* One group so the buttons wrap (if ever) as a whole, never one by one. Narrow screens show arrow-only prev/next. */}
          <div className="kb-pagenav">
            <button type="button" className="kb-pagebtn" disabled={loading || page === 1} aria-label={narrow ? t("manager.pagination.prev") : undefined} onClick={() => { commitView({ page: Math.max(1, page - 1) }); setPage((p) => Math.max(1, p - 1)); }}>{narrow ? <CharIcon ch="←" aria-hidden /> : <CharIconText ch="←">{t("manager.pagination.prev")}</CharIconText>}</button>
            {!loading && pageNums.map((p, i) => p === "…"
              ? <span key={`d${i}`} className="kb-pagedots">…</span>
              : <button key={p} type="button" className={`kb-pagebtn ${p === page ? "active" : ""}`} onClick={() => { commitView({ page: p }); setPage(p); }}>{p}</button>)}
            <button type="button" className="kb-pagebtn" disabled={loading || page >= pages} aria-label={narrow ? t("manager.pagination.next") : undefined} onClick={() => { commitView({ page: Math.min(pages, page + 1) }); setPage((p) => Math.min(pages, p + 1)); }}>{narrow ? <CharIcon ch="→" aria-hidden /> : <CharIconText ch="→" at="end">{t("manager.pagination.next")}</CharIconText>}</button>
          </div>
        </div>
      )}

      {/* modals + drawer + toasts */}
      {docTypeIds && me && (
        <KbDocTypeModal
          fileIds={docTypeIds}
          workspaceId={me.workspace_id}
          onClose={() => setDocTypeIds(null)}
          onApplied={() => { setDocTypeIds(null); void refreshAll(); }}
          toast={toast}
        />
      )}
      {scopeModal && me && (
        <KbScopeModal
          targets={scopeModal.targets}
          bulk={scopeModal.bulk}
          me={me}
          onClose={() => setScopeModal(null)}
          onMigrated={(failedIds) => {
            // Chat source-card original-text cache is per file; refetch with new permissions after scope changes.
            scopeModal.targets.forEach((x) => evictKbChunks(x.file_id));
            setScopeModal(null);
            setSelected(new Set(failedIds));
            void refreshAll();
          }}
          toast={toast}
        />
      )}
      {deleteIds && (
        <DialogShell
          onClose={() => setDeleteIds(null)}
          label={t("manager.delete_modal.title", { n: deleteIds.length })}
          backdropClassName="kb-overlay"
          panelClassName="kb-modal"
        >
          <div className="kb-modal-head">
            <h3>{t("manager.delete_modal.title", { n: deleteIds.length })}</h3>
            <div className="sub">{t("manager.delete_modal.sub")}</div>
          </div>
          <div className="kb-modal-body">
            <div className="kb-notice err">{t("manager.delete_modal.notice")}</div>
          </div>
          <div className="kb-modal-foot">
            <button type="button" className="kb-btn kb-btn-ghost" onClick={() => setDeleteIds(null)}>{t("manager.cancel")}</button>
            <button type="button" className="kb-btn kb-btn-danger-solid" onClick={() => { const ids = deleteIds; setDeleteIds(null); doDeferredDelete(ids); }}><IconTrash /> {t("manager.delete_modal.confirm", { n: deleteIds.length })}</button>
          </div>
        </DialogShell>
      )}
      {uploadScope && (
        <KbUploadModal
          defaultScope={uploadScope}
          onClose={() => setUploadScope(null)}
          onUploaded={(dto) => { setUploadScope(null); handleUploaded(dto); }}
        />
      )}
      {drawerLive && (
        <KbFileDrawer
          file={drawerLive}
          openedWithPointer={drawerOpenedWithPointer}
          scopeLabel={drawerScopeLabel(drawerLive)}
          hitChunk={drawerHit?.chunk ?? null}
          hitQuery={drawerHit?.query ?? ""}
          hitSource={drawerHit?.source ?? "search"}
          onClose={closeDrawer}
          onRetry={drawerLive.can_delete && kbFailRetryable(kbFailReason(drawerLive)) ? () => void doReparse(drawerLive) : undefined}
          onUseInChat={canUseInChat(drawerLive) ? () => sendToChat(drawerLive) : undefined}
          // Scope-change / template-binding dialogs share drawer layer; close drawer before opening to avoid dialogs beneath it.
          onChangeScope={drawerLive.can_change_scope && me
            ? () => { const f = drawerLive; closeDrawer(); setScopeModal({ targets: targetsFor([f.file_id]), bulk: false }); }
            : undefined}
          onBindTemplate={me ? () => { const f = drawerLive; closeDrawer(); setDocTypeIds([f.file_id]); } : undefined}
          onDeleted={() => {
            const gone = drawerLive;
            evictKbChunks(gone.file_id);
            closeDrawer();
            setFiles((fs) => fs.filter((f) => f.file_id !== gone.file_id));
            toast("success", t("toast.deleted_one", { name: gone.filename }));
            void refreshAll();
          }}
        />
      )}
    </>
  );
}

function SortableTh({ label, k, sortKey, sortDir, onSort }: {
  label: string; k: SortKey; sortKey: SortKey; sortDir: SortDir; onSort: (k: SortKey) => void;
}) {
  const sorted = sortKey === k;
  return (
    <div className={`kb-th sortable ${sorted ? "sorted" : ""}`} role="button" tabIndex={0} onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onSort(k); } }} onClick={() => onSort(k)}>
      {label} <span className="sortarrow">{sortDir === "asc" ? <IconArrowUp /> : <IconArrowDn />}</span>
    </div>
  );
}

function pillStateFor(f: KbFileDTO, jobStatus?: string): ExtractionState {
  // Parsing uses unified user-facing states (PRD §4); enter structured-extraction state machine only after availability.
  const fileState = kbFileState(f);
  if (fileState === "failed") return "parse_failed";
  if (fileState === "uploading") return "uploading";
  if (fileState === "queued") return "queued";
  if (fileState === "processing") return "parsing";
  return getExtractionState({
    file: f, hasRecord: false, recordVerifyStatus: null,
    extractedRowsStatus: jobStatus ?? f.extracted_rows_status,
  });
}

function Row(props: {
  t: Translator;
  f: KbFileDTO;
  cols: string;
  showType: boolean;
  /** Scope column cell (scope "All"); absent = no column. */
  scopeLabel?: string;
  scopeTitle?: string;
  selected: boolean;
  hit?: KbSearchResultChunk;
  query: string;
  job?: PerRowJobStatus;
  menuOpen: boolean;
  onToggleSel: () => void;
  onOpen: (viaPointer?: boolean) => void;
  onMenuOpenChange: (open: boolean) => void;
  onReparse: () => void;
  onExtractRetry: () => void;
  onBindDocType: () => void;
  onChangeScope: () => void;
  onDelete: () => void;
}) {
  const { t, f, selected, hit, query, job } = props;
  const locale = useLocale();
  const tm = typeMeta(f.filename);
  const state = pillStateFor(f, job?.status);
  const docHref = f.doc_type_id
    ? appPath(`/knowledge/?tab=records&view=tpl_${encodeURIComponent(f.doc_type_id)}`)
    : f.auto_classified_doc_type_id
      ? appPath(`/knowledge/?tab=records&view=tpl_${encodeURIComponent(f.auto_classified_doc_type_id)}`)
      : undefined;

  const failReason = kbFailReason(f);
  const pill = state === "parse_failed"
      ? <KbStatusPill state="parse_failed" onRetry={f.can_delete && kbFailRetryable(failReason) ? props.onReparse : undefined} retryName={f.filename} title={failReason ? t(`fail_reason.${failReason}`) : undefined} />
      : state === "extracting_failed"
        ? <KbStatusPill state="extracting_failed" onRetry={props.onExtractRetry} retryName={f.filename} />
        : <KbStatusPill
            sourceHealth={f.source_health}
            state={state}
            href={docHref}
            pct={state === "parsing" ? f.parse_progress_pct : null}
            jobProgress={job ? { completed_batches: job.completed_batches, total_batches: job.total_batches, rows_extracted: job.rows_extracted } : null}
          />;
  const loc = hit ? kbChunkLocation(t, hit) : null;

  return (
    <div
      className={`kb-grid kb-row ${selected ? "sel" : ""} ${hit ? "hit" : ""}`}
      style={{ gridTemplateColumns: props.cols }}
      data-testid="kb-files-row"
      data-file-id={f.file_id}
      onClick={(e) => { e.currentTarget.querySelector<HTMLButtonElement>(".kb-file-open")?.focus(); props.onOpen(e.detail > 0); }}
    >
      <div className="kb-cell">
        <span className={`kb-cb ${selected ? "on" : ""}`} onClick={(e) => { e.stopPropagation(); props.onToggleSel(); }} role="checkbox" aria-label={t("files_view.select_file", { name: f.filename })} tabIndex={0} onKeyDown={(e) => { if (e.key === " ") { e.preventDefault(); e.stopPropagation(); props.onToggleSel(); } }} aria-checked={selected}>
          <IconCheck />
        </span>
      </div>
      <div className="kb-cell"><span className="kb-ftype" aria-hidden style={{ background: tm.color }}>{tm.tag}</span></div>
      <div className="kb-cell">
        <div className="kb-fname"><button type="button" className="ftext kb-file-open" title={f.filename} onClick={(e) => { e.stopPropagation(); e.currentTarget.focus(); props.onOpen(e.detail > 0); }}>{kbHighlight(f.filename, query)}</button></div>
        {hit && (
          <div className="kb-snippet">
            {loc && <span className="loc" data-testid="kb-hit-loc">{loc}</span>}
            …{snippetText(hit.content, query)}…
          </div>
        )}
      </div>
      {props.scopeLabel !== undefined && <div className="kb-cell kb-scope-cell" title={props.scopeTitle}>{props.scopeLabel}</div>}
      {props.showType && <div className="kb-cell kb-num">{tm.label}</div>}
      <div className="kb-cell kb-num">{formatBytes(f.size_bytes)}</div>
      <div className="kb-cell kb-num kb-date-cell" title={formatShortDateTime(f.uploaded_at, locale)}>{fmtAgo(f.uploaded_at, t)}</div>
      <div className="kb-cell kb-status-cell">
        <span className="kb-status-slot" onClick={(e) => e.stopPropagation()}>{pill}</span>
        <KbRowMenu
          t={t}
          f={f}
          open={props.menuOpen}
          onOpenChange={props.onMenuOpenChange}
          onOpen={() => props.onOpen()}
          onBindDocType={props.onBindDocType}
          onChangeScope={props.onChangeScope}
          onReparse={props.onReparse}
          onDelete={props.onDelete}
        />
      </div>
    </div>
  );
}

/**
 * File-row ⋯ menu. Mount to body (useMenu / MenuFloating, design-document section 图层): previous inline
 * absolute overlays in final rows were clipped by `.kb-tablewrap { overflow: hidden }` (real browser showed only an edge),
 * inside hover-only `.kb-rowactions` (opacity 0), making open menus invisible after pointer exit / keyboard opening.
 * useMenu handles keyboard, Esc, outside clicks and focus restoration.
 */
function KbRowMenu({
  t, f, open, onOpenChange, onOpen, onBindDocType, onChangeScope, onReparse, onDelete,
}: {
  t: Translator;
  f: KbFileDTO;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onOpen: () => void;
  onBindDocType: () => void;
  onChangeScope: () => void;
  onReparse: () => void;
  onDelete: () => void;
}) {
  // Keep closeOnScroll off (see 6528df29f); close when the entire row scrolls outside list visibility, avoiding viewport-edge menu clamping.
  const menu = useMenu({ open, onOpenChange, placement: "bottom-end", offset: 4, closeWhenAnchorHidden: true });
  const canReparse = f.can_delete && kbFileState(f) === "failed" && kbFailRetryable(kbFailReason(f));
  return (
    <div className={`kb-rowactions${open ? " is-open" : ""}`}>
      <button
        type="button"
        className="kb-iconbtn"
        ref={menu.refs.setReference}
        aria-label={t("manager.row_menu.more")}
        {...menu.getReferenceProps({ onClick: (e) => e.stopPropagation() })}
      >
        <IconDots />
      </button>
      <MenuFloating menu={menu}>
        <div
          ref={menu.refs.setFloating}
          {...menu.getFloatingProps({
            className: "kb-pop kb-rowmenu",
            "aria-label": t("manager.row_menu.more"),
            // Portal clicks still bubble along React tree to row onClick (opens details); stop them here.
            onClick: (e) => e.stopPropagation(),
          })}
        >
          <KbRowMenuItem onSelect={onOpen} icon={<IconFolder />} label={t("manager.row_menu.details")} />
          <KbRowMenuItem onSelect={onBindDocType} icon={<IconTag />} label={t("manager.row_menu.bind_doctype")} />
          <KbRowMenuItem
            onSelect={onChangeScope}
            disabled={!f.can_change_scope}
            reason={t(`manager.row_menu.change_scope_reason${kbScopeReasonSuffix(f.scope)}`)}
            icon={<IconFolder />}
            label={t("manager.row_menu.change_scope")}
          />
          {canReparse && (
            // Behavior batch A: reparse executes directly without deleting data; no red warning (only data-deleting items are dangerous).
            <KbRowMenuItem
              onSelect={onReparse}
              icon={<IconRetry />}
              label={t("manager.row_menu.reparse")}
            />
          )}
          <div className="kb-pop-sep" role="separator" />
          {/* I7/⑫: gate delete by can_delete — matches change_scope above + the
              drawer (which hides delete when !can_delete). Deletion confirms first; copy includes …. */}
          <KbRowMenuItem
            onSelect={onDelete}
            disabled={!f.can_delete}
            reason={t(`manager.row_menu.delete_reason${kbScopeReasonSuffix(f.scope)}`)}
            danger={f.can_delete}
            icon={<IconTrash />}
            label={f.can_delete ? withConfirmEllipsis(t("manager.row_menu.delete")) : t("manager.row_menu.delete")}
          />
        </div>
      </MenuFloating>
    </div>
  );
}

/**
 * Accurate disabled reasons by file scope (matches bowhead_core/kb/permission.py can_delete: personal requires owner,
 * space requires uploader / admin, team requires team admin; scope changes and deletion share the same condition).
 */
function kbScopeReasonSuffix(scope: string): "" | "_team" | "_personal" {
  if (scope === "team") return "_team";
  if (scope === "personal") return "_personal";
  return "";
}

function KbRowMenuItem({
  onSelect, disabled = false, reason, danger = false, icon, label,
}: {
  onSelect: () => void;
  disabled?: boolean;
  /** Disabled reason: second line visible and announced through aria-describedby (behavior batch A). */
  reason?: string;
  danger?: boolean;
  icon: React.ReactNode;
  label: string;
}) {
  const props = useMenuItem({ onSelect, disabled, label });
  const reasonId = useId();
  const shownReason = disabled && reason ? reason : null;
  return (
    <>
      <div
        className={`kb-opt${danger ? " danger" : ""}${disabled ? " disabled" : ""}`}
        aria-describedby={shownReason ? reasonId : undefined}
        {...props}
      >
        {icon}<span>{label}</span>
      </div>
      {shownReason ? <MenuItemReason id={reasonId}>{shownReason}</MenuItemReason> : null}
    </>
  );
}

function snippetText(body: string, q: string) {
  const w = snippetWindow(body, q);
  return (<>{w.before}{w.match && <mark>{w.match}</mark>}{w.after}</>);
}
