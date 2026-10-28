"use client";

import { useEffect, useState, useSyncExternalStore } from "react";
import { useTranslations } from "next-intl";
import { LiteConfirm, type LiteConfirmState } from "@/components/config-copilot/lite-confirm";
import { Button } from "@/components/ui/button";
import { Notice } from "@/components/ui/notice";
import { useWorkspaceSettingsCanvasBridge } from "./workspace-settings-canvas-context";
import { getLiteProposals, getLiteServerState, hydrateLiteState, ignoreLiteProposal, isLiteBaselineConflict, liteVersion, subscribeLite } from "@/lib/config-copilot-lite";
import { WorkspaceSettingsStageError, type StagedWorkspaceSettingsSuggestion } from "@/lib/workspace-settings-agent";
import type { ConfigLiteProposePayload } from "@/lib/types";

const noSubscribe = () => () => {};
const zero = () => 0;

/** Both surfaces use the same original-controller review/action path. */
export function WorkspaceSettingsSuggestion({ proposal }: { proposal: ConfigLiteProposePayload }) {
  const t = useTranslations("settings_canvas");
  const labels = useTranslations("settings.config_copilot_lite");
  const bridge = useWorkspaceSettingsCanvasBridge();
  const revision = useSyncExternalStore(bridge?.subscribe ?? noSubscribe, bridge?.getSnapshot ?? zero, zero);
  useSyncExternalStore(subscribeLite, liteVersion, liteVersion);
  const [review, setReview] = useState<StagedWorkspaceSettingsSuggestion | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [conflict, setConflict] = useState(false);
  const [applyFailed, setApplyFailed] = useState(false);
  const status = getLiteServerState(proposal.proposal_id);

  useEffect(() => {
    let cancelled = false;
    void hydrateLiteState(proposal.proposal_id);
    if (!bridge?.hasOriginal(proposal)) {
      setReview(null);
      let failure = "page_not_ready";
      try { bridge?.assertOriginal(proposal); }
      catch (reason) { if (reason instanceof WorkspaceSettingsStageError) failure = reason.code; }
      setError((current) => failure === "local_changes" ? failure : current ?? failure);
      return;
    }
    setError(null);
    void bridge.stage(proposal).then((value) => {
      if (!cancelled) setReview(value);
    }).catch((reason: unknown) => {
      if (!cancelled) setError(reason instanceof WorkspaceSettingsStageError ? reason.code : "review_unavailable");
    });
    return () => { cancelled = true; };
  }, [bridge, revision, proposal]);

  const run = async (op: "apply" | "undo") => {
    if (!bridge || busy || !review) return;
    setBusy(true);
    setError(null);
    setConflict(false);
    setApplyFailed(false);
    try {
      await bridge.perform(proposal, op);
    } catch (reason) {
      const baseline = isLiteBaselineConflict(reason);
      const code = reason instanceof WorkspaceSettingsStageError ? reason.code : "action_unavailable";
      setConflict(baseline);
      // A failed apply becomes the card's “Couldn't apply” state, whose Retry re-applies the same suggestion.
      setApplyFailed(!baseline && op === "apply" && code === "action_unavailable");
      setError(code);
    } finally { setBusy(false); }
  };

  const reviewSaved = async () => {
    if (!bridge || busy) return;
    setBusy(true);
    setError(null);
    try { setReview(await bridge.reviewSaved(proposal)); }
    catch (reason) { setError(reason instanceof WorkspaceSettingsStageError ? reason.code : "review_unavailable"); }
    finally { setBusy(false); }
  };

  if (!review) return <>
    {status && status !== "proposed" ? <LiteConfirm state={status} rows={[]}
      noUndoNote={proposal.form_id === "workspace_metadata" ? labels("no_undo") : undefined} /> : null}
    <Notice tone={error ? "muted" : "info"}>{t(error === "local_changes" ? "local_changes" : error === "baseline_changed" ? "review_stale" : error ? "review_unavailable" : "review_loading")}</Notice>
    {(!status || status === "proposed" || (status === "applied" && proposal.form_id === "workspace_i18n")) && bridge?.canReviewSaved(proposal) ?
      <Button variant="secondary" disabled={busy} onClick={() => void reviewSaved()}>{t("review_saved")}</Button> : null}
  </>;
  const localize = (key: string, fallback: string) => labels.has(key) ? labels(key) : fallback;
  const actionPending = Boolean(busy || bridge?.isActionPending(proposal.proposal_id));
  const reviewedStatus = status && status !== "proposed" ? status : review.status;
  // A field without a display name can be neither shown nor silently applied (see LiteConfirmConnected).
  const unreviewable = review.proposal.rows.some((row) => !labels.has(`field.${row.field}`));
  const state: LiteConfirmState = conflict ? "conflict" : reviewedStatus !== "proposed" ? reviewedStatus
    : unreviewable ? "stale" : actionPending ? "busy" : applyFailed ? "fail" : "propose";
  // Name and description edits cannot be restored by the suggestion service: no Undo, explained instead (D11).
  const restorable = proposal.form_id !== "workspace_metadata";
  return <div className="ui-card-stack">
    {error && !conflict && !applyFailed ? <Notice tone="error">{t(error === "local_changes" ? "local_changes" : error === "baseline_changed" ? "review_stale" : "action_unavailable")}</Notice> : null}
    <LiteConfirm
      state={state}
      rows={review.proposal.rows.filter((row) => labels.has(`field.${row.field}`)).map((row) => ({
        label: labels(`field.${row.field}`),
        from: row.from === null ? null : localize(`value.${row.field}.${row.from}`, String(row.from)),
        to: localize(`value.${row.field}.${row.to}`, String(row.to)),
      }))}
      onApply={() => void run("apply")}
      onUndo={restorable ? () => void run("undo") : undefined}
      undoBusy={actionPending}
      noUndoNote={restorable ? undefined : labels("no_undo")}
      onRetry={() => void run("apply")}
      onIgnore={reviewedStatus === "applied" ? undefined : () => ignoreLiteProposal(proposal.proposal_id)}
    />
  </div>;
}

export function WorkspaceSettingsCanvasSuggestions({ controllerId }: { controllerId: string }) {
  const bridge = useWorkspaceSettingsCanvasBridge();
  useSyncExternalStore(subscribeLite, liteVersion, liteVersion);
  useSyncExternalStore(bridge?.subscribe ?? noSubscribe, bridge?.getSnapshot ?? zero, zero);
  const proposals = getLiteProposals().filter((proposal) => bridge?.routesTo(proposal, controllerId));
  return <>{proposals.map((proposal) => <WorkspaceSettingsSuggestion key={proposal.proposal_id} proposal={proposal} />)}</>;
}
