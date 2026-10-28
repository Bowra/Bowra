/**
 * SectionRevisionDrawer — edit history of a report canvas (Canvas Objects Spec C13, part 2 screen 13).
 *
 * Shared Dialog drawer (right side; bottom sheet below 640): rows of time + “who · what”; the selected
 * row expands a human-readable diff (strike-through / insertion background; charts as “Type: Bar → Line”;
 * internal fields only counted). “Restore this version…” confirms first; early records that cannot be
 * restored say so inline.
 */

"use client";

import { useLocale, useTranslations } from "next-intl";
import { useEffect, useId, useState, type KeyboardEvent as ReactKeyboardEvent } from "react";
import { Composite, CompositeItem } from "@floating-ui/react";
import { patchCanvasSection } from "@/lib/canvas-pm-adapter";
import { apiFetch, HttpStatusError } from "@/lib/api-client";
import { toast } from "@/lib/use-toast";
import {
  ErrorDiagnosticsInline,
  type ShownError,
  shownError,
  useCopyDiagnosticsToastAction,
} from "@/components/error-notice";
import { useConfirmDialog } from "@/components/ui/confirm-dialog";
import { Dialog } from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { LoadingSkeleton } from "@/components/ui/loading-skeleton";
import { AlertCircle, History } from "@/components/icons";
import { ChangeRow } from "@/components/canvas/object";
import { diffText, trimDiffContext, type ChangeValue } from "@/lib/canvas-change-value";
import { formatShortDateTime } from "@/lib/locale";
import styles from "./section-revision-drawer.module.css";

/** Backend turn marker for machine organization (reordering etc.), not a user conversation turn. */
const MACHINE_TURN_PREFIX = "machine:";

/**
 * AI revision turn numbers use only server `turnOrdinal` (revision turn's ordinal across whole report).
 * Missing/nonpositive/nonintegral field displays no number. exported for tests.
 */
export function revisionTurnOrdinal(r: { turnOrdinal?: unknown }): number | null {
  const n = r.turnOrdinal;
  return typeof n === "number" && Number.isInteger(n) && n > 0 ? n : null;
}

/** Short revision timestamp such as "9月26日 14:23" (include year when different). exported for tests. */
export function formatRevisionTime(iso: string | undefined, locale: string, now = new Date()): string {
  return formatShortDateTime(iso, locale, now);
}

interface Revision {
  id: string | number;
  sectionId?: string | null;
  op: "append" | "patch" | "reorder" | "delete";
  byTurnId?: string | null;
  createdAt?: string;
  prevPayload?: Record<string, unknown> | null;
  newPayload?: Record<string, unknown> | null;
  /** Delete revision carrying a recovery envelope (server decides). */
  restorable?: boolean;
  /** Server ordinal of this revision's turn across the whole report; manual/machine edits use null. */
  turnOrdinal?: number | null;
}

/** Cascading deletions group delete revisions; only the host row restores the whole group. */
function restoreGroupSize(r: Revision, all: Revision[]): number | null {
  if (r.op !== "delete" || !r.restorable) return null;
  const env = r.prevPayload as { batch?: unknown; batch_host?: unknown } | null;
  if (!env?.batch || env.batch_host !== r.sectionId) return null;
  return all.filter(
    (x) =>
      x.op === "delete"
      && (x.prevPayload as { batch?: unknown } | null)?.batch === env.batch,
  ).length;
}

const TEXT_KEYS = ["markdown", "text", "body_md"] as const;
const TITLE_KEYS = ["title", "h2", "label", "caption"] as const;

export interface RevisionChange {
  label: "body" | "title" | "chart_type" | "chart_title";
  value: ChangeValue;
}

/**
 * Human-readable changes between two section payloads: prose as a diff, titles as a diff, chart type as
 * “before → after”. Fields users cannot read are only counted (“2 more formatting changes”). exported for tests.
 */
export function revisionChanges(
  prev: Record<string, unknown> | null | undefined,
  next: Record<string, unknown> | null | undefined,
  chartKind: (kind: string) => string,
): { changes: RevisionChange[]; other: number } {
  const changes: RevisionChange[] = [];
  let other = 0;
  const keys = new Set([...Object.keys(prev ?? {}), ...Object.keys(next ?? {})]);
  for (const key of keys) {
    const before = prev?.[key];
    const after = next?.[key];
    if (JSON.stringify(before ?? null) === JSON.stringify(after ?? null)) continue;
    if ((TEXT_KEYS as readonly string[]).includes(key) || (TITLE_KEYS as readonly string[]).includes(key)) {
      if (typeof (before ?? "") === "string" && typeof (after ?? "") === "string") {
        changes.push({
          label: (TEXT_KEYS as readonly string[]).includes(key) ? "body" : "title",
          value: { kind: "diff", segments: trimDiffContext(diffText(String(before ?? ""), String(after ?? ""))) },
        });
        continue;
      }
    }
    if (key === "spec" && before && after && typeof before === "object" && typeof after === "object") {
      const a = before as Record<string, unknown>;
      const b = after as Record<string, unknown>;
      let counted = false;
      if (a.kind !== b.kind && typeof a.kind === "string" && typeof b.kind === "string") {
        changes.push({ label: "chart_type", value: { kind: "text", text: `${chartKind(a.kind)} → ${chartKind(b.kind)}` } });
        counted = true;
      }
      if (a.title !== b.title && typeof (a.title ?? "") === "string" && typeof (b.title ?? "") === "string") {
        changes.push({ label: "chart_title", value: { kind: "diff", segments: diffText(String(a.title ?? ""), String(b.title ?? "")) } });
        counted = true;
      }
      const rest = new Set([...Object.keys(a), ...Object.keys(b)]);
      rest.delete("kind");
      rest.delete("title");
      if ([...rest].some((name) => JSON.stringify(a[name] ?? null) !== JSON.stringify(b[name] ?? null))) other += 1;
      else if (!counted) other += 1;
      continue;
    }
    other += 1;
  }
  return { changes, other };
}

export function SectionRevisionDrawer({
  canvasId,
  open,
  onClose,
  onRolledBack,
  sectionLabel,
  canEdit = true,
}: {
  canvasId: string;
  open: boolean;
  onClose: () => void;
  /** Reconciliation after a successful restore: the caller exits edit mode and refreshes from the server. */
  onRolledBack?: () => Promise<void> | void;
  /** Block ID → recognizable name (section title or opening words), replacing internal IDs. */
  sectionLabel?: (sectionId: string) => string | null;
  /** False hides restore actions (server rejects non-owners / read-only accounts too), leaving history only. */
  canEdit?: boolean;
}) {
  const t = useTranslations("revision_drawer");
  const tChart = useTranslations("chart_block");
  const locale = useLocale();
  const { confirm, dialog: confirmDialog } = useConfirmDialog();
  const [revisions, setRevisions] = useState<Revision[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<ShownError | null>(null);
  const [attempt, setAttempt] = useState(0);
  const copyDiagnosticsAction = useCopyDiagnosticsToastAction();
  const [activeIndex, setActiveIndex] = useState(0);
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const listId = useId();
  useEffect(() => {
    if (!open) return;
    setActiveIndex(0);
    setSelectedKey(null);
  }, [open, revisions]);

  useEffect(() => {
    if (!open || !canvasId) return;
    setLoading(true);
    setError(null);
    apiFetch(`/api/v1/canvas/${encodeURIComponent(canvasId)}/revisions`)
      .then((r) => {
        if (!r.ok) throw new HttpStatusError("revisions", r);
        return r.json();
      })
      .then((data) => {
        setRevisions(data?.items ?? []);
        setLoading(false);
      })
      .catch((e) => {
        // Say only what happened and the remedy; status codes and request IDs go in "复制诊断".
        setError(shownError(t("load_failed_plain"), e));
        setLoading(false);
      });
  }, [open, canvasId, t, attempt]);

  if (!open) return null;

  const chartKind = (kind: string) => (tChart.has(`kind.${kind}`) ? tChart(`kind.${kind}` as never) : kind);
  const whoLabel = (r: Revision): string => {
    const turn = r.byTurnId;
    if (!turn) return t("who_manual");
    if (turn.startsWith(MACHINE_TURN_PREFIX)) return t("who_system");
    const n = revisionTurnOrdinal(r);
    return n ? t(r.op === "append" ? "who_ai_written_turn" : "who_ai_rewritten_turn", { n }) : t(r.op === "append" ? "who_ai_written" : "who_ai_rewritten");
  };
  const rowKey = (r: Revision, i: number) => `${r.id}_${i}`;
  const selectedIndex = revisions.findIndex((r, i) => rowKey(r, i) === selectedKey);
  const selected = selectedIndex >= 0 ? revisions[selectedIndex] : null;
  const label = (r: Revision) => (r.sectionId ? sectionLabel?.(r.sectionId) ?? null : null);

  const restoreSection = async (r: Revision, groupSize: number) => {
    const ok = await confirm({
      title: t("restore_title"),
      description: groupSize > 1 ? t("restore_confirm_with_children_plain", { count: groupSize - 1 }) : t("restore_confirm_plain"),
      confirmLabel: t("restore_action"),
      tone: "primary",
    });
    if (!ok) return;
    const res = await apiFetch(
      `/api/v1/canvas/${encodeURIComponent(canvasId)}/revisions/${encodeURIComponent(String(r.id))}/restore`,
      { method: "POST" },
    );
    if (res.ok) {
      const data = await res.json().catch(() => null);
      try {
        await onRolledBack?.();
      } catch (e) {
        console.warn("[revision-drawer] onRolledBack failed", e);
      }
      toast({ kind: "success", text: t("restore_success", { count: data?.restoredSectionIds?.length ?? 0 }) });
      onClose();
    } else if (res.status === 409) {
      toast({ kind: "info", text: t("restore_already") });
    } else {
      const failure = new HttpStatusError("restore revision", res);
      toast({
        kind: "error",
        text: t(
          res.status === 403 ? "restore_forbidden"
            : res.status === 404 ? "restore_gone"
              // Old-format deletion records lack recovery content: retries cannot help; explain clearly.
              : res.status === 422 ? "restore_unsupported" : "restore_failed_plain",
        ),
        action: copyDiagnosticsAction(failure, "task"),
      });
    }
  };
  const restoreVersion = async (r: Revision) => {
    const time = formatRevisionTime(r.createdAt, locale);
    const ok = await confirm({
      title: t("rollback_title"),
      description: t("rollback_confirm_plain", { section: label(r) ?? t("this_section"), time }),
      confirmLabel: t("rollback_action"),
      tone: "primary",
    });
    if (!ok) return;
    const res = await patchCanvasSection(canvasId, r.sectionId ?? "", r.prevPayload as Record<string, unknown>);
    if (res.ok) {
      // Reconcile first (exit editing + reload + refresh from server), then close; close even if reconciliation throws.
      try {
        await onRolledBack?.();
      } catch (e) {
        console.warn("[revision-drawer] onRolledBack failed", e);
      }
      toast({ kind: "success", text: t("rollback_success", { time }) });
      onClose();
    } else {
      toast({
        kind: "error",
        text: t(res.status === "not_found" ? "rollback_gone" : "rollback_failed_plain"),
        action: res.status === "error" ? copyDiagnosticsAction(res.error, "task") : undefined,
      });
    }
  };

  const action = (r: Revision | null): { label: string; run: () => void } | null => {
    if (!r || !canEdit) return null;
    const groupSize = restoreGroupSize(r, revisions);
    if (groupSize !== null) return {
      label: groupSize > 1 ? t("restore_button_with_children_plain", { count: groupSize - 1 }) : t("restore_button_plain"),
      run: () => void restoreSection(r, groupSize),
    };
    if (r.op === "patch" && r.prevPayload) return { label: t("rollback_button_plain"), run: () => void restoreVersion(r) };
    return null;
  };
  const unsupported = (r: Revision) => (r.op === "delete" && !r.restorable) || (r.op === "patch" && !r.prevPayload);

  const onListKeyDown = (event: ReactKeyboardEvent<HTMLElement>) => {
    if (event.key !== "Home" && event.key !== "End") return;
    const rows = Array.from(event.currentTarget.querySelectorAll<HTMLElement>(":scope > [role='option']"));
    if (rows.length === 0) return;
    event.preventDefault();
    const index = event.key === "Home" ? 0 : rows.length - 1;
    setActiveIndex(index);
    rows[index].focus();
  };

  const current = action(selected);
  return (
    <>
      <Dialog
        variant="drawer"
        width={420}
        title={<span className={styles.title}><History size={14} aria-hidden />{t("title_plain")}</span>}
        closeLabel={t("close")}
        onClose={onClose}
        footer={canEdit ? (
          <div className={styles.footer}>
            <span className={styles.hint}>{t("footer_plain")}</span>
            <Button disabled={!current} onClick={() => current?.run()}>{current?.label ?? t("rollback_button_plain")}</Button>
          </div>
        ) : undefined}
      >
        {loading ? <LoadingSkeleton rows={3} label={t("loading")} /> : null}
        {error ? (
          <div className={styles.status}>
            <span className={styles.statusText} data-tone="error" role="alert"><AlertCircle size={14} aria-hidden />{error.message}</span>
            <Button size="sm" onClick={() => setAttempt((value) => value + 1)}>{t("retry")}</Button>
            <ErrorDiagnosticsInline error={error.error} surface="route" />
          </div>
        ) : null}
        {!loading && !error && revisions.length === 0 ? (
          <p className={styles.statusText}><History size={14} aria-hidden />{t("no_history")}</p>
        ) : null}
        {!loading && !error && revisions.length > 0 ? (
          // ↑/↓ move between revisions (Home / End to endpoints); Enter selects and expands its changes.
          <Composite
            role="listbox"
            aria-label={t("list_aria")}
            id={listId}
            orientation="vertical"
            loop={false}
            activeIndex={activeIndex}
            onNavigate={setActiveIndex}
            className={styles.list}
            onKeyDown={onListKeyDown}
          >
            {revisions.map((r, i) => {
              const key = rowKey(r, i);
              const isSelected = key === selectedKey;
              const details = isSelected && r.op === "patch" ? revisionChanges(r.prevPayload, r.newPayload, chartKind) : null;
              const section = label(r);
              const what = [whoLabel(r), r.op === "delete" ? t("op_delete") : r.op === "reorder" ? t("op_reorder") : null, section].filter(Boolean).join(" · ");
              return (
                <CompositeItem
                  key={key}
                  render={
                    <div
                      role="option"
                      aria-selected={isSelected}
                      className={styles.row}
                      onClick={() => { setSelectedKey(isSelected ? null : key); setActiveIndex(i); }}
                      onKeyDown={(event) => {
                        if (event.target !== event.currentTarget) return;
                        if (event.key !== "Enter" && event.key !== " ") return;
                        event.preventDefault();
                        setSelectedKey(isSelected ? null : key);
                      }}
                    />
                  }
                >
                  <span className={styles.rowTime}>
                    {formatRevisionTime(r.createdAt, locale)}
                    {r.sectionId && r.op !== "delete" && revisions.findIndex((item) => item.sectionId === r.sectionId) === i ? <Badge size="sm">{t("current")}</Badge> : null}
                  </span>
                  <span className={styles.rowWhat}>
                    {what}
                    {unsupported(r) ? ` · ${t("restore_unsupported_short")}` : null}
                  </span>
                  {details ? (
                    <ChangeRow location={section ?? t("this_section")}
                      rows={details.changes.map((change) => ({ label: t(`diff_${change.label}`), value: <DiffValue value={change.value} /> }))}>
                      {details.other ? <span className={styles.hint}>{t("diff_other", { count: details.other })}</span> : null}
                      {!details.changes.length && !details.other ? <span className={styles.hint}>{t("no_field_changes")}</span> : null}
                    </ChangeRow>
                  ) : null}
                </CompositeItem>
              );
            })}
          </Composite>
        ) : null}
      </Dialog>
      {confirmDialog}
    </>
  );
}

function DiffValue({ value }: { value: ChangeValue }) {
  if (value.kind === "diff") return (
    <span>
      {value.segments.map((segment, index) => segment.op === "equal" ? <span key={index}>{segment.text}</span>
        : segment.op === "insert" ? <ins key={index} className={styles.inserted}>{segment.text}</ins>
          : <del key={index} className={styles.deleted}>{segment.text}</del>)}
    </span>
  );
  return <>{value.kind === "text" ? value.text : null}</>;
}
