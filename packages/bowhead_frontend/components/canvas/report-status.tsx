"use client";

/**
 * R2 report-state-machine UI: reuse design-system rc-badge / rc-action-btn primitives (no new visual language).
 *
 *  - ReportStatusBadge: explicit lifecycle copy. Owner review is neutral and is
 *    never presented as system verification.
 *  - ReportStatusControl: badge + menu (archive/restore/owner review), independent of data source; shared by /reports cards
 *    and report-header chips.
 *  - ReportStatusChip: report header; fetch canvasId status (lightweight /status endpoint) + persist via setCanvasStatus.
 */

import { useEffect, useId, useState, type ReactNode } from "react";
import type { Placement } from "@floating-ui/react";
import { useTranslations } from "next-intl";
import {
  fetchCanvasStatus,
  setCanvasStatus,
  type CanvasStatusMeta,
  type ReportStatus,
} from "@/lib/api";
import { Caret } from "@/components/icons";
import { reportStatusTransitions } from "@/lib/report-status-transitions";
import { MenuFloating, MenuItemReason, useMenu, useMenuItem } from "@/components/ui/menu";
import { toast } from "@/lib/use-toast";
import {
  CANVAS_CHANGED_EVENT,
  cachedReportStatus,
  invalidateReportStatus,
  requestReportStatus,
  subscribeReportStatus,
} from "@/lib/report-status-store";

const BADGE_VARIANT: Record<string, string> = {
  verified: "neutral",
  stale: "amber",
  archived: "neutral",
  active: "neutral",
};

export function ReportStatusBadge({
  status,
  quarantinedCount = 0,
  className,
}: {
  status: string;
  quarantinedCount?: number;
  className?: string;
}) {
  const t = useTranslations("report_status");
  const key = status in BADGE_VARIANT ? status : "active";
  const labelKey = key === "stale" && quarantinedCount > 0 ? "quarantined_stale" : key;
  return (
    <span className={`rc-badge ${BADGE_VARIANT[key]}${className ? " " + className : ""}`}>
      {t(labelKey)}
    </span>
  );
}

/** Available transitions from current stored state (manual: mark/unmark reviewed + archive/restore). */
const transitionsFor = reportStatusTransitions;

export function ReportStatusControl({
  storedStatus,
  effectiveStatus,
  quarantinedCount = 0,
  onChange,
  busy = false,
  menuPlacement = "bottom-end",
}: {
  storedStatus: string;
  effectiveStatus: string;
  quarantinedCount?: number;
  onChange: (to: ReportStatus) => void | Promise<void>;
  busy?: boolean;
  /** Right-aligned in /reports cards by default; left-aligned, opening right into body space in report headers (Feishu 07.06 #10). */
  menuPlacement?: Placement;
}) {
  const t = useTranslations("report_status");
  const [open, setOpen] = useState(false);
  const menu = useMenu({ open, onOpenChange: setOpen, placement: menuPlacement, offset: 4 });

  const actions = transitionsFor(storedStatus);

  return (
    <div className="rc-status-control">
      <button
        type="button"
        ref={menu.refs.setReference}
        className="rc-status-trigger"
        disabled={busy}
        title={t("change_status")}
        {...menu.getReferenceProps()}
      >
        <ReportStatusBadge status={effectiveStatus} quarantinedCount={quarantinedCount} />
        <Caret size={11} aria-hidden />
      </button>
      <MenuFloating menu={menu}>
        <div
          ref={menu.refs.setFloating}
          {...menu.getFloatingProps({ className: "rc-status-menu", "aria-label": t("change_status") })}
        >
          {actions.map((a) => (
            <StatusMenuItem key={a.key} disabled={busy} onSelect={() => void onChange(a.to)}>
              {t(`action_${a.key}`)}
            </StatusMenuItem>
          ))}
        </div>
      </MenuFloating>
    </div>
  );
}

function StatusMenuItem({
  disabled,
  onSelect,
  children,
}: {
  disabled: boolean;
  onSelect: () => void;
  children: ReactNode;
}) {
  const itemProps = useMenuItem({ onSelect, disabled });
  // Behavior batch A: saving items omit native disabled (keyboard-highlightable), with reason always on line two.
  const t = useTranslations("report_status");
  const reasonId = useId();
  const reason = disabled ? t("busy_reason") : null;
  return (
    <>
      <button
        type="button"
        className="rc-status-menu-item"
        aria-describedby={reason ? reasonId : undefined}
        {...itemProps}
      >
        {children}
      </button>
      {reason ? <MenuItemReason id={reasonId}>{reason}</MenuItemReason> : null}
    </>
  );
}

/** Failure to read status does not block report rendering; the chip simply stays hidden or unchanged. */
function ignoreStatusReadError(): void {}

/**
 * A report's lifecycle status: fetch, persist and refresh on canvas changes (quarantine may change effective).
 * Every reader of one canvas shares a single request (see `lib/report-status-store.ts`): the chip's placement
 * remount and the report "⋯" items no longer each send their own GET.
 */
export function useReportStatus(canvasId: string) {
  const [meta, setMeta] = useState<CanvasStatusMeta | null>(() => cachedReportStatus(canvasId));
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    setMeta(cachedReportStatus(canvasId));
    const unsubscribe = subscribeReportStatus(canvasId, setMeta);
    requestReportStatus(canvasId, fetchCanvasStatus).catch(ignoreStatusReadError);
    // `cause` starts the event's change generation first, so this never joins a GET sent before the change.
    const onChanged = (event: Event) => {
      requestReportStatus(canvasId, fetchCanvasStatus, { force: true, cause: event }).catch(ignoreStatusReadError);
    };
    window.addEventListener(CANVAS_CHANGED_EVENT, onChanged);
    return () => {
      unsubscribe();
      window.removeEventListener(CANVAS_CHANGED_EVENT, onChanged);
    };
  }, [canvasId]);

  const change = async (to: ReportStatus) => {
    if (busy) return false;
    setBusy(true);
    try {
      await setCanvasStatus(canvasId, to);
      invalidateReportStatus(canvasId);
      // Mounted readers (this one included) refetch once, sharing a single GET, which this change then awaits.
      window.dispatchEvent(new Event(CANVAS_CHANGED_EVENT));
      await requestReportStatus(canvasId, fetchCanvasStatus).catch(ignoreStatusReadError);
      return true;
    } catch {
      /* The chip stays as it was; callers without a visible chip report the failure. */
      return false;
    } finally {
      setBusy(false);
    }
  };

  return { meta, busy, change };
}

/**
 * The default state carries no badge (Report Canvas Chrome Spec: 活跃不显示徽标; the reports list does the same):
 * an active report shows no "Report status: active" chip and its transitions live in the report "⋯" menu.
 */
export function isDefaultReportStatus(meta: Pick<CanvasStatusMeta, "status" | "effectiveStatus">): boolean {
  return meta.status === "active" && meta.effectiveStatus === "active";
}

/** Report header: the status badge and its transition menu, for a non-default status only. */
export function ReportStatusChip({
  canvasId,
  readOnly = false,
  hideDefault = false,
}: {
  canvasId: string;
  /**
   * Appearance refresh batch 4b: after badge moves into toolbar status group, read-only/shared views show status without a status-editing menu
   * (no editing entries in read-only views). Only toolbar status group passes this; legacy report-header position does not, retaining behavior.
   */
  readOnly?: boolean;
  /** Toolbar placement: hide the default (active) status; the report "⋯" menu then carries its transitions. */
  hideDefault?: boolean;
}) {
  const { meta, busy, change } = useReportStatus(canvasId);

  if (!meta || (hideDefault && isDefaultReportStatus(meta))) return null;

  if (readOnly) {
    return (
      <span className="rc-status-control rc-status-control--static" data-testid="report-status-static">
        <ReportStatusBadge status={meta.effectiveStatus} quarantinedCount={meta.quarantinedCount} />
      </span>
    );
  }

  return (
    <ReportStatusControl
      storedStatus={meta.status}
      effectiveStatus={meta.effectiveStatus}
      quarantinedCount={meta.quarantinedCount}
      onChange={(to) => void change(to)}
      busy={busy}
      menuPlacement="bottom-start"
    />
  );
}

/**
 * Transitions of an active report (Mark as reviewed, Archive) for the report "⋯" menu, since the default status shows
 * no chip. Renders nothing for a non-default status: its chip carries the menu. `renderItem` supplies the host
 * menu's own item component so the items share that menu's roles and keyboard handling.
 */
export function ActiveReportStatusMenuItems({
  canvasId,
  renderItem,
}: {
  canvasId: string;
  renderItem: (item: { key: string; label: string; onSelect: () => void }) => ReactNode;
}) {
  const t = useTranslations("report_status");
  const { meta, change } = useReportStatus(canvasId);
  if (!meta || !isDefaultReportStatus(meta)) return null;
  return (
    <>
      {transitionsFor(meta.status).map((transition) =>
        renderItem({
          key: transition.key,
          label: t(`action_${transition.key}`),
          // The menu has closed by the time this settles and no chip shows the unchanged status: say it failed.
          onSelect: () => void change(transition.to).then((ok) => { if (!ok) toast({ kind: "error", text: t("change_failed") }); }),
        }),
      )}
    </>
  );
}
