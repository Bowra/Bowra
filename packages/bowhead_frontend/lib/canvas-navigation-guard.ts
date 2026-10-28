"use client";

import { reportSaveFailureMessage } from "./report-save-errors";
import { requestConfirm } from "./confirm-dialog-store";
import { staticTranslator } from "./locale-messages";
import { toast } from "./use-toast";
import { BEFORE_APP_NAVIGATION_EVENT, type AppNavigationDetail } from "./use-app-router";

type CanvasNavigationGuard = {
  hasUnsaved: () => boolean;
  flush: () => Promise<{ ok: boolean; message?: string } | void> | void;
};
type SubagentDraftNavigationGuard = {
  confirmLeave: () => Promise<boolean>;
  hasUnsaved?: () => boolean;
  cancelLeave?: () => void;
};

let activeGuard: CanvasNavigationGuard | null = null;
const draftGuards = new Set<SubagentDraftNavigationGuard>();
let navigationIntent = 0;
let listening = false;
let reportConfirmation: AbortController | null = null;

const guardRoute = (event: Event) => {
  cancelPendingNavigation();
  if (!hasCanvasUnsavedChanges()) return;
  event.preventDefault();
  const { proceed } = (event as CustomEvent<AppNavigationDetail>).detail;
  void confirmCanvasNavigation().then((allowed) => { if (allowed) proceed(); });
};

function cancelPendingNavigation() {
  navigationIntent += 1;
  reportConfirmation?.abort();
  for (const guard of draftGuards) guard.cancelLeave?.();
}
function syncRouteListener() {
  if (typeof window === "undefined") return;
  const needed = draftGuards.size > 0 || activeGuard !== null;
  if (needed === listening) return;
  listening = needed;
  if (needed) window.addEventListener(BEFORE_APP_NAVIGATION_EVENT, guardRoute);
  else window.removeEventListener(BEFORE_APP_NAVIGATION_EVENT, guardRoute);
}

export function registerSubagentDraftNavigationGuard(guard: SubagentDraftNavigationGuard): () => void {
  draftGuards.add(guard);
  syncRouteListener();
  return () => {
    draftGuards.delete(guard);
    guard.cancelLeave?.();
    syncRouteListener();
  };
}

export function registerCanvasNavigationGuard(guard: CanvasNavigationGuard): () => void {
  activeGuard = guard;
  syncRouteListener();
  return () => {
    if (activeGuard === guard) activeGuard = null;
    syncRouteListener();
  };
}

export function hasCanvasUnsavedChanges(): boolean {
  return Boolean(activeGuard?.hasUnsaved()) || [...draftGuards].some((guard) => guard.hasUnsaved?.() ?? true);
}

export async function confirmCanvasNavigation(
  message?: string,
): Promise<boolean> {
  cancelPendingNavigation();
  const intent = navigationIntent;
  const participants = [...draftGuards];
  for (const guard of participants) {
    if (!draftGuards.has(guard)) return false;
    if (!(await guard.confirmLeave()) || intent !== navigationIntent) return false;
  }
  // An approval never covers reopened/new participants or input typed while another prompt waited.
  if (participants.some((guard) => !draftGuards.has(guard) || guard.hasUnsaved?.())
    || [...draftGuards].some((guard) => !participants.includes(guard))) return false;
  if (!activeGuard?.hasUnsaved()) return true;
  const reportGuard = activeGuard;
  if (typeof window === "undefined") return true;
  // Called outside the tree before route changes; read canvas_navigation_guard messages in the UI language.
  const t = staticTranslator("canvas_navigation_guard");
  const controller = new AbortController();
  reportConfirmation = controller;
  const ok = await requestConfirm({
    title: t("title"),
    description: message ?? t("description"),
    confirmLabel: t("confirm"),
    cancelLabel: t("cancel"),
    tone: "primary",
  }, controller.signal);
  if (!ok || intent !== navigationIntent || activeGuard !== reportGuard) return false;
  try {
    const result = await reportGuard.flush();
    if (result && !result.ok) {
      toast({ kind: "error", text: t("save_failed", { detail: result.message ?? t("unknown_error") }) });
      return false;
    }
    return intent === navigationIntent && activeGuard === reportGuard
      && !reportGuard.hasUnsaved()
      && participants.every((guard) => draftGuards.has(guard) && !guard.hasUnsaved?.())
      && [...draftGuards].every((guard) => participants.includes(guard));
  } catch (e) {
    const detail = reportSaveFailureMessage(e);
    toast({ kind: "error", text: t("save_failed", { detail }) });
    return false;
  }
}
