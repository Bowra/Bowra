"use client";

import { useEffect, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { fetchSessionDetail } from "./api";
import { apiFetch, readApiError } from "./api-client";
import { createComposerChatAction, isDefinitiveComposerChatActionRejection, lookupComposerChatAction } from "./composer-chat-action";
import { composerDraftScopeSignature } from "./composer-persistence-scope";
import type { ComposerPersistenceScope } from "./composer-persistence-scope";
import {
  clearContextMaintenanceJournal, prepareContextMaintenanceJournal, readContextMaintenanceJournal,
  type PendingContextMaintenance,
} from "./composer-submission-recovery";
import { requireMaintenanceReceipt, type ContextMaintenanceReceipt } from "./context-maintenance";
import { getChatSendErrorMessage } from "./chat-send-errors";
import { captureComposerSubmissionWireSnapshot, postSendMessage } from "./use-turn-runner";

async function lookup(scope: ComposerPersistenceScope, pending: PendingContextMaintenance): Promise<ContextMaintenanceReceipt> {
  if (pending.action) return requireMaintenanceReceipt(await lookupComposerChatAction(pending.action), pending.context, scope.requestedSessionId!);
  const query = new URLSearchParams({ workspace_id: scope.workspaceId, requested_session_id: scope.requestedSessionId! });
  const response = await apiFetch(`/api/v1/chat/submissions/${encodeURIComponent(pending.journalId)}?${query}`, { cache: "no-store" });
  if (!response.ok) throw await readApiError(response);
  const receipt = await response.json();
  if (receipt.outcome === "rejected") throw Object.assign(new Error("Maintenance request was rejected"), { maintenanceRejected: true });
  if (receipt.outcome !== "started") throw new Error("Maintenance request is not admitted");
  return requireMaintenanceReceipt(receipt.response, pending.context, scope.requestedSessionId!);
}

/** The menu owns maintenance recovery; it never settles or sends the editable Composer. */
export function useContextMaintenance({ scope, blocked, attach, onMessage }: {
  scope: ComposerPersistenceScope | null;
  blocked: boolean;
  attach?: (receipt: ContextMaintenanceReceipt, existingOnly?: boolean) => boolean;
  onMessage: (message: string) => void;
}) {
  const t = useTranslations("chat");
  const key = composerDraftScopeSignature(scope);
  const current = useRef({ scope, key, blocked, attach, onMessage });
  current.current = { scope, key, blocked, attach, onMessage };
  const lock = useRef(false);
  const mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  const [state, setState] = useState<{ key: string; pending: boolean; busy: boolean }>({ key, pending: false, busy: false });

  const update = (scopeKey: string, pending: boolean, busy = false) => {
    if (mounted.current && current.current.key === scopeKey) setState({ key: scopeKey, pending, busy });
  };
  const report = (scopeKey: string, message: string) => {
    if (mounted.current && current.current.key === scopeKey) current.current.onMessage(message);
  };
  const adopt = async (captured: ComposerPersistenceScope, scopeKey: string, pending: PendingContextMaintenance, receipt: ContextMaintenanceReceipt) => {
    if (!mounted.current || current.current.key !== scopeKey) return false;
    const accepted = current.current.blocked ? current.current.attach?.(receipt, true) : current.current.attach?.(receipt);
    if (!accepted) return false;
    await clearContextMaintenanceJournal(captured, pending.journalId);
    update(scopeKey, false);
    return true;
  };

  useEffect(() => {
    if (!scope?.requestedSessionId) return;
    const captured = { ...scope };
    let active = true;
    void (async () => {
      try {
        const pending = await readContextMaintenanceJournal(captured);
        if (!active || current.current.key !== key) return;
        update(key, Boolean(pending));
        if (!pending) return;
        try {
          const receipt = await lookup(captured, pending);
          if (active) await adopt(captured, key, pending, receipt);
        }
        catch { report(key, t("compact_context_pending")); }
      } catch { if (active) { update(key, true); report(key, t("compact_context_restore_failed")); } }
    })();
    return () => { active = false; };
    // A new authenticated scope owns its own journal; ordinary rerenders never retry a request.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);

  const run = async () => {
    const selected = current.current;
    if (lock.current || selected.blocked || !selected.scope?.requestedSessionId || !selected.attach) return;
    const captured = { ...selected.scope };
    const scopeKey = selected.key;
    lock.current = true;
    update(scopeKey, false, true);
    let pending: PendingContextMaintenance | null = null;
    try {
      pending = await readContextMaintenanceJournal(captured);
      if (pending) {
        try { await adopt(captured, scopeKey, pending, await lookup(captured, pending)); return; }
        catch (error) {
          if ((error as { maintenanceRejected?: boolean }).maintenanceRejected) {
            await clearContextMaintenanceJournal(captured, pending.journalId); pending = null;
            report(scopeKey, t("compact_context_rejected")); return;
          }
          const missing = (error as { status?: number }).status === 404;
          if (!missing || !pending.draft) { report(scopeKey, t(pending.draft ? "compact_context_pending" : "compact_context_restore_failed")); return; }
          // Only this explicit menu action may resend the original readable request after lookup.
        }
      } else {
        const detail = await fetchSessionDetail(captured.requestedSessionId!);
        if (!mounted.current || current.current.key !== scopeKey || current.current.blocked) return;
        if (!detail.context_generation && !detail.turns.some((turn) => turn.operation_kind !== "context_maintenance")) { report(scopeKey, t("compact_context_unavailable")); return; }
        if (detail.turns.some((turn) => ["running", "clarification_pending", "approval_pending", "awaiting_user", "awaiting_approval"].includes(turn.status))) {
          report(scopeKey, t("compact_context_busy")); return;
        }
        const protocol = detail.chat_execution.write_protocol;
        if (protocol !== "terminal_v1" && protocol !== "legacy_v0") { report(scopeKey, t("compact_context_unavailable")); return; }
        const context = { contract: "context-maintenance.v1" as const, expected_generation: detail.context_generation ?? null };
        pending = await prepareContextMaintenanceJournal(captured, {
          message: "", attachments: [], wireSnapshot: {
            ...captureComposerSubmissionWireSnapshot(captured.requestedSessionId, false, null), planningMode: "off", contextMaintenance: context,
          },
          ...(protocol === "terminal_v1" ? { terminalAction: createComposerChatAction({ actionKind: "start", sourceRevision: null, sessionId: captured.requestedSessionId }) } : {}),
        });
      }
      if (!mounted.current || current.current.key !== scopeKey || current.current.blocked || !pending?.draft) return;
      const receipt = await postSendMessage(captured.workspaceId, "", "", captured.requestedSessionId, [],
        pending.action ? null : pending.journalId, null, pending.draft.wireSnapshot!, pending.action);
      await adopt(captured, scopeKey, pending, requireMaintenanceReceipt(receipt, pending.context, captured.requestedSessionId!));
    } catch (error) {
      if (pending && isDefinitiveComposerChatActionRejection(error, pending.action ?? undefined)) {
        await clearContextMaintenanceJournal(captured, pending.journalId);
        pending = null;
        report(scopeKey, getChatSendErrorMessage((error as { code?: string }).code));
      } else report(scopeKey, t("compact_context_pending"));
    } finally {
      lock.current = false;
      try { update(scopeKey, Boolean(await readContextMaintenanceJournal(captured))); }
      catch { update(scopeKey, true); }
    }
  };
  return { run, busy: state.key === key && state.busy, pending: state.key === key && state.pending };
}
