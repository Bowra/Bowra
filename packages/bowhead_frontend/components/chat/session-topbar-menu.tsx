"use client";

/**
 * Session "⋯" menu in the conversation title bar (Chat Cards Spec §10, D12; docs/design/menus.en-US.md).
 *
 * It carries the same items, labels and order as the sidebar session row menu — Rename…, Pin / Unpin,
 * Archive Session, a separator and Delete Session — with the same feedback: a past-tense toast with an
 * eight-second Undo, and the running-session reason when the action is unavailable. The title bar adds only
 * "Copy Diagnostic Information" after the shared non-destructive items: identifying the session and turn is
 * needed only when reporting a problem. The copied text keeps the session and latest turn ids on the first line,
 * then the transport measurement line. After a successful copy the menu has closed, so a toast confirms it; when
 * the clipboard is unavailable the existing prompt fallback shows the text for manual copying.
 */

import { Archive, Copy, Pencil, Pin, Trash2 } from "lucide-react";
import { useRef } from "react";
import { useTranslations } from "next-intl";

import { LucideGlyph } from "@/components/icon-refresh";
import { SessionActionMenu, type SessionMenuAction } from "@/components/sidebar/session-action-menu";
import { truncateSessionTitle, useSessionLifecycleErrorText } from "@/components/sidebar/work-recovery";
import { SessionArchiveError } from "@/lib/api";
import { copyTextWithFallback } from "@/lib/clipboard";
import { notifySessionListChanged } from "@/lib/session-list-events";
import { trackInteraction as track } from "@/lib/metrics";
import { describeTransport, type TransportSample } from "@/lib/transport-health";
import { toast } from "@/lib/use-toast";

/** Outcome of an archive/delete request: the page may cancel it (for example, unsaved canvas changes). */
export type SessionTopbarMutationResult = "success" | "cancelled";

export interface SessionTopbarMenuProps {
  sessionId: string;
  turnId?: string | null;
  /** This turn's SSE transport measurement, appended to the copied text. */
  transport?: TransportSample | null;
  /** Session title, used in the menu's accessible name and the Undo toasts. */
  title?: string;
  /** Current pin state when the session is listed; omit to leave Pin out. */
  pinned?: boolean;
  onTogglePin?: () => void | Promise<unknown>;
  onArchive?: () => SessionTopbarMutationResult | Promise<SessionTopbarMutationResult>;
  /** Undo for Archive; without it the toast offers no Undo. */
  onRestore?: () => void | Promise<unknown>;
  /** A running session cannot be archived; the reason is read with the disabled item. */
  archiveDisabledReason?: string | null;
  /** Soft-delete the session: no confirmation, eight-second Undo (same as the session row). */
  onDelete?: () => SessionTopbarMutationResult | Promise<SessionTopbarMutationResult>;
  /** Undo for Delete; without it the toast offers no Undo. */
  onUndelete?: () => void | Promise<unknown>;
  /** A running session cannot be deleted; the reason is read with the disabled item. */
  deleteDisabledReason?: string | null;
  /** Opens the rename dialog (owned by the page); omit to leave Rename out. */
  onRename?: () => void;
}

/** The copied diagnostic text: ids first (they matter most), transport measurement second. */
export function sessionDiagnosticText(sessionId: string, turnId?: string | null, transport?: TransportSample | null): string {
  const ids = turnId ? `${sessionId} · ${turnId}` : sessionId;
  return transport ? `${ids}\n${describeTransport(transport)}` : ids;
}

export function SessionTopbarMenu({
  sessionId,
  turnId,
  transport,
  title,
  pinned,
  onTogglePin,
  onArchive,
  onRestore,
  archiveDisabledReason,
  onDelete,
  onUndelete,
  deleteDisabledReason,
  onRename,
}: SessionTopbarMenuProps) {
  const t = useTranslations("chat");
  const tCard = useTranslations("chat_card");
  const tSession = useTranslations("session");
  const shortTitle = truncateSessionTitle(title ?? "");
  const lifecycleErrorText = useSessionLifecycleErrorText();
  const inFlightRef = useRef(false);

  const copy = async () => {
    const ok = await copyTextWithFallback(sessionDiagnosticText(sessionId, turnId, transport), tCard("copy_fallback_prompt"));
    if (ok) toast({ kind: "success", text: tCard("copied") });
  };

  // Same feedback as the sidebar row (components/sidebar/work-recovery.tsx): past-tense toast with an eight-second
  // Undo, the Gateway's lifecycle reason on refusal, the same action telemetry (surface "topbar"), and one
  // request at a time.
  const runLifecycle = async (kind: "archive" | "delete") => {
    const run = kind === "archive" ? onArchive : onDelete;
    const undo = kind === "archive" ? onRestore : onUndelete;
    if (!run || inFlightRef.current) return;
    inFlightRef.current = true;
    try {
      if ((await run()) === "cancelled") {
        track("bowhead_sidebar_session_action_total", { action: kind, outcome: "blocked", surface: "topbar" });
        return;
      }
    } catch (error) {
      notifySessionListChanged();
      const running = error instanceof SessionArchiveError && error.code === `session_${kind}_active_turn`;
      track("bowhead_sidebar_session_action_total", { action: kind, outcome: running ? "blocked" : "error", surface: "topbar" });
      toast({
        kind: "error",
        text: lifecycleErrorText(error, tSession(running ? `rail_${kind}_running_error` : `rail_${kind}_failed`)),
      });
      return;
    } finally {
      inFlightRef.current = false;
    }
    track("bowhead_sidebar_session_action_total", { action: kind, outcome: "success", surface: "topbar" });
    toast({
      kind: "success",
      undo: true,
      text: tSession(`rail_${kind}_done`, { title: shortTitle }),
      durationMs: 8_000,
      action: undo
        ? {
            label: tSession(`rail_${kind}_undo`),
            onAction: async () => {
              try {
                await undo();
                track("bowhead_sidebar_session_action_total", { action: "undo", outcome: "success", surface: "topbar" });
              } catch (error) {
                notifySessionListChanged();
                track("bowhead_sidebar_session_action_total", { action: "undo", outcome: "error", surface: "topbar" });
                toast({
                  kind: "error",
                  text: lifecycleErrorText(error, tSession(kind === "archive" ? "rail_restore_failed" : "rail_delete_undo_failed")),
                });
                throw error;
              }
            },
          }
        : undefined,
    });
  };
  const archive = () => runLifecycle("archive");
  const remove = () => runLifecycle("delete");

  const actions: SessionMenuAction[] = [
    ...(onRename
      ? [{
          id: "rename",
          label: tSession("rail_rename_title"),
          icon: <LucideGlyph icon={Pencil} size={16} aria-hidden="true" />,
          opensDialog: true,
          onSelect: onRename,
        }]
      : []),
    ...(onTogglePin && pinned !== undefined
      ? [{
          id: "pin",
          label: pinned ? tSession("rail_unpin_title") : tSession("rail_pin_title"),
          icon: <LucideGlyph icon={Pin} size={16} aria-hidden="true" />,
          onSelect: () => void onTogglePin(),
        }]
      : []),
    ...(onArchive
      ? [{
          id: "archive",
          label: tSession("rail_archive_title"),
          icon: <LucideGlyph icon={Archive} size={16} aria-hidden="true" />,
          disabled: Boolean(archiveDisabledReason),
          title: archiveDisabledReason ?? undefined,
          onSelect: () => void archive(),
        }]
      : []),
    {
      id: "copy-diagnostics",
      label: tCard("copy_diagnostics_menu"),
      icon: <LucideGlyph icon={Copy} size={16} aria-hidden="true" />,
      onSelect: () => void copy(),
    },
    // The destructive item comes last behind a separator. Delete is undoable, so it has no "…" and no confirmation.
    ...(onDelete
      ? [{
          id: "delete",
          label: tSession("rail_delete_title"),
          icon: <LucideGlyph icon={Trash2} size={16} aria-hidden="true" />,
          danger: true,
          disabled: Boolean(deleteDisabledReason),
          title: deleteDisabledReason ?? undefined,
          onSelect: () => void remove(),
        }]
      : []),
  ];

  return (
    <SessionActionMenu
      label={shortTitle ? tSession("rail_more_actions_for", { title: shortTitle }) : t("session_menu_label")}
      actions={actions}
      wrapperClassName="session-topbar-menu"
      triggerClassName="session-topbar-menu__trigger"
    />
  );
}
