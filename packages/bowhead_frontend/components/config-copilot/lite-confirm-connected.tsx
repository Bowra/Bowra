"use client";

/** Lite-confirm wiring (batch 0.5 R7 → batch 1-a server state, tech-design-batch1 §8).
 *
 * Handles proposal-store subscription and transient baseline-conflict feedback,
 * row localization (settings.config_copilot_lite.field/value, unmapped falls back to original text), mount-time
 * server-terminal hydration, and apply/undo/ignore POST forwarding. busy/fail are transient component states.
 */

import { useTranslations } from "next-intl";
import { useCallback, useEffect, useState, useSyncExternalStore } from "react";

import { LiteConfirm, type LiteConfirmState } from "@/components/config-copilot/lite-confirm";
import { WorkspaceSettingsSuggestion } from "@/components/canvas/workspace-settings-suggestion";
import { SpaceInstructionsDiff } from "@/components/settings/space-instructions";
import { useWorkspaceSettingsCanvasBridge } from "@/components/canvas/workspace-settings-canvas-context";
import { ApiError } from "@/lib/api-client";
import { configurationValueText } from "@/lib/configuration-values";
import { useAppRouter } from "@/lib/use-app-router";
import { teamSettingsPath } from "@/lib/team-settings-paths";
import { configurationCanvasOpen } from "@/lib/configuration-canvas-events";
import {
  getLiteProposal,
  getLiteServerState,
  hydrateLiteState,
  ignoreLiteProposal,
  isLiteBaselineConflict,
  isLiteFormUnavailable,
  liteVersion,
  performLiteAction,
  subscribeLite,
} from "@/lib/config-copilot-lite";

export function LiteConfirmConnected({ proposalId }: { proposalId: string }) {
  const t = useTranslations("settings.config_copilot_lite");
  const router = useAppRouter();
  const bridge = useWorkspaceSettingsCanvasBridge();
  useSyncExternalStore(bridge?.subscribe ?? (() => () => {}), bridge?.getSnapshot ?? (() => 0), () => 0);
  useSyncExternalStore(subscribeLite, liteVersion, liteVersion);
  const proposal = getLiteProposal(proposalId);
  const serverState = getLiteServerState(proposalId);
  const [busy, setBusy] = useState(false);
  const [failedOp, setFailedOp] = useState<"apply" | "undo" | null>(null);
  const [baselineConflict, setBaselineConflict] = useState(false);
  const [forbidden, setForbidden] = useState(false);
  const [unavailable, setUnavailable] = useState(false);

  useEffect(() => {
    // Terminal hydration (library handles memory-cache hits/in-flight deduplication; network errors leave propose retryable).
    void hydrateLiteState(proposalId);
  }, [proposalId]);

  const run = useCallback(
    (op: "apply" | "undo") => {
      const current = getLiteProposal(proposalId);
      if (!current) return;
      setBusy(true);
      setFailedOp(null);
      setBaselineConflict(false);
      setForbidden(false);
      setUnavailable(false);
      void performLiteAction(current, op)
        .catch((error: unknown) => {
          setFailedOp(op);
          setBaselineConflict(isLiteBaselineConflict(error));
          setForbidden(error instanceof ApiError && error.status === 403);
          setUnavailable(isLiteFormUnavailable(error));
        })
        .finally(() => setBusy(false));
    },
    [proposalId],
  );

  if (!proposal) return null;
  if (proposal.settings_target || (["workspace_i18n", "workspace_metadata"].includes(proposal.form_id)
    && (bridge?.isToolTurn(proposal.turn_id) || configurationCanvasOpen(proposal.turn_id))))
    return <WorkspaceSettingsSuggestion proposal={proposal} />;

  if (proposal.form_id === "space_instructions") {
    // One long free text: the card shows its line diff and saves a new version; earlier versions restore it.
    const row = proposal.rows.find((item) => item.field === "content" && typeof item.to === "string" && (item.from === null || typeof item.from === "string"));
    const space = proposal.workspace_name ?? "";
    const terminal = serverState && serverState !== "proposed" ? serverState : null;
    const state: LiteConfirmState =
      baselineConflict && serverState === "proposed"
        ? "conflict"
        : (terminal ?? (!row || proposal.rows.length !== 1 ? "stale" : failedOp === "apply" ? "fail" : busy ? "busy" : "propose"));
    return (
      <LiteConfirm
        state={state}
        rows={row ? [{ label: t("space_instructions_label"), from: String(row.from ?? ""), to: String(row.to) }] : []}
        title={space ? t("space_instructions_title", { space }) : t("space_instructions_title_current")}
        preview={row ? <SpaceInstructionsDiff before={String(row.from ?? "")} after={String(row.to)} /> : undefined}
        appliedSummary={space ? t("space_instructions_applied", { space }) : t("space_instructions_applied_current")}
        onApply={() => run("apply")}
        onIgnore={serverState === "applied" ? undefined : () => ignoreLiteProposal(proposal.proposal_id)}
        noUndoNote={t("space_instructions_no_undo")}
        // Retrying cannot help when the server refused the permission; say who can change the instructions instead.
        failBody={forbidden ? t("space_instructions_forbidden") : undefined}
        onRetry={forbidden || unavailable ? undefined : () => run("apply")}
      />
    );
  }

  const localize = (key: string, fallback: string): string => (t.has(key) ? t(key) : fallback);
  // Workspace-domain cards include the target space name (review P2-9): payload.workspace_id fixes the apply
  // target, so multi-space users reviewing from another space can still identify which space changes.
  const labelFor = (field: string, fallback?: string): string => {
    const label = localize(`field.${field}`, fallback ?? field);
    return proposal.workspace_name ? `${label}（${proposal.workspace_name}）` : label;
  };
  // A field without a display name can be neither shown (never a raw key) nor silently applied: such a
  // suggestion is shown as no longer available. Free-text values show as written.
  const unreviewable = proposal.rows.some((row) => !t.has(`field.${row.field}`) && !row.label);
  const rows = proposal.rows.filter((row) => t.has(`field.${row.field}`) || row.label).map((row) => ({
    label: labelFor(row.field, row.label),
    from: row.from == null && row.from_unset ? null : localize(`value.${row.field}.${String(row.from)}`, configurationValueText(row.from)),
    to: row.operation === "reset" ? t("reset_value", { value: localize(`value.${row.field}.${String(row.to)}`, configurationValueText(row.to)) }) : localize(`value.${row.field}.${String(row.to)}`, configurationValueText(row.to)),
  }));

  // The fail state means apply failed (“Couldn’t apply”); not for undo failures: stay applied,
  // allowing another Undo click (server replay guarantees idempotency). Stay applied during undo too.
  const terminal = serverState && serverState !== "proposed" ? serverState : null;
  const state: LiteConfirmState =
    baselineConflict && (serverState === "proposed" || serverState === "applied")
      ? "conflict"
      : (terminal ?? (unreviewable ? "stale" : failedOp === "apply" ? "fail" : busy ? "busy" : "propose"));
  return (
    <LiteConfirm
      state={state}
      rows={rows}
      applyLabel={proposal.form_id === "team_model_config" ? t("review_models") : undefined}
      onApply={() => proposal.form_id === "team_model_config"
        ? router.push(`${teamSettingsPath("llm")}?configuration_proposal=${encodeURIComponent(proposal.proposal_id)}`)
        : run("apply")}
      onIgnore={serverState === "applied" ? undefined : () => ignoreLiteProposal(proposal.proposal_id)}
      onUndo={proposal.form_id === "workspace_metadata" || proposal.form_id.startsWith("team_") ? undefined : () => run("undo")}
      undoBusy={busy}
      noUndoNote={proposal.form_id === "workspace_metadata" || proposal.form_id.startsWith("team_") ? t("no_undo") : undefined}
      failBody={unavailable ? t("form_unavailable") : forbidden && proposal.form_id.startsWith("team_") ? t("team_forbidden") : undefined}
      onRetry={forbidden || unavailable ? undefined : () => run("apply")}
    />
  );
}
