"use client";

import { useEffect, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { ListChecks, Lock, RefreshCw } from "@/components/icons";
import { ChatCard, type ChatCardIntent } from "@/components/ui/card";
import { LoadingSkeleton } from "@/components/ui/loading-skeleton";
import {
  CanvasObjectHeader,
  CanvasObjectSurface,
  CanvasObjectToolbar,
  ObjectStatusBadge,
  StateBlock,
  ToolbarAction,
  type CanvasObjectMarker,
  type CanvasObjectStatus,
} from "@/components/canvas/object";
import {
  readCoachOperation,
  record,
  type CoachOperationOpen,
  type CoachOperationReceipt,
} from "@/lib/coach-operations";
import type { CoachResourceReference } from "@/lib/coach-resource-editors";

const text = (value: unknown) =>
  typeof value === "string" ? value.slice(0, 4000) : "";
const statuses = new Set([
  "succeeded",
  "failed",
  "unknown",
  "pending",
  "accepted",
  "dismissed",
  "applied",
  "requested",
  "authorized",
  "cancelled",
  "revoked",
  "expired",
  "exhausted",
  "editing",
  "ready",
  "blocked",
  "consumed",
  "discarded",
]);

export function operationSummary(receipt: CoachOperationReceipt) {
  const result = receipt.domain_result;
  const proposal = record(result.proposal);
  const draft = record(result.draft);
  const definition = record(draft.definition);
  const target = record(result.result_target ?? result.original_target);
  const requirement = record(result.requirement);
  const status = text(
    receipt.operation_ref.kind === "tool_requirement"
      ? "requirement_recorded"
      : (result.outcome ??
          result.state ??
          proposal.status ??
          draft.draft_status ??
          record(result.delegation).state),
  );
  return {
    status:
      status === "open"
        ? "pending"
        : status === "requirement_recorded" || statuses.has(status)
          ? status
          : "unknown",
    // Display names only: Skill and draft `name` values are identifiers (slugs) and never become the title.
    title: text(definition.display_name ?? target.display_name ?? requirement.operation),
    summary: text(
      proposal.rationale ??
        record(proposal.payload).purpose ??
        draft.purpose ??
        requirement.reason,
    ),
    observation: result.observation
      ? record(result.observation).current_status === "available"
        ? "observation_available"
        : "observation_unavailable"
      : null,
  };
}

/** Where a receipt's card action goes: a hosted Skill / Plugin / Agent editor, or the original Agent draft. */
export type CoachOperationTarget =
  | { kind: "resource"; reference: CoachResourceReference }
  | { kind: "agent_draft"; draftId: string };

export type CoachOperationAction =
  | "open_skill"
  | "open_plugin"
  | "review_in_agent"
  | "view_in_agent"
  | "open_draft"
  | "review_in_draft";

const textId = (value: unknown) => typeof value === "string" && value.length > 0 && value.length <= 512 ? value : null;

/**
 * The resource a receipt is about, with the card action that opens it. Saved Skills and Plugins open their
 * hosted editor (the saved target, else the original one, so a failed save still opens what it tried to
 * change); Agent proposals, cases and evaluations open the Agent; draft receipts open the original draft.
 * Tool requirements have no hosted editor and offer no action.
 */
export function operationTarget(receipt: CoachOperationReceipt): { action: CoachOperationAction; target: CoachOperationTarget } | null {
  const ref = receipt.operation_ref;
  const result = receipt.domain_result;
  if (ref.kind === "skill_save") {
    const saved = record(record(result.result).target);
    const original = record(result.original_target);
    const target = textId(saved.id) && textId(saved.revision) ? saved : original;
    const id = textId(target.id);
    const revision = textId(target.revision);
    return id && revision ? { action: "open_skill", target: { kind: "resource", reference: { kind: "skill", source_kind: "user", id, revision } } } : null;
  }
  if (ref.kind === "plugin_save") {
    const target = record(result.result_target ?? result.original_target);
    const id = textId(target.id);
    const revision = target.revision == null ? null : textId(String(target.revision));
    return id && revision ? { action: "open_plugin", target: { kind: "resource", reference: { kind: "capability", id, revision } } } : null;
  }
  if (ref.kind === "agent_change" || ref.kind === "eval_cases" || ref.kind === "ci_delegation") {
    // Agent editors are opened by identity; the server reads the current working copy (no exact revision check),
    // so the reference carries a placeholder revision instead of a stale one.
    // Coach writes the draft directly: an accepted change is there to look at, not to review.
    const written = ref.kind === "agent_change" && record(result.proposal).status === "accepted";
    return {
      action: ref.kind === "ci_delegation" || written ? "view_in_agent" : "review_in_agent",
      target: { kind: "resource", reference: { kind: "agent", id: ref.agent_id, revision: "current" } },
    };
  }
  if (ref.kind === "draft_record" || ref.kind === "draft_suggestion") {
    return { action: ref.kind === "draft_record" ? "open_draft" : "review_in_draft", target: { kind: "agent_draft", draftId: ref.draft_id } };
  }
  return null;
}

type OperationIntent = Exclude<ChatCardIntent, "progress">;

/**
 * Name of the operation as its outcome allows: a success says what happened (“Skill saved”), a failure says what
 * did not (“Skill couldn't be saved”), anything else stays neutral (“Skill save”) — never claiming an outcome the
 * receipt does not show. Used for the tab, the title fallback and the card eyebrow.
 */
export function operationLabelKey(kind: CoachOperationOpen["reference"]["kind"], intent: OperationIntent | null): string {
  if (intent === "success") return `kinds.${kind}`;
  if (intent === "failure") return `failed_kinds.${kind}`;
  return `operation_names.${kind}`;
}

/** Card intent of a receipt status: saved / applied = success, awaiting you = input, failed = failure, the rest = info. */
export function operationIntent(status: string): OperationIntent {
  if (["succeeded", "applied", "accepted", "consumed", "requirement_recorded", "authorized", "ready"].includes(status)) return "success";
  if (["pending", "requested", "editing"].includes(status)) return "input";
  if (["failed", "blocked", "exhausted"].includes(status)) return "failure";
  return "info";
}

/** C11 badge tone and icon per intent; the word is the receipt's own status. */
const BADGE: Record<OperationIntent, CanvasObjectStatus> = {
  success: "published",
  input: "review",
  failure: "failed",
  info: "cancelled",
};
const MARKER: Partial<Record<OperationIntent, CanvasObjectMarker>> = {
  input: "attention",
  failure: "failed",
};

/**
 * Operation result (Canvas Objects Spec part 2 §10): a read-only receipt of what the configuration assistant did.
 * Toolbar: status badge + Refresh. Header: “Operation result”, the resource's display name (the operation name when
 * there is none) and the read-only note. Body: one ChatCard (success / input / failure / info). There is no
 * primary action and no in-body close: the tab × is the only close (D1).
 */
export function CoachOperationView({
  pointer,
  active,
  onMarker,
  onLabel,
  onOpen,
}: {
  pointer: CoachOperationOpen;
  active: boolean;
  /** Opens the resource the receipt is about (the card's secondary action); absent hides the action. */
  onOpen?: (target: CoachOperationTarget) => void;
  /** Tab marker for receipts that wait for the user or failed (C1). */
  onMarker?: (marker: { marker: CanvasObjectMarker; statusLabel: string | null } | null) => void;
  /** Tab name that follows the outcome (see operationLabelKey); null until the receipt has been read. */
  onLabel?: (label: string | null) => void;
}) {
  const t = useTranslations("coach_operation");
  const common = useTranslations("canvas_object");
  const [refresh, setRefresh] = useState(0);
  const [state, setState] = useState<{
    requestId: string;
    receipt: CoachOperationReceipt | null;
    failed: boolean;
  } | null>(null);
  useEffect(() => {
    setState(null);
    if (!active) return;
    const controller = new AbortController();
    void readCoachOperation(
      pointer.reference,
      pointer.workspaceId,
      pointer.sessionId,
      controller.signal,
      pointer.teamId,
    )
      .then((receipt) => {
        if (!controller.signal.aborted)
          setState({ requestId: pointer.requestId, receipt, failed: false });
      })
      .catch(() => {
        if (!controller.signal.aborted)
          setState({ requestId: pointer.requestId, receipt: null, failed: true });
      });
    return () => controller.abort();
  }, [pointer, refresh, active]);
  const kind = pointer.reference.kind;
  const current = active && state?.requestId === pointer.requestId ? state : null;
  const summary = current?.receipt ? operationSummary(current.receipt) : null;
  const intent = summary ? operationIntent(summary.status) : null;
  const statusLabel = summary ? t(`statuses.${summary.status}`) : null;
  const open = current?.receipt && onOpen ? operationTarget(current.receipt) : null;

  const label = t(operationLabelKey(kind, intent));
  const report = useRef({ onMarker, onLabel });
  report.current = { onMarker, onLabel };
  const marker = intent ? MARKER[intent] ?? null : null;
  // Only a completed read changes what the tab shows; an inactive tab keeps its last known marker and name.
  const settled = Boolean(current);
  const readFailed = Boolean(current?.failed);
  useEffect(() => {
    if (!settled) return;
    report.current.onMarker?.(marker ? { marker, statusLabel } : null);
    report.current.onLabel?.(readFailed ? null : label);
  }, [settled, readFailed, marker, statusLabel, label]);
  useEffect(() => () => {
    report.current.onMarker?.(null);
    report.current.onLabel?.(null);
  }, []);

  const titleId = `coach-operation-${pointer.requestId}-title`;
  const eyebrow = intent === "info" ? statusLabel : label;
  const lines = summary?.status === "unknown" ? [t("unknown_body")]
    : [summary?.summary, summary?.observation ? t(summary.observation) : null, t(`limits.${kind}`)];
  return (
    <CanvasObjectSurface
      labelledBy={summary ? titleId : undefined}
      label={summary ? undefined : label}
      toolbar={(
        <CanvasObjectToolbar
          status={intent && statusLabel ? <ObjectStatusBadge status={BADGE[intent]} label={statusLabel} /> : null}
          actions={(
            <ToolbarAction
              icon={<RefreshCw size={14} aria-hidden />}
              label={common("refresh")}
              disabled={active && !current}
              onClick={() => setRefresh((value) => value + 1)}
            />
          )}
        />
      )}
    >
      {current?.failed ? (
        <StateBlock icon={<Lock size={20} />} title={t("unavailable_title")} body={t("unavailable")} />
      ) : summary && intent ? (
        <>
          <CanvasObjectHeader
            titleId={titleId}
            icon={<ListChecks size={14} aria-hidden />}
            eyebrow={t("eyebrow")}
            title={summary.title || label}
            subtitle={t("read_only")}
          />
          <ChatCard
            intent={intent}
            eyebrow={eyebrow}
            data-testid="coach-operation-card"
            // Receipts carry no primary action: opening the resource is a secondary button (spec §10).
            secondary={open && onOpen ? [{ label: t(`actions.${open.action}`), variant: "secondary", onSelect: () => onOpen(open.target) }] : undefined}
          >
            {lines.filter(Boolean).map((line, index) => (
              <p key={index} className="ui-card__text">{line}</p>
            ))}
          </ChatCard>
        </>
      ) : (
        <LoadingSkeleton variant="form" rows={3} label={t("loading")} />
      )}
    </CanvasObjectSurface>
  );
}
