/**
 * AgentBlock — the atom of the multi-agent trace (§6 of block-model-spec).
 * A ledger section: header (badge + name + task + time + status + chevron),
 * a one-line summary when folded, and the nested step trace when expanded.
 * Four statuses: done / run / bail (amber, Lead recovered) / fail (red, peer
 * died).
 * Identity = colour + icon badge + the one localized display name (no second English role label: on 116 it
 * read "Lead Lead" in English and repeated a name that disagreed with the Marketplace in other languages).
 */
import { type ReactNode, useEffect, useId, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { ErrorDiagnosticsDisclosure } from "@/components/error-notice";
import type { ErrorDiagnostics } from "@/lib/error-presentation";
import { AgentBadge } from "./AgentBadge";
import { LEAD_BRAND_NAME, agentDisplayName, agentKeyFor, agentMetaFor } from "./agent-meta";
import { Chevron, StatusGlyph, type StepStatus } from "./block-primitives";
import { useBandOwnsLiveStatus } from "./turn-running";
import { CharIcon } from "@/components/char-icon";

export interface AgentBlockProps {
  /** backend agent_name (compute / financial-analyst / custom…). */
  agent: string;
  /** Configured Agent name captured at spawn time. */
  displayName?: string;
  task?: ReactNode;
  time?: string;
  status?: StepStatus;
  /** one-line summary shown while folded. */
  summary?: ReactNode;
  /** Localized terminal reason. Visible both folded and expanded. */
  outcomeReason?: string;
  /** Authenticated finite facts shown only in the expanded peer block. */
  outcomeDiagnostics?: ErrorDiagnostics;
  /** which agent recovered a bailed peer (default: lead). */
  bailTo?: string;
  /** start expanded; defaults to expanded while running. */
  open?: boolean;
  children?: ReactNode;
}

export function AgentBlock({
  agent,
  displayName,
  task,
  time,
  status = "done",
  summary,
  outcomeReason,
  outcomeDiagnostics,
  bailTo = "lead",
  open: openProp,
  children,
}: AgentBlockProps) {
  const meta = agentMetaFor(agent);
  const label = agentDisplayName(agent, displayName);
  const t = useTranslations("step_list");
  // chat-ux-2026-08 batch B: `useState` initializer runs once; peer blocks mounted during run
  // remain **open forever** after done. `Step.tsx` already fixed this same latch with an effect
  // (see its UX-1 comment about one-time useState initialization); AgentBlock was missed,
  // leaving every collaborating Agent's full ledger open throughout a 270-second turn.
  // Follow Step's pattern: effect for state transitions; never auto-toggle after the user toggles manually.
  const [open, setOpen] = useState(openProp ?? status === "run");
  const userToggledRef = useRef(false);
  const prevStatusRef = useRef(status);
  useEffect(() => {
    const prev = prevStatusRef.current;
    prevStatusRef.current = status;
    if (prev === status) return;
    if (userToggledRef.current) return;
    // Collapse only the completed transition. fail / bail are unexpected terminal states; keep them open.
    if (prev === "run" && status === "done") setOpen(false);
  }, [status]);
  const bodyId = useId();
  // chat-ux-2026-08: while the running band is present, it exclusively owns who is running and on what.
  // On 116, band current row repeated this heading's task row / body「当前任务」text,
  // with a spinner in each place. Suppress **only those three live-progress items**; keep terminal facts and prose.
  const bandOwnsLiveStatus = useBandOwnsLiveStatus();
  const liveStatusMuted = bandOwnsLiveStatus && status === "run";
  const taskTitle = typeof task === "string" ? task : undefined;
  const showTaskDetail = !liveStatusMuted && !!taskTitle && taskTitle.length > 32;
  const foldedSummary = summary ?? outcomeReason;
  const stClass =
    status === "run" ? "is-run" : status === "bail" ? "is-bail" : status === "fail" ? "is-fail" : "";

  return (
    <div className={`agb ${meta.cls} ${stClass}`.trim()}>
      <button
        type="button"
        className="agb-head"
        onClick={() => {
          userToggledRef.current = true;
          setOpen((o) => !o);
        }}
        aria-expanded={open}
        aria-controls={bodyId}
      >
        <AgentBadge agent={agent} />
        <span className="agb-name">{label}</span>
        {task != null && !liveStatusMuted
          ? <span className="agb-task" title={taskTitle}>{task}</span>
          : <span style={{ flex: 1 }} />}
        {time && <span className="agb-time">{time}</span>}
        {liveStatusMuted ? null : <StatusGlyph status={status} />}
        <Chevron open={open} className="agb-chev" />
      </button>

      {!open && foldedSummary != null && <div className="agb-summary">{foldedSummary}</div>}
      {open && (
        <div className="agb-body" id={bodyId}>
          {showTaskDetail ? (
            <div className="agb-task-detail">
              <span>{t("agent_task_detail")}</span>
              <p>{taskTitle}</p>
            </div>
          ) : null}
          {outcomeReason ? (
            <div className="agb-outcome-reason">
              <span>{t("agent_outcome_reason")}</span>
              <p>{outcomeReason}</p>
            </div>
          ) : null}
          {outcomeDiagnostics ? (
            <ErrorDiagnosticsDisclosure
              diagnostics={outcomeDiagnostics}
              surface="chat"
            />
          ) : null}
          {children}
        </div>
      )}

      {status === "bail" && (
        <div className="bail-note">
          <StatusGlyph status="bail" />
          {t("agent_bail_note")}
          <span className="arr"><CharIcon ch="→" /></span>
          <span className="lp">
            <AgentBadge agent={bailTo} size={15} />
            <b>{agentKeyFor(bailTo) === "lead" ? LEAD_BRAND_NAME : agentDisplayName(bailTo)}</b> {t("agent_bail_recover")}
          </span>
        </div>
      )}
      {status === "fail" && (
        // O5 (2026-07-03): remove fake block-level peer retry/skip buttons (stub endpoints never actually rerun,
        // and buttons rendered even with handlers often undefined). Real intervention = composer steer.
        <div className="fail-note">
          <StatusGlyph status="fail" />
          {t("agent_fail_note")}
        </div>
      )}
    </div>
  );
}
