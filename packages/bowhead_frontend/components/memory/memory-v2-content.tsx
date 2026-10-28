"use client";

import {
  type FormEvent,
  type RefObject,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { useLocale, useTranslations } from "next-intl";

import {
  ArchiveBox,
  Brain,
  Check,
  Database,
  Edit,
  RefreshCw,
  Search,
  Shield,
  Sparkles,
  Trash,
  UserIcon,
  XSm,
} from "@/components/icons";
import { useCopyDiagnosticsToastAction } from "@/components/error-notice";
import { MemoryActionConfirmDialog } from "@/components/memory/memory-action-confirm-dialog";
import {
  approveCandidate,
  beginAboutYouEditSession,
  createMemoryIdempotencyKey,
  deleteAboutYou,
  deleteEntry,
  exportMemoryUrl,
  fetchCanonicalMemoryItems,
  fetchMemoryOverview,
  listMemoryResets,
  MemoryAPIError,
  pauseDreaming,
  patchAboutYou,
  patchCanonicalMemoryItem,
  permanentDeleteAllMemory,
  permanentDeleteMemoryItem,
  permanentDeleteMemoryReset,
  bulkDecideCandidates,
  permanentlyClearMemoryTrash,
  previewMemoryReset,
  rejectCandidate,
  releaseAboutYouEditSession,
  resetMemory,
  restoreEntry,
  restoreMemoryReset,
  resumeDreaming,
  type CanonicalMemoryItem,
  type MemoryAboutYouV2,
  type MemoryOverviewV2,
  type MemoryResetCounts,
  type MemoryResetSummary,
  type PendingCandidate,
} from "@/lib/memory-api";
import { countChars } from "@/lib/notes-markdown";
import { ActiveFilterChips } from "@/components/ui/active-filter-chips";
import { DialogShell } from "@/components/ui/dialog";
import { LoadingSkeleton } from "@/components/ui/loading-skeleton";
import { RowActionsMenu } from "@/components/ui/row-actions-menu";
import { Switch } from "@/components/ui/switch";
import { Segment, SegmentedControl } from "@/components/ui/segmented-control";
import { MEMORY_FOCUS_PARAM } from "@/lib/legacy-app-links";
import { _gateRefusal } from "@/lib/use-memory-candidates";
import { notify, toast } from "@/lib/use-toast";
import { useMemoryScopeLabel, withoutMemoryIds } from "@/lib/memory-display";

type ItemState = "active" | "trashed";
type ActionConfirmTarget =
  | { kind: "about-you" }
  | { kind: "suggestion"; candidate: PendingCandidate };
type PermanentDeleteTarget = (
  | { kind: "item"; item: CanonicalMemoryItem }
  | { kind: "trash"; count: number }
  | { kind: "reset"; snapshot: MemoryResetSummary }
  | { kind: "all"; count: number }
) & { idempotencyKey: string };

/** Deep-linked memories beyond this many extra pages are reported as unavailable. */
const MAX_FOCUS_PAGES = 10;
const FOCUS_HIGHLIGHT_MS = 2600;

function memoryItemElementId(id: string): string {
  return `memory-item-${id}`;
}

const DISPLAY_TYPES = [
  "preference",
  "work_rule",
  "background",
  "data_binding",
] as const;

/**
 * User-facing sentence for memory API failure. Backend detail is developer-oriented English (e.g.
 * "entry not found or locked"), never show verbatim; explain what happened/next action by status code.
 */
function memoryErrorText(error: unknown, t: (key: string, values?: Record<string, string | number>) => string): string {
  if (error instanceof MemoryAPIError) {
    if (error.status === 404) return t("error_not_found");
    if (error.status === 401 || error.status === 403) return t("error_forbidden");
    if (error.status === 409) return t("error_conflict");
    if (error.status === 429) return t("error_rate_limited");
    if (error.status >= 500) return t("error_server");
    return t("error_generic");
  }
  return t("error_network");
}

export function MemoryV2Content() {
  const t = useTranslations("memory_page.v2");
  const scopeLabel = useMemoryScopeLabel();
  const tCard = useTranslations("memory_card");
  const locale = useLocale();
  const [overview, setOverview] = useState<MemoryOverviewV2 | null>(null);
  const [items, setItems] = useState<CanonicalMemoryItem[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [initialError, setInitialError] = useState<string | null>(null);
  const [refreshWarning, setRefreshWarning] = useState<string | null>(null);
  const [acting, setActing] = useState<string | null>(null);
  const [editingItem, setEditingItem] = useState<CanonicalMemoryItem | null>(
    null,
  );
  const [editingAboutYou, setEditingAboutYou] = useState(false);
  const [resetPreview, setResetPreview] = useState<MemoryResetCounts | null>(
    null,
  );
  const [recentResets, setRecentResets] = useState<MemoryResetSummary[]>([]);
  const [resetting, setResetting] = useState(false);
  const [restoringReset, setRestoringReset] = useState<string | null>(null);
  const [permanentDeleteTarget, setPermanentDeleteTarget] =
    useState<PermanentDeleteTarget | null>(null);
  const [permanentlyDeleting, setPermanentlyDeleting] = useState(false);
  const [actionConfirmTarget, setActionConfirmTarget] =
    useState<ActionConfirmTarget | null>(null);
  // ★ Batch 1c: “查看全部” overlay removed; expansion **in place**, same list grows downward,
  // same item styling, no second fetch. Previously page showed three, drawer independently fetched 50,
  // potentially inconsistent lists on same page.
  const [suggestionsExpanded, setSuggestionsExpanded] = useState(false);
  const [selectionMode, setSelectionMode] = useState(false);
  const [selectedIds, setSelectedIds] = useState<ReadonlySet<string>>(
    () => new Set<string>(),
  );
  const [bulkIgnoreConfirm, setBulkIgnoreConfirm] = useState<string[] | null>(
    null,
  );
  const [bulkBusy, setBulkBusy] = useState(false);
  const [itemState, setItemState] = useState<ItemState>("active");
  const [queryInput, setQueryInput] = useState("");
  const [query, setQuery] = useState("");
  const [displayType, setDisplayType] = useState<string | null>(null);
  const [scopeType, setScopeType] = useState<"global" | "space" | null>(null);
  const requestGeneration = useRef(0);
  const actionConfirmReturnFocusRef = useRef<HTMLElement | null>(null);
  const aboutYouReturnFocusRef = useRef<HTMLElement | null>(null);
  const editItemReturnFocusRef = useRef<HTMLElement | null>(null);
  const permanentDeleteReturnFocusRef = useRef<HTMLElement | null>(null);
  const resetReturnFocusRef = useRef<HTMLElement | null>(null);
  const resetIdempotencyKey = useRef<string | null>(null);
  const restoreResetIdempotencyKeys = useRef(new Map<string, string>());
  const [focusItemId, setFocusItemId] = useState<string | null>(null);
  const [highlightedItemId, setHighlightedItemId] = useState<string | null>(null);
  const focusPending = useRef(true);
  const focusPagesFetched = useRef(0);

  const pushToast = notify;
  const copyDiagnosticsAction = useCopyDiagnosticsToastAction();
  // Failure hint: localized sentence + “复制诊断” action (status/request ID only in copied content).
  const pushErrorToast = (error: unknown, text?: string) =>
    toast({ kind: "error", text: text ?? memoryErrorText(error, t), action: copyDiagnosticsAction(error, "settings") });

  const load = useCallback(
    async ({ background = false }: { background?: boolean } = {}) => {
      const generation = ++requestGeneration.current;
      setLoadingMore(false);
      if (background) setRefreshing(true);
      else setLoading(true);
      if (!background) setInitialError(null);
      try {
        const [nextOverview, page, resetHistory] = await Promise.all([
          fetchMemoryOverview(),
          fetchCanonicalMemoryItems({
            state: itemState,
            query,
            sourceLabelKeys: memorySourceKeysMatching((key) => t(key as never), query),
            displayType,
            scopeType,
          }),
          listMemoryResets(),
        ]);
        if (generation !== requestGeneration.current) return;
        setOverview(nextOverview);
        setItems(page.items);
        setNextCursor(page.next_cursor);
        setRecentResets(resetHistory.items);
        setRefreshWarning(null);
      } catch (error) {
        if (generation !== requestGeneration.current) return;
        const message = memoryErrorText(error, t);
        setInitialError(message);
        setRefreshWarning(message);
      } finally {
        if (generation === requestGeneration.current) {
          setLoading(false);
          setRefreshing(false);
        }
      }
    },
    // `t` changes with the interface language, which changes which source lines the query matches.
    [displayType, itemState, query, scopeType, t],
  );

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    const onFocus = () => void load({ background: true });
    window.addEventListener("focus", onFocus);
    return () => window.removeEventListener("focus", onFocus);
  }, [load]);

  const submitSearch = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const nextQuery = queryInput.trim();
    if (nextQuery === query) {
      void load();
      return;
    }
    setLoading(true);
    setQuery(nextQuery);
  };

  const clearFilters = () => {
    setLoading(true);
    setQueryInput("");
    setQuery("");
    setDisplayType(null);
    setScopeType(null);
  };

  const reloadAfterMutation = useCallback(async () => {
    await load({ background: true });
  }, [load]);
  // Undo Toast action fixed at creation, executes up to eight seconds later: refresh using then-current filters/view from latest ref.
  const reloadAfterMutationRef = useRef(reloadAfterMutation);
  reloadAfterMutationRef.current = reloadAfterMutation;

  const toggleLearning = async () => {
    if (!overview) return;
    setActing("learning");
    try {
      const paused = overview.learning.automatic_learning === "paused";
      const result = paused ? await resumeDreaming() : await pauseDreaming();
      setOverview((current) =>
        current
          ? {
              ...current,
              learning: {
                automatic_learning: result.paused ? "paused" : "active",
              },
            }
          : current,
      );
      pushToast(
        "info",
        result.paused ? t("learning_paused") : t("learning_resumed"),
      );
    } catch (error) {
      pushErrorToast(error);
    } finally {
      setActing(null);
    }
  };

  const COLLAPSED_SUGGESTIONS = 3;

  const toggleSelected = (cid: string) => {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(cid)) next.delete(cid);
      else next.add(cid);
      return next;
    });
  };

  const leaveSelection = () => {
    setSelectionMode(false);
    setSelectedIds(new Set<string>());
  };

  const runBulk = async (ids: string[], action: "approve" | "reject") => {
    if (ids.length === 0) return;
    setBulkBusy(true);
    try {
      const r = await bulkDecideCandidates(ids, action);
      // ★ Report per-item success/failure, never swallow: bulk endpoint allows partial failure (target space inaccessible,
      // already decided elsewhere…). Merely saying “已处理 N 条” while actually processing fewer
      // makes user think inbox cleared, interpreting later leftovers as new arrivals.
      if (r.failed > 0) {
        pushToast(
          "error",
          t("suggestions_bulk_partial", { ok: r.succeeded, failed: r.failed }),
        );
      } else {
        pushToast("info", t("suggestions_bulk_done", { n: r.succeeded }));
      }
      leaveSelection();
      await reloadAfterMutation();
    } catch (error) {
      pushErrorToast(error);
    } finally {
      setBulkBusy(false);
    }
  };

  const decideSuggestion = async (
    candidate: PendingCandidate,
    action: "approve" | "reject",
  ): Promise<boolean> => {
    if (action === "approve" && !candidate.default_target) {
      pushToast("error", t("suggestion_target_unavailable"));
      return false;
    }

    setActing(candidate.id);
    try {
      if (action === "approve") {
        await approveCandidate(
          candidate.id,
          undefined,
          candidate.default_target,
        );
        pushToast("info", t("suggestion_saved"));
      } else {
        await rejectCandidate(candidate.id);
        pushToast("info", t("suggestion_ignored"));
      }
      await reloadAfterMutation();
      return true;
    } catch (error) {
      // Write-gate refusal `MemoryAPIError.message` is **error code** (`super(detail)`):
      // direct toast displays `memory_write_gate_refused` (captured on actual 116 device).
      // Share `memory_card` namespace with chat inline card, no duplicated wording.
      // (Before batch 1c, third copy existed: drawer `candidates-section.tsx` also used
      // `(error as Error).message`; drawer removed with in-place expansion.)
      const refusal = _gateRefusal(error);
      pushErrorToast(error, refusal ? tCard(`refused_${refusal}`) : undefined);
      return false;
    } finally {
      setActing(null);
    }
  };

  const removeItem = async (item: CanonicalMemoryItem): Promise<boolean> => {
    setActing(item.id);
    try {
      await deleteEntry(item.id, item.version);
      // Behavior batch B: undo after moving to trash (backend `POST /memory/{id}/restore`). Trashing increments version,
      // undo omits If-Match (predelete version yields 409).
      toast({
        kind: "info",
        undo: true,
        text: t("delete_done"),
        action: {
          label: tCard("undo"),
          onAction: async () => {
            try {
              await restoreEntry(item.id);
              pushToast("info", t("restore_done"));
            } catch (error) {
              pushErrorToast(error);
              throw error;
            } finally {
              await reloadAfterMutationRef.current();
            }
          },
        },
      });
      await reloadAfterMutation();
      return true;
    } catch (error) {
      pushErrorToast(error);
      return false;
    } finally {
      setActing(null);
    }
  };

  const restoreItem = async (item: CanonicalMemoryItem) => {
    setActing(item.id);
    try {
      await restoreEntry(item.id, item.version);
      pushToast("info", t("restore_done"));
      await reloadAfterMutation();
    } catch (error) {
      pushErrorToast(error);
    } finally {
      setActing(null);
    }
  };

  const removeAboutYou = async (): Promise<boolean> => {
    if (!overview) return false;
    setActing("about-you");
    try {
      await deleteAboutYou(overview.about_you.version);
      pushToast("info", t("about_delete_done"));
      await reloadAfterMutation();
      return true;
    } catch (error) {
      pushErrorToast(error);
      return false;
    } finally {
      setActing(null);
    }
  };

  const openResetPreview = async (returnFocus: HTMLElement) => {
    resetReturnFocusRef.current = returnFocus;
    setResetting(true);
    try {
      const preview = await previewMemoryReset();
      resetIdempotencyKey.current = createMemoryIdempotencyKey();
      setResetPreview(preview.counts);
    } catch (error) {
      pushErrorToast(error);
    } finally {
      setResetting(false);
    }
  };

  const confirmAction = async () => {
    if (!actionConfirmTarget) return;
    const succeeded =
      actionConfirmTarget.kind === "suggestion"
        ? await decideSuggestion(actionConfirmTarget.candidate, "approve")
        : await removeAboutYou();
    if (succeeded) setActionConfirmTarget(null);
  };

  const confirmReset = async () => {
    setResetting(true);
    try {
      const idempotencyKey =
        resetIdempotencyKey.current ?? createMemoryIdempotencyKey();
      resetIdempotencyKey.current = idempotencyKey;
      const result = await resetMemory(idempotencyKey);
      resetIdempotencyKey.current = null;
      setResetPreview(null);
      setItemState("active");
      if (result.undo_available && result.snapshot_id) {
        // Behavior batch B: undo after clearing, same restore endpoint as “最近清空” (restores captured versions only).
        const snapshotId = result.snapshot_id;
        const restoreKey = createMemoryIdempotencyKey();
        toast({
          kind: "info",
          undo: true,
          text: t("reset_done"),
          action: {
            label: tCard("undo"),
            onAction: async () => {
              try {
                const restored = await restoreMemoryReset(snapshotId, restoreKey);
                pushToast(
                  restored.status === "restored" ? "info" : "error",
                  restored.status === "restored"
                    ? t("reset_restore_done")
                    : t("reset_restore_partial", { n: restored.remaining_recoverable }),
                );
              } catch (error) {
                pushErrorToast(error);
                throw error;
              } finally {
                await reloadAfterMutationRef.current();
              }
            },
          },
        });
      } else {
        pushToast(
          "info",
          result.undo_available ? t("reset_done") : t("reset_nothing"),
        );
      }
      await reloadAfterMutation();
    } catch (error) {
      pushErrorToast(error);
    } finally {
      setResetting(false);
    }
  };

  const restoreReset = async (snapshot: MemoryResetSummary) => {
    setRestoringReset(snapshot.id);
    try {
      const idempotencyKey =
        restoreResetIdempotencyKeys.current.get(snapshot.id) ??
        createMemoryIdempotencyKey();
      restoreResetIdempotencyKeys.current.set(snapshot.id, idempotencyKey);
      const result = await restoreMemoryReset(snapshot.id, idempotencyKey);
      pushToast(
        result.status === "restored" ? "info" : "error",
        result.status === "restored"
          ? t("reset_restore_done")
          : t("reset_restore_partial", {
              n: result.remaining_recoverable,
            }),
      );
      await reloadAfterMutation();
      restoreResetIdempotencyKeys.current.delete(snapshot.id);
    } catch (error) {
      pushErrorToast(error);
    } finally {
      setRestoringReset(null);
    }
  };

  const confirmPermanentDelete = async () => {
    if (!permanentDeleteTarget) return;
    setPermanentlyDeleting(true);
    try {
      const operation =
        permanentDeleteTarget.kind === "item"
          ? await permanentDeleteMemoryItem(
              permanentDeleteTarget.item.id,
              permanentDeleteTarget.idempotencyKey,
            )
          : permanentDeleteTarget.kind === "trash"
            ? await permanentlyClearMemoryTrash(
                permanentDeleteTarget.idempotencyKey,
              )
            : permanentDeleteTarget.kind === "reset"
              ? await permanentDeleteMemoryReset(
                  permanentDeleteTarget.snapshot.id,
                  permanentDeleteTarget.idempotencyKey,
                )
              : await permanentDeleteAllMemory(
                  permanentDeleteTarget.idempotencyKey,
                );
      if (
        operation.status !== "completed" ||
        !operation.logical_scrub_completed ||
        !operation.physical_purge_completed ||
        !operation.deletion_receipt_committed
      ) {
        throw new Error(t("permanent_delete_incomplete"));
      }
      setPermanentDeleteTarget(null);
      if (operation.action === "permanent_delete_all") {
        setItemState("active");
      }
      pushToast("info", t("permanent_delete_done"));
      await reloadAfterMutation();
    } catch (error) {
      pushErrorToast(error);
    } finally {
      setPermanentlyDeleting(false);
    }
  };

  const loadMore = async () => {
    if (!nextCursor) return;
    const generation = requestGeneration.current;
    setLoadingMore(true);
    try {
      const page = await fetchCanonicalMemoryItems({
        state: itemState,
        query,
        sourceLabelKeys: memorySourceKeysMatching((key) => t(key as never), query),
        displayType,
        scopeType,
        cursor: nextCursor,
      });
      if (generation !== requestGeneration.current) return;
      setItems((current) => [...current, ...page.items]);
      setNextCursor(page.next_cursor);
    } catch (error) {
      if (generation !== requestGeneration.current) return;
      pushErrorToast(error);
    } finally {
      setLoadingMore(false);
    }
  };

  const dateFormatter = useMemo(
    () =>
      new Intl.DateTimeFormat(locale, {
        year: "numeric",
        month: "short",
        day: "numeric",
      }),
    [locale],
  );

  // Deep link `?item=<id>` (reset recovery, legacy /memory/?item= links): once the
  // default view has loaded, page forward until the memory appears, then scroll to
  // and briefly highlight it. A deleted or unknown id ends with a notice instead.
  useEffect(() => {
    const target = new URLSearchParams(window.location.search).get(MEMORY_FOCUS_PARAM);
    if (target) setFocusItemId(target);
  }, []);

  useEffect(() => {
    if (!focusItemId || !focusPending.current || loading || loadingMore) return;
    if (initialError) {
      focusPending.current = false;
      return;
    }
    if (items.some((item) => item.id === focusItemId)) {
      focusPending.current = false;
      setHighlightedItemId(focusItemId);
      document
        .getElementById(memoryItemElementId(focusItemId))
        ?.scrollIntoView({ block: "center" });
      return;
    }
    if (nextCursor && focusPagesFetched.current < MAX_FOCUS_PAGES) {
      focusPagesFetched.current += 1;
      void loadMore();
      return;
    }
    focusPending.current = false;
    pushToast("info", t("item_focus_missing"));
    // eslint-disable-next-line react-hooks/exhaustive-deps -- loadMore/pushToast read the current render's state.
  }, [focusItemId, initialError, items, loading, loadingMore, nextCursor]);

  useEffect(() => {
    if (!highlightedItemId) return;
    const timer = window.setTimeout(() => setHighlightedItemId(null), FOCUS_HIGHLIGHT_MS);
    return () => window.clearTimeout(timer);
  }, [highlightedItemId]);

  if (loading && !overview) {
    return <MemoryV2Skeleton />;
  }

  if (initialError && !overview) {
    return (
      <div
        className="memory-v2-state"
        role="alert"
        data-testid="memory-v2-error"
      >
        <Brain size={24} aria-hidden />
        <h2>{t("load_error_title")}</h2>
        <p>{initialError}</p>
        <button
          type="button"
          className="memory-v2-button primary"
          onClick={() => void load()}
        >
          <RefreshCw size={15} aria-hidden />
          {t("retry")}
        </button>
      </div>
    );
  }

  if (!overview) return null;

  const paused = overview.learning.automatic_learning === "paused";
  const visibleSuggestions = overview.suggestions.filter(
    (candidate) => candidate.canonical_state !== "approved",
  );

  return (
    <>
      <main className="memory-v2" data-testid="memory-v2">
        <header className="memory-v2-hero">
          <div>
            <span className="memory-v2-eyebrow">{t("eyebrow")}</span>
            <h1 className="ui-page-title">{t("title")}</h1>
            <p className="ui-page-desc">{t("lead")}</p>
          </div>
          <Switch
            appearance="none"
            className={`memory-v2-learning${paused ? " is-paused" : ""}`}
            checked={!paused}
            onCheckedChange={() => void toggleLearning()}
            busy={acting === "learning"}
            data-testid="memory-v2-learning"
          >
            <span className="memory-v2-learning-icon" aria-hidden>
              <Sparkles size={16} />
            </span>
            <span>
              <strong>
                {paused
                  ? t("learning_paused_label")
                  : t("learning_active_label")}
              </strong>
              <small>
                {paused ? t("learning_paused_hint") : t("learning_active_hint")}
              </small>
            </span>
            <span className="memory-v2-switch" aria-hidden>
              <span />
            </span>
          </Switch>
        </header>

        {refreshWarning && (
          <div className="memory-v2-warning" role="status">
            <span>{t("refresh_warning")}</span>
            <button
              type="button"
              onClick={() => void load({ background: true })}
            >
              {t("retry")}
            </button>
          </div>
        )}

        <section
          className="memory-v2-about"
          aria-labelledby="memory-about-title"
        >
          <div className="memory-v2-section-heading">
            <span className="memory-v2-section-icon">
              <UserIcon aria-hidden />
            </span>
            <div>
              <h2 id="memory-about-title">{t("about_title")}</h2>
              <p>{t("about_lead")}</p>
            </div>
            <div className="memory-v2-about-heading-actions">
              <span className="memory-v2-scope">
                {scopeLabel(overview.about_you.scope)}
              </span>
              {overview.about_you.capabilities.edit && (
                <button
                  type="button"
                  className="memory-v2-icon-button"
                  onClick={(event) => {
                    aboutYouReturnFocusRef.current = event.currentTarget;
                    setEditingAboutYou(true);
                  }}
                  aria-label={t("about_edit")}
                >
                  <Edit aria-hidden />
                </button>
              )}
              {overview.about_you.capabilities.soft_delete && (
                <button
                  type="button"
                  className="memory-v2-icon-button"
                  onClick={(event) => {
                    actionConfirmReturnFocusRef.current = event.currentTarget;
                    setActionConfirmTarget({ kind: "about-you" });
                  }}
                  disabled={acting === "about-you"}
                  aria-label={t("about_delete")}
                >
                  <Trash aria-hidden />
                </button>
              )}
            </div>
          </div>
          {overview.about_you.content ? (
            <div className="memory-v2-about-copy">
              {overview.about_you.content.split("\n").map((line, index) => (
                <p key={`${index}-${line}`}>{line || "\u00a0"}</p>
              ))}
            </div>
          ) : (
            <div className="memory-v2-about-empty">
              <p>{t("about_empty")}</p>
              <span>{t("about_empty_hint")}</span>
            </div>
          )}
          <div className="memory-v2-about-meta">
            <span>{t("about_scope_note")}</span>
            {overview.about_you.is_locked && <span>{t("about_locked")}</span>}
            {overview.about_you.updated_at && (
              <time dateTime={overview.about_you.updated_at}>
                {t("updated", {
                  date: dateFormatter.format(
                    new Date(overview.about_you.updated_at),
                  ),
                })}
              </time>
            )}
          </div>
        </section>

        {visibleSuggestions.length > 0 && (
          <section
            className="memory-v2-section"
            aria-labelledby="memory-suggestions-title"
          >
            <div className="memory-v2-section-heading">
              <span className="memory-v2-section-icon accent">
                <Sparkles aria-hidden />
              </span>
              <div>
                <h2 id="memory-suggestions-title">{t("suggestions_title")}</h2>
                <p>{t("suggestions_lead")}</p>
              </div>
              <span className="memory-v2-count">
                {overview.stats.pending_candidates}
              </span>
            </div>

            {(suggestionsExpanded || selectionMode) && (
              <div className="memory-v2-suggestions-bar">
                {selectionMode ? (
                  <>
                    <strong>
                      {t("suggestions_selected", { n: selectedIds.size })}
                    </strong>
                    <button
                      type="button"
                      className="memory-v2-text-button"
                      disabled={bulkBusy}
                      onClick={() =>
                        setSelectedIds(
                          new Set(visibleSuggestions.map((c) => c.id)),
                        )
                      }
                    >
                      {t("suggestions_select_all")}
                    </button>
                    <span className="memory-v2-suggestions-bar-gap" />
                    <button
                      type="button"
                      className="memory-v2-button primary small"
                      disabled={bulkBusy || selectedIds.size === 0}
                      data-testid="suggestions-bulk-approve"
                      onClick={() =>
                        void runBulk([...selectedIds], "approve")
                      }
                    >
                      {t("suggestions_bulk_approve")}
                    </button>
                    {/* ★ Ignore requires confirmation, save does not: ignore writes 30-day verbatim suppression,
                        one click suppresses N topics for month without user seeing what was suppressed;
                        save reversible (individual move to trash). Asymmetric cost warrants different threshold. */}
                    <button
                      type="button"
                      className="memory-v2-button danger small"
                      disabled={bulkBusy || selectedIds.size === 0}
                      data-testid="suggestions-bulk-ignore"
                      onClick={() => setBulkIgnoreConfirm([...selectedIds])}
                    >
                      {t("suggestions_bulk_ignore")}
                    </button>
                    <button
                      type="button"
                      className="memory-v2-text-button"
                      disabled={bulkBusy}
                      onClick={leaveSelection}
                    >
                      {t("suggestions_bulk_cancel")}
                    </button>
                  </>
                ) : (
                  <>
                    <span>
                      {t("suggestions_total", {
                        n: overview.stats.pending_candidates,
                      })}
                    </span>
                    <span className="memory-v2-suggestions-bar-gap" />
                    <button
                      type="button"
                      className="memory-v2-text-button"
                      data-testid="suggestions-enter-select"
                      onClick={() => setSelectionMode(true)}
                    >
                      {t("suggestions_select")}
                    </button>
                    <button
                      type="button"
                      className="memory-v2-text-button"
                      onClick={() => setSuggestionsExpanded(false)}
                    >
                      {t("suggestions_collapse")}
                    </button>
                  </>
                )}
              </div>
            )}

            <div className="memory-v2-suggestions">
              {(suggestionsExpanded || selectionMode
                ? visibleSuggestions
                : visibleSuggestions.slice(0, COLLAPSED_SUGGESTIONS)
              ).map((candidate) => (
                <article
                  className="memory-v2-suggestion"
                  key={candidate.id}
                  data-selected={
                    selectionMode && selectedIds.has(candidate.id)
                      ? "true"
                      : undefined
                  }
                >
                  {selectionMode && (
                    <input
                      type="checkbox"
                      className="memory-v2-suggestion-check"
                      checked={selectedIds.has(candidate.id)}
                      onChange={() => toggleSelected(candidate.id)}
                      aria-label={t("suggestions_select_item")}
                      data-testid={`suggestion-check-${candidate.id}`}
                    />
                  )}
                  <div className="memory-v2-suggestion-copy">
                    <p>{candidate.display_value ?? t("content_unavailable")}</p>
                    {withoutMemoryIds(candidate.display_reason) && (
                      <small>{withoutMemoryIds(candidate.display_reason)}</small>
                    )}
                    <div className="memory-v2-suggestion-meta">
                      <span>
                        {scopeLabel(candidate.default_target) ??
                          t("suggestion_target_unavailable")}
                      </span>
                      {candidate.expires_at && (
                        <time dateTime={candidate.expires_at}>
                          {t("expires", {
                            date: dateFormatter.format(
                              new Date(candidate.expires_at),
                            ),
                          })}
                        </time>
                      )}
                    </div>
                  </div>
                  {/* In selection mode, individual actions yield to checkboxes: combining both leaves
                      clicking ✓ then selecting it with no explainable semantics. */}
                  {!selectionMode && (
                    <div className="memory-v2-suggestion-actions">
                      <button
                        type="button"
                        className="memory-v2-icon-button approve"
                        onClick={(event) => {
                          if (candidate.default_target?.requires_confirmation) {
                            actionConfirmReturnFocusRef.current =
                              event.currentTarget;
                            setActionConfirmTarget({
                              kind: "suggestion",
                              candidate,
                            });
                            return;
                          }
                          void decideSuggestion(candidate, "approve");
                        }}
                        disabled={
                          acting === candidate.id ||
                          candidate.capabilities?.approve === false ||
                          !candidate.default_target
                        }
                        aria-label={t("approve")}
                      >
                        <Check aria-hidden />
                      </button>
                      <button
                        type="button"
                        className="memory-v2-icon-button"
                        onClick={() =>
                          void decideSuggestion(candidate, "reject")
                        }
                        disabled={acting === candidate.id}
                        aria-label={t("ignore")}
                      >
                        <XSm aria-hidden />
                      </button>
                    </div>
                  )}
                </article>
              ))}
            </div>

            {!suggestionsExpanded &&
              !selectionMode &&
              visibleSuggestions.length > COLLAPSED_SUGGESTIONS && (
                <button
                  type="button"
                  className="memory-v2-suggestions-expand"
                  data-testid="suggestions-expand"
                  onClick={() => setSuggestionsExpanded(true)}
                >
                  {t("suggestions_expand", {
                    n: visibleSuggestions.length - COLLAPSED_SUGGESTIONS,
                  })}
                </button>
              )}
          </section>
        )}

        <section
          className="memory-v2-section"
          aria-labelledby="memory-learned-title"
        >
          <div className="memory-v2-section-heading">
            <span className="memory-v2-section-icon">
              <Brain aria-hidden />
            </span>
            <div>
              <h2 id="memory-learned-title">{t("learned_title")}</h2>
              <p>{t("learned_lead")}</p>
            </div>
            <button
              type="button"
              className="memory-v2-refresh"
              aria-label={t("refresh")}
              onClick={() => void load({ background: true })}
              disabled={refreshing}
            >
              <RefreshCw size={15} aria-hidden />
            </button>
          </div>

          {/* “有效 / 回收站” segmented control (styles/ui-refresh.css); the “清空回收站” action sits outside the group on the same row. */}
          <div className="memory-v2-state-tabs">
            <SegmentedControl
              className="memory-v2-state-seg"
              aria-label={t("item_state")}
              value={itemState}
              onValueChange={(next) => {
                if (next === itemState) return;
                setLoading(true);
                setItemState(next);
              }}
            >
              <Segment
                value="active"
                className={itemState === "active" ? "is-active" : ""}
              >
                {t("active_items", {
                  n:
                    overview.stats.saved_entries,
                })}
              </Segment>
              <Segment
                value="trashed"
                className={itemState === "trashed" ? "is-active" : ""}
              >
                {t("trash_items", { n: overview.stats.trash_count })}
              </Segment>
            </SegmentedControl>
            {itemState === "trashed" &&
              overview.stats.trash_count > 0 &&
              overview.capabilities.actions.permanent_delete && (
                <button
                  type="button"
                  className="is-danger memory-v2-clear-trash"
                  onClick={(event) => {
                    permanentDeleteReturnFocusRef.current = event.currentTarget;
                    setPermanentDeleteTarget({
                      kind: "trash",
                      count: overview.stats.trash_count,
                      idempotencyKey: createMemoryIdempotencyKey(),
                    });
                  }}
                >
                  {t("clear_trash_action")}
                </button>
              )}
          </div>

          <form className="memory-v2-filters" onSubmit={submitSearch}>
            <label className="memory-v2-search">
              <Search aria-hidden />
              <span className="sr-only">{t("search")}</span>
              <input
                type="search"
                value={queryInput}
                onChange={(event) => setQueryInput(event.target.value)}
                placeholder={t("search_placeholder")}
              />
            </label>
            <select
              value={displayType ?? ""}
              onChange={(event) => {
                setLoading(true);
                setDisplayType(event.target.value || null);
              }}
              aria-label={t("filter_type")}
            >
              <option value="">{t("all_types")}</option>
              {DISPLAY_TYPES.map((type) => (
                <option key={type} value={type}>
                  {t(`type_${type}`)}
                </option>
              ))}
            </select>
            <select
              value={scopeType ?? ""}
              onChange={(event) => {
                const value = event.target.value;
                setLoading(true);
                setScopeType(
                  value === "global" || value === "space" ? value : null,
                );
              }}
              aria-label={t("filter_scope")}
            >
              <option value="">{t("all_scopes")}</option>
              <option value="global">{t("scope_global")}</option>
              <option value="space">{t("scope_space")}</option>
            </select>
            <button type="submit" className="memory-v2-button">
              {t("search_action")}
            </button>
          </form>
          {/* At least two filter conditions show the selected filters below the filter row (decision 20). */}
          <ActiveFilterChips
            filters={[
              ...(query
                ? [{
                    key: "query",
                    label: t("chip_query", { value: query }),
                    onRemove: () => {
                      setLoading(true);
                      setQueryInput("");
                      setQuery("");
                    },
                  }]
                : []),
              ...(displayType
                ? [{
                    key: "type",
                    label: t("chip_type", { value: t(`type_${displayType}`) }),
                    onRemove: () => {
                      setLoading(true);
                      setDisplayType(null);
                    },
                  }]
                : []),
              ...(scopeType
                ? [{
                    key: "scope",
                    label: t("chip_scope", { value: t(scopeType === "global" ? "scope_global" : "scope_space") }),
                    onRemove: () => {
                      setLoading(true);
                      setScopeType(null);
                    },
                  }]
                : []),
            ]}
            onClearAll={clearFilters}
          />

          {loading ? (
            <LoadingSkeleton rows={3} label={t("items_loading")} />
          ) : items.length === 0 ? (
            <div className="memory-v2-empty">
              <Database size={22} aria-hidden />
              {query || displayType || scopeType ? (
                <>
                  <strong>{t("learned_no_results")}</strong>
                  <span>{t("learned_no_results_hint")}</span>
                  <button
                    type="button"
                    className="memory-v2-button"
                    onClick={clearFilters}
                  >
                    {t("clear_filters")}
                  </button>
                </>
              ) : (
                <>
                  <strong>
                    {itemState === "active"
                      ? t("learned_empty")
                      : t("trash_empty")}
                  </strong>
                  <span>
                    {itemState === "active"
                      ? t("learned_empty_hint")
                      : t("trash_empty_hint")}
                  </span>
                </>
              )}
            </div>
          ) : (
            <div className="memory-v2-items">
              {items.map((item) => (
                <MemoryItemRow
                  key={item.id}
                  item={item}
                  acting={acting === item.id}
                  highlighted={highlightedItemId === item.id}
                  dateFormatter={dateFormatter}
                  onDelete={(target) => {
                    // Behavior batch B (user decision 2026-09-28): moving to trash is undoable, so it runs without confirmation and offers undo.
                    void removeItem(target);
                  }}
                  onEdit={(target, returnFocus) => {
                    editItemReturnFocusRef.current = returnFocus;
                    setEditingItem(target);
                  }}
                  onPermanentDelete={(target, returnFocus) => {
                    permanentDeleteReturnFocusRef.current = returnFocus;
                    setPermanentDeleteTarget({
                      kind: "item",
                      item: target,
                      idempotencyKey: createMemoryIdempotencyKey(),
                    });
                  }}
                  onRestore={restoreItem}
                />
              ))}
            </div>
          )}

          {nextCursor && (
            <button
              type="button"
              className="memory-v2-load-more"
              onClick={() => void loadMore()}
              disabled={loadingMore}
            >
              {loadingMore ? t("loading_more") : t("load_more")}
            </button>
          )}
        </section>

        <section
          className="memory-v2-privacy"
          aria-labelledby="memory-privacy-title"
        >
          <div className="memory-v2-section-heading">
            <span className="memory-v2-section-icon">
              <Shield aria-hidden />
            </span>
            <div>
              <h2 id="memory-privacy-title">{t("privacy_title")}</h2>
              <p>{t("privacy_lead")}</p>
            </div>
          </div>
          <div className="memory-v2-privacy-grid">
            <div>
              <span>
                <ArchiveBox aria-hidden />
              </span>
              <strong>{t("export_title")}</strong>
              <p>{t("export_lead")}</p>
              <a
                className="memory-v2-text-button"
                href={exportMemoryUrl()}
                target="_blank"
                rel="noopener noreferrer"
              >
                {t("export_action")}
              </a>
            </div>
            <div>
              <span>
                <Trash aria-hidden />
              </span>
              <strong>{t("retention_title")}</strong>
              <p>{t("retention_lead")}</p>
              <button
                type="button"
                className="memory-v2-text-button"
                onClick={() => {
                  if (itemState === "trashed") return;
                  setLoading(true);
                  setItemState("trashed");
                }}
              >
                {t("open_trash", { n: overview.stats.trash_count })}
              </button>
            </div>
            <div>
              <span>
                <RefreshCw aria-hidden />
              </span>
              <strong>{t("reset_title")}</strong>
              <p>{t("reset_lead")}</p>
              <button
                type="button"
                className="memory-v2-text-button"
                onClick={(event) => {
                  void openResetPreview(event.currentTarget);
                }}
                disabled={resetting}
              >
                {resetting ? t("reset_preparing") : t("reset_action")}
              </button>
            </div>
            {overview.capabilities.actions.permanent_delete && (
              <div className="memory-v2-privacy-danger">
                <span>
                  <Shield aria-hidden />
                </span>
                <strong>{t("delete_all_title")}</strong>
                <p>{t("delete_all_lead")}</p>
                <button
                  type="button"
                  className="memory-v2-text-button"
                  onClick={(event) => {
                    permanentDeleteReturnFocusRef.current = event.currentTarget;
                    setPermanentDeleteTarget({
                      kind: "all",
                      count:
                        overview.stats.saved_entries +
                        overview.stats.trash_count +
                        overview.stats.recoverable_resets +
                        (overview.stats.notes_configured ? 1 : 0),
                      idempotencyKey: createMemoryIdempotencyKey(),
                    });
                  }}
                >
                  {t("delete_all_action")}
                </button>
              </div>
            )}
          </div>
          {recentResets.length > 0 && (
            <div className="memory-v2-recent-resets">
              <strong>{t("recent_resets_title")}</strong>
              <p>{t("recent_resets_lead")}</p>
              {recentResets.map((snapshot) => (
                <div key={snapshot.id}>
                  <span>
                    {t("recent_reset_row", {
                      date: dateFormatter.format(new Date(snapshot.created_at)),
                      n: snapshot.remaining_recoverable,
                    })}
                  </span>
                  <RowActionsMenu
                    label={t("reset_more_actions", {
                      date: dateFormatter.format(new Date(snapshot.created_at)),
                    })}
                    busy={restoringReset === snapshot.id}
                    busyLabel={t("restoring")}
                    actions={[
                      {
                        id: "reset-restore",
                        label: t("reset_restore_action"),
                        disabled: restoringReset === snapshot.id,
                        onSelect: () => void restoreReset(snapshot),
                      },
                      ...(overview.capabilities.actions.permanent_delete
                        ? [
                            {
                              id: "reset-permanent-delete",
                              label: t("reset_permanent_delete_action"),
                              danger: true,
                              onSelect: () => {
                            const trigger = document.activeElement;
                            permanentDeleteReturnFocusRef.current =
                              trigger instanceof HTMLElement ? trigger : null;
                            setPermanentDeleteTarget({
                              kind: "reset",
                              snapshot,
                              idempotencyKey: createMemoryIdempotencyKey(),
                            });
                          },
                            },
                          ]
                        : []),
                    ]}
                  />
                </div>
              ))}
            </div>
          )}
          <p className="memory-v2-privacy-note">{t("privacy_note")}</p>
        </section>
      </main>

      {editingItem && (
        <MemoryEditDialog
          item={editingItem}
          returnFocusRef={editItemReturnFocusRef}
          onClose={() => setEditingItem(null)}
          onSaved={(updated) => {
            setItems((current) =>
              current.map((item) => (item.id === updated.id ? updated : item)),
            );
            setEditingItem(null);
            pushToast("info", t("edit_done"));
          }}
        />
      )}

      {editingAboutYou && (
        <MemoryAboutYouEditDialog
          aboutYou={overview.about_you}
          returnFocusRef={aboutYouReturnFocusRef}
          onClose={() => setEditingAboutYou(false)}
          onSaved={(updated) => {
            setOverview((current) =>
              current
                ? {
                    ...current,
                    about_you: updated,
                    stats: {
                      ...current.stats,
                      notes_configured: Boolean(updated.content),
                    },
                  }
                : current,
            );
            setEditingAboutYou(false);
            pushToast("info", t("about_edit_done"));
          }}
        />
      )}

      {resetPreview && (
        <MemoryResetDialog
          counts={resetPreview}
          returnFocusRef={resetReturnFocusRef}
          saving={resetting}
          onClose={() => {
            resetIdempotencyKey.current = null;
            setResetPreview(null);
          }}
          onConfirm={() => void confirmReset()}
        />
      )}

      {permanentDeleteTarget && (
        <MemoryPermanentDeleteDialog
          target={permanentDeleteTarget}
          returnFocusRef={permanentDeleteReturnFocusRef}
          saving={permanentlyDeleting}
          onClose={() => setPermanentDeleteTarget(null)}
          onConfirm={() => void confirmPermanentDelete()}
        />
      )}

      {/* ★ Bulk-ignore confirmation. Save omits this step; see button comment above. */}
      {bulkIgnoreConfirm !== null && (
        <MemoryActionConfirmDialog
          busy={bulkBusy}
          title={t("suggestions_bulk_ignore_title", {
            n: bulkIgnoreConfirm.length,
          })}
          description={t("suggestions_bulk_ignore_body")}
          eyebrow={t("confirmation_eyebrow")}
          tone="danger"
          confirmLabel={t("suggestions_bulk_ignore_confirm", {
            n: bulkIgnoreConfirm.length,
          })}
          cancelLabel={t("suggestions_bulk_ignore_cancel")}
          onCancel={() => setBulkIgnoreConfirm(null)}
          onConfirm={() => {
            const ids = bulkIgnoreConfirm;
            setBulkIgnoreConfirm(null);
            void runBulk(ids, "reject");
          }}
        />
      )}

      {actionConfirmTarget && (
        <MemoryActionConfirmDialog
          busy={
            actionConfirmTarget.kind === "suggestion"
              ? acting === actionConfirmTarget.candidate.id
              : acting === "about-you"
          }
          cancelLabel={t("cancel")}
          confirmLabel={
            actionConfirmTarget.kind === "suggestion"
              ? t("approve")
              : t("delete")
          }
          description={
            actionConfirmTarget.kind === "suggestion"
              ? t("suggestion_global_confirm")
              : t("about_delete_confirm")
          }
          eyebrow={t("confirmation_eyebrow")}
          onCancel={() => setActionConfirmTarget(null)}
          onConfirm={() => void confirmAction()}
          returnFocusRef={actionConfirmReturnFocusRef}
          title={
            actionConfirmTarget.kind === "suggestion"
              ? t("suggestion_global_confirm_title")
              : t("about_delete_confirm_title")
          }
          tone={
            actionConfirmTarget.kind === "suggestion" ? "default" : "danger"
          }
        />
      )}
    </>
  );
}

function MemoryResetDialog({
  counts,
  returnFocusRef,
  saving,
  onClose,
  onConfirm,
}: {
  counts: MemoryResetCounts;
  returnFocusRef: RefObject<HTMLElement | null>;
  saving: boolean;
  onClose: () => void;
  onConfirm: () => void;
}) {
  const t = useTranslations("memory_page.v2");
  const total = counts.notes + counts.entries;

  return (
    <DialogShell
      mount="inline"
      role="dialog"
      busy={saving}
      closeOnBackdrop={false}
      returnFocusRef={returnFocusRef}
      onClose={onClose}
      labelledBy="memory-v2-reset-title"
      panelProps={{ "aria-describedby": "memory-v2-reset-description" }}
      backdropClassName="memory-modal-overlay"
      panelClassName="memory-modal-box memory-v2-edit-dialog"
    >
      <div>
        <span className="memory-v2-eyebrow">{t("reset_eyebrow")}</span>
        <h2 id="memory-v2-reset-title">{t("reset_confirm_title")}</h2>
        <p id="memory-v2-reset-description">{t("reset_confirm_lead")}</p>
      </div>
      <dl className="memory-v2-reset-counts">
        <div>
          <dt>{t("reset_count_entries")}</dt>
          <dd>{counts.entries}</dd>
        </div>
        <div>
          <dt>{t("reset_count_about")}</dt>
          <dd>{counts.notes}</dd>
        </div>
        <div>
          <dt>{t("reset_count_pending")}</dt>
          <dd>{counts.pending_preserved}</dd>
        </div>
      </dl>
      {counts.locked_skipped > 0 && (
        <p className="memory-v2-warning">
          {t("reset_locked_skipped", { n: counts.locked_skipped })}
        </p>
      )}
      <p className="memory-v2-edit-note">
        {total > 0 ? t("reset_recovery_note") : t("reset_empty")}
      </p>
      <div className="memory-modal-actions">
        <button type="button" onClick={onClose} disabled={saving}>
          {t("cancel")}
        </button>
        <button
          type="button"
          className="primary"
          onClick={onConfirm}
          disabled={saving || total === 0}
        >
          {saving ? t("resetting") : t("reset_confirm_action")}
        </button>
      </div>
    </DialogShell>
  );
}

function MemoryPermanentDeleteDialog({
  target,
  returnFocusRef,
  saving,
  onClose,
  onConfirm,
}: {
  target: PermanentDeleteTarget;
  returnFocusRef: RefObject<HTMLElement | null>;
  saving: boolean;
  onClose: () => void;
  onConfirm: () => void;
}) {
  const t = useTranslations("memory_page.v2");
  const count =
    target.kind === "item"
      ? 1
      : target.kind === "reset"
        ? target.snapshot.remaining_recoverable
        : target.count;

  return (
    <DialogShell
      mount="inline"
      role="alertdialog"
      busy={saving}
      closeOnBackdrop={false}
      returnFocusRef={returnFocusRef}
      onClose={onClose}
      labelledBy="memory-v2-permanent-delete-title"
      panelProps={{ "aria-describedby": "memory-v2-permanent-delete-description" }}
      backdropClassName="memory-modal-overlay"
      panelClassName="memory-modal-box memory-v2-edit-dialog memory-v2-danger-dialog"
    >
      <div>
        <span className="memory-v2-eyebrow">
          {t("permanent_delete_eyebrow")}
        </span>
        <h2 id="memory-v2-permanent-delete-title">
          {t(`permanent_delete_${target.kind}_title`)}
        </h2>
        <p id="memory-v2-permanent-delete-description">
          {t(`permanent_delete_${target.kind}_lead`, {
            n: count,
            value: target.kind === "item" ? target.item.display_value : "",
          })}
        </p>
      </div>
      <p className="memory-v2-warning">
        {target.kind === "all"
          ? t("permanent_delete_all_boundary")
          : t("permanent_delete_no_undo")}
      </p>
      <div className="memory-modal-actions">
        <button type="button" onClick={onClose} disabled={saving}>
          {t("cancel")}
        </button>
        <button
          type="button"
          className="danger"
          onClick={onConfirm}
          disabled={saving}
        >
          {saving
            ? t("permanent_deleting")
            : t("permanent_delete_confirm_action")}
        </button>
      </div>
    </DialogShell>
  );
}

/** Selecting menu item already returned focus to “⋯” trigger; return there after dialog closes. */
function menuTrigger(): HTMLElement | null {
  const trigger = document.activeElement;
  return trigger instanceof HTMLElement ? trigger : null;
}

/** Item summary in menu name: first 24 characters, ellipsis if longer. */
function summarize(text: string): string {
  const flat = text.replace(/\s+/g, " ").trim();
  return flat.length > 24 ? `${flat.slice(0, 24)}…` : flat;
}

const MEMORY_SOURCE_KEYS = new Set([
  "legacy_migration",
  "auto_captured",
  "confirmed_suggestion",
  "told_in_chat",
  "system_maintained",
  "unavailable",
]);

/**
 * The item's source line in the interface language. The Gateway's `source_label` is Chinese copy kept for search
 * and older clients; render the structured `source_label_key` instead and fall back to the label only for a key
 * this client does not know (an older or newer Gateway).
 */
export function memorySourceLabel(
  t: (key: string) => string,
  createdFrom: { source_label: string; source_label_key?: string | null },
): string {
  const key = createdFrom.source_label_key;
  return key && MEMORY_SOURCE_KEYS.has(key) ? t(`source_${key}`) : createdFrom.source_label;
}

/**
 * Source-line keys whose label in the interface language contains the search text. The Gateway cannot see the
 * localized copy, so search sends these keys and the server matches exactly the source line the user reads.
 */
export function memorySourceKeysMatching(t: (key: string) => string, query: string): string[] {
  const needle = query.trim().toLocaleLowerCase();
  if (!needle) return [];
  return [...MEMORY_SOURCE_KEYS].filter((key) => t(`source_${key}`).toLocaleLowerCase().includes(needle));
}

function MemoryItemRow({
  item,
  acting,
  highlighted = false,
  dateFormatter,
  onDelete,
  onEdit,
  onPermanentDelete,
  onRestore,
}: {
  item: CanonicalMemoryItem;
  acting: boolean;
  highlighted?: boolean;
  dateFormatter: Intl.DateTimeFormat;
  onDelete: (item: CanonicalMemoryItem) => void;
  onEdit: (item: CanonicalMemoryItem, returnFocus: HTMLElement) => void;
  onPermanentDelete: (
    item: CanonicalMemoryItem,
    returnFocus: HTMLElement,
  ) => void;
  onRestore: (item: CanonicalMemoryItem) => Promise<void>;
}) {
  const t = useTranslations("memory_page.v2");
  const scopeLabel = useMemoryScopeLabel();
  const isTrashed = item.deleted_at !== null;
  return (
    <article
      id={memoryItemElementId(item.id)}
      className={`memory-v2-item${highlighted ? " is-focus-target" : ""}`}
      data-testid={`memory-v2-item-${item.id}`}
      data-highlighted={highlighted ? "true" : undefined}
    >
      <div className="memory-v2-item-type" data-type={item.display_type}>
        <span aria-hidden />
        {t(`type_${item.display_type}`)}
      </div>
      <div className="memory-v2-item-main">
        <p>{item.display_value}</p>
        <div className="memory-v2-item-meta">
          <span>{scopeLabel(item.scope)}</span>
          <span>{memorySourceLabel((key) => t(key as never), item.provenance.created_from)}</span>
          <time dateTime={item.updated_at}>
            {t("updated", {
              date: dateFormatter.format(new Date(item.updated_at)),
            })}
          </time>
          {isTrashed && item.purge_at && (
            <time dateTime={item.purge_at}>
              {t("purges", {
                date: dateFormatter.format(new Date(item.purge_at)),
              })}
            </time>
          )}
        </div>
      </div>
      <div className="memory-v2-item-actions">
        <RowActionsMenu
          label={t("more_actions_for", { name: summarize(item.display_value) })}
          busy={acting}
          actions={
            isTrashed
              ? [
                  {
                    id: "restore",
                    label: t("restore"),
                    disabled: acting || !item.capabilities.restore,
                    disabledReason: !item.capabilities.restore ? t("restore_unavailable_reason") : undefined,
                    onSelect: () => void onRestore(item),
                  },
                  ...(item.capabilities.permanent_delete
                    ? [
                        {
                          id: "permanent-delete",
                          label: t("permanent_delete"),
                          danger: true,
                          disabled: acting,
                          // Selecting menu item already returned focus to “⋯”; confirmation closes back there.
                          onSelect: () => {
                            const trigger = menuTrigger();
                            if (trigger) onPermanentDelete(item, trigger);
                          },
                        },
                      ]
                    : []),
                ]
              : [
                  ...(item.capabilities.edit
                    ? [
                        {
                          id: "edit",
                          label: t("edit"),
                          disabled: acting,
                          onSelect: () => {
                            const trigger = menuTrigger();
                            if (trigger) onEdit(item, trigger);
                          },
                        },
                      ]
                    : []),
                  {
                    id: "trash",
                    label: t("delete"),
                    danger: true,
                    // Undoable (undo hint after trash), no confirmation or “…”.
                    confirms: false,
                    disabled: acting || !item.capabilities.soft_delete,
                    disabledReason: !item.capabilities.soft_delete ? t("delete_unavailable_reason") : undefined,
                    onSelect: () => onDelete(item),
                  },
                ]
          }
        />
      </div>
    </article>
  );
}

function MemoryAboutYouEditDialog({
  aboutYou,
  returnFocusRef,
  onClose,
  onSaved,
}: {
  aboutYou: MemoryAboutYouV2;
  returnFocusRef: RefObject<HTMLElement | null>;
  onClose: () => void;
  onSaved: (aboutYou: MemoryAboutYouV2) => void;
}) {
  const t = useTranslations("memory_page.v2");
  const scopeLabel = useMemoryScopeLabel();
  const [draft, setDraft] = useState(aboutYou.content);
  const [session, setSession] = useState<{
    editToken: string;
    version: string;
  } | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const tokenRef = useRef<string | null>(null);
  const leasePromiseRef = useRef<ReturnType<
    typeof beginAboutYouEditSession
  > | null>(null);
  const sessionEffectActiveRef = useRef(false);
  const releaseTimerRef = useRef<number | null>(null);
  const translateRef = useRef(t);
  const charCount = countChars(draft);
  const overflow = charCount > 3000;

  useEffect(() => {
    if (releaseTimerRef.current !== null) {
      window.clearTimeout(releaseTimerRef.current);
      releaseTimerRef.current = null;
    }
    sessionEffectActiveRef.current = true;
    let active = true;
    const leasePromise =
      leasePromiseRef.current ?? beginAboutYouEditSession(aboutYou.version);
    leasePromiseRef.current = leasePromise;
    void leasePromise
      .then((lease) => {
        if (!active) {
          // React Strict Mode immediately tears down and replays effects in
          // development. The replay reuses this promise; only release when no
          // replacement effect is active (a real unmount).
          if (
            !sessionEffectActiveRef.current &&
            tokenRef.current !== lease.edit_token
          ) {
            tokenRef.current = lease.edit_token;
            void releaseAboutYouEditSession(lease.edit_token)
              .catch(() => undefined)
              .finally(() => {
                if (tokenRef.current === lease.edit_token) {
                  tokenRef.current = null;
                }
              });
          }
          return;
        }
        tokenRef.current = lease.edit_token;
        setSession({
          editToken: lease.edit_token,
          version: lease.version,
        });
      })
      .catch((caught) => {
        if (!active) return;
        setError(
          caught instanceof MemoryAPIError &&
            (caught.status === 409 || caught.status === 423)
            ? translateRef.current("about_edit_locked")
            : (caught as Error).message,
        );
      });
    return () => {
      active = false;
      sessionEffectActiveRef.current = false;
      releaseTimerRef.current = window.setTimeout(() => {
        releaseTimerRef.current = null;
        if (sessionEffectActiveRef.current) return;
        const token = tokenRef.current;
        tokenRef.current = null;
        if (token) {
          void releaseAboutYouEditSession(token).catch(() => undefined);
        }
      }, 0);
    };
  }, [aboutYou.version]);

  const save = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!session || overflow) return;
    setSaving(true);
    setError(null);
    try {
      const updated = await patchAboutYou({
        content: draft,
        version: session.version,
        editToken: session.editToken,
      });
      tokenRef.current = null;
      onSaved(updated);
    } catch (caught) {
      setError(
        caught instanceof MemoryAPIError && caught.status === 409
          ? t("about_edit_conflict")
          : caught instanceof MemoryAPIError && caught.status === 423
            ? t("about_edit_expired")
            : (caught as Error).message,
      );
    } finally {
      setSaving(false);
    }
  };

  return (
    <DialogShell
      mount="inline"
      role="dialog"
      busy={saving}
      closeOnBackdrop={false}
      returnFocusRef={returnFocusRef}
      onClose={onClose}
      labelledBy="memory-v2-about-edit-title"
      panelProps={{ "aria-describedby": error ? "memory-v2-about-edit-error" : undefined }}
      backdropClassName="memory-modal-overlay"
      panelClassName="memory-modal-box memory-v2-edit-dialog memory-v2-about-edit-dialog"
    >
      <form style={{ display: "contents" }} onSubmit={save}>
        <div>
          <span className="memory-v2-eyebrow">{scopeLabel(aboutYou.scope)}</span>
          <h2 id="memory-v2-about-edit-title">{t("about_edit_title")}</h2>
          <p>{t("about_edit_lead")}</p>
        </div>
        <label>
          {t("edit_value")}
          <textarea
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            rows={10}
            autoFocus
          />
        </label>
        <div
          className={
            overflow ? "memory-char-count overflow" : "memory-char-count"
          }
        >
          {t("about_char_count", { n: charCount })}
        </div>
        {error && (
          <p
            className="memory-modal-error"
            id="memory-v2-about-edit-error"
            role="alert"
          >
            {error}
          </p>
        )}
        <div className="memory-modal-actions">
          <button type="button" onClick={onClose} disabled={saving}>
            {t("cancel")}
          </button>
          <button
            type="submit"
            className="primary"
            disabled={!session || saving || overflow}
          >
            {!session ? t("edit_preparing") : saving ? t("saving") : t("save")}
          </button>
        </div>
      </form>
    </DialogShell>
  );
}

function MemoryEditDialog({
  item,
  returnFocusRef,
  onClose,
  onSaved,
}: {
  item: CanonicalMemoryItem;
  returnFocusRef: RefObject<HTMLElement | null>;
  onClose: () => void;
  onSaved: (item: CanonicalMemoryItem) => void;
}) {
  const t = useTranslations("memory_page.v2");
  const scopeLabel = useMemoryScopeLabel();
  const schema = item.display_schema.name;
  const initialValue =
    schema === "text_v1"
      ? item.display_schema.payload.content
      : item.display_schema.payload.value;
  const [draft, setDraft] = useState(String(initialValue ?? ""));
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const save = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setSaving(true);
    setError(null);
    try {
      let patch: Record<string, unknown>;
      if (schema === "text_v1") {
        patch = { content: draft };

      } else if (schema === "boolean_v1") {
        patch = { value: draft === "true" };
      } else if (schema === "number_v1") {
        const numberValue = Number(draft);
        if (!Number.isFinite(numberValue)) {
          setError(t("edit_number_invalid"));
          return;
        }
        patch = { value: numberValue };
      } else {
        setError(t("edit_schema_unsupported"));
        return;
      }
      const updated = await patchCanonicalMemoryItem(item.id, {
        schema,
        patch,
        version: item.version,
      });
      onSaved(updated);
    } catch (caught) {
      setError(
        caught instanceof MemoryAPIError && caught.status === 409
          ? t("edit_conflict")
          : (caught as Error).message,
      );
    } finally {
      setSaving(false);
    }
  };

  return (
    <DialogShell
      mount="inline"
      role="dialog"
      busy={saving}
      closeOnBackdrop={false}
      returnFocusRef={returnFocusRef}
      onClose={onClose}
      labelledBy="memory-v2-edit-title"
      panelProps={{ "aria-describedby": error ? "memory-v2-edit-error" : undefined }}
      backdropClassName="memory-modal-overlay"
      panelClassName="memory-modal-box memory-v2-edit-dialog"
    >
      <form style={{ display: "contents" }} onSubmit={save}>
        <div>
          <span className="memory-v2-eyebrow">
            {t(`type_${item.display_type}`)}
          </span>
          <h2 id="memory-v2-edit-title">{t("edit_title")}</h2>
          <p>{scopeLabel(item.scope)}</p>
        </div>

        {schema === "text_v1" && (
          <label>
            {t("edit_value")}
            <textarea
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
              rows={5}
              autoFocus
            />
          </label>
        )}
        {schema === "boolean_v1" && (
          <label>
            {t("edit_value")}
            <select
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
              autoFocus
            >
              <option value="true">{t("boolean_true")}</option>
              <option value="false">{t("boolean_false")}</option>
            </select>
          </label>
        )}
        {schema === "number_v1" && (
          <label>
            {t("edit_value")}
            <input
              type="number"
              step="any"
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
              autoFocus
            />
          </label>
        )}

        <p className="memory-v2-edit-note">{t("edit_scope_note")}</p>
        {error && (
          <p
            className="memory-modal-error"
            id="memory-v2-edit-error"
            role="alert"
          >
            {error}
          </p>
        )}
        <div className="memory-modal-actions">
          <button type="button" onClick={onClose} disabled={saving}>
            {t("cancel")}
          </button>
          <button type="submit" className="primary" disabled={saving}>
            {saving ? t("saving") : t("save")}
          </button>
        </div>
      </form>
    </DialogShell>
  );
}

function MemoryV2Skeleton() {
  const t = useTranslations("memory_page.v2");
  return (
    <div
      className="memory-v2 memory-v2-skeleton"
      aria-busy="true"
      aria-label={t("loading")}
    >
      <div className="memory-v2-skeleton-line wide" />
      <div className="memory-v2-skeleton-line" />
      <div className="memory-v2-skeleton-panel" />
      <div className="memory-v2-skeleton-panel" />
    </div>
  );
}
