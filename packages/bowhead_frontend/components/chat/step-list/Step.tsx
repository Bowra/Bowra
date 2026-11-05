/**
 * Step — one tool-call row inside an AgentBlock's body (§6 of block-model-spec).
 * Status glyph + optional kind chip + tool name (icon-prefixed, verb form while
 * running) + label + right summary + chevron; expanded detail holds a tool-result
 * renderer (see tool-results.tsx). Class/token-driven (consumes `.ag-*` / `--c-*`):
 * this is the v2 replacement for the legacy inline-styled StepRow render path.
 *
 * Subagent/peer calls do NOT render as a Step — they become their own
 * <AgentBlock> through the production grouping transformer. Step is tool steps
 * only.
 */
import { type ReactNode, useEffect, useId, useRef, useState } from "react";
import { Chevron, StatusGlyph, type StepStatus } from "./block-primitives";
import { getToolMeta } from "./tool-catalog";
import { CharIcon } from "@/components/char-icon";

export interface StepProps {
  /** backend tool name — resolved via tool-catalog for icon + display label. */
  toolName?: string | null;
  /** free-text label (file name / SQL summary / query text). */
  label?: ReactNode;
  /** right-aligned summary line (row count / "234 rows" / live status). */
  summary?: ReactNode;
  /** elapsed-time chip (e.g. "0.4s"), shown after the summary on completed steps. */
  time?: string;
  status?: StepStatus;
  /** small kind chip before the tool name; `"skill"` gets the accent treatment. */
  kind?: string;
  /** Recorded failure, including failed steps displayed with a warning glyph. */
  failed?: boolean;
  /** Start expanded. Failed steps start collapsed. */
  open?: boolean;
  /** UX-1 (2026-07-03): auto-collapse detail on run→done (respect manual user toggles).
   *  Pass true only for code detail (python/sql/stdout); tables/hits are user-requested results
   *  and stay expanded after completion. */
  autoCollapseOnDone?: boolean;
  /** expanded detail body — a tool-result renderer. Omit for a non-expandable row. */
  children?: ReactNode;
}

export function Step({
  toolName,
  label,
  summary,
  time,
  status = "done",
  failed = status === "fail",
  kind,
  open: openProp,
  autoCollapseOnDone = false,
  children,
}: StepProps) {
  const meta = getToolMeta(toolName ?? null);
  const expandable = children != null && children !== false;
  const [open, setOpen] = useState(!failed && (openProp ?? status === "run"));
  const userToggledRef = useRef(false);
  const previousRef = useRef({ status, failed });
  useEffect(() => {
    const previous = previousRef.current;
    previousRef.current = { status, failed };
    if (previous.status === status && previous.failed === failed) return;
    if (failed) {
      if (!userToggledRef.current) setOpen(false);
      return;
    }
    // A partial step can contain successful results and remains open as before.
    if (status === "part") {
      setOpen(true);
      return;
    }
    if (!userToggledRef.current && autoCollapseOnDone && previous.status === "run" && status === "done") {
      setOpen(false);
    }
  }, [status, failed, autoCollapseOnDone]);

  // A caller can request late expansion of a partial result when the turn ends.
  const prevOpenPropRef = useRef(openProp);
  useEffect(() => {
    const prev = prevOpenPropRef.current;
    prevOpenPropRef.current = openProp;
    if (prev === openProp) return;
    if (openProp === true && !failed && !userToggledRef.current) setOpen(true);
  }, [openProp, failed]);
  const isOpen = expandable && open;
  const bodyId = useId();

  const name =
    status === "run" && meta?.activeName
      ? meta.activeName
      : meta?.name ?? toolName ?? "";
  const isSkill = kind === "skill";
  const labelTitle = typeof label === "string" ? label : undefined;

  return (
    <div className={`stp is-${status}`}>
      <button
        type="button"
        className="stp-head"
        disabled={!expandable}
        onClick={() => {
          if (!expandable) return;
          userToggledRef.current = true;
          setOpen((o) => !o);
        }}
        aria-expanded={expandable ? isOpen : undefined}
        aria-controls={expandable ? bodyId : undefined}
      >
        <StatusGlyph status={status} />
        {kind && <span className={`kchip${isSkill ? " is-skill" : ""}`}>{isSkill ? "skill" : kind}</span>}
        <span className="stp-tool">
          {meta?.icon ? <><CharIcon ch={meta.icon} />{" "}</> : ""}
          {name}
        </span>
        {/* chat-ux-2026-08 batch A: prettifyStepLabel returns an empty string for unrecognized arguments
            (replacing old "[no-preview]" jargon). turn-store-dispatch normalizes via
            `prettifiedLabel || null`, usually already null here; rechecking empty string
            protects **direct callers bypassing store**, since empty .stp-label occupies space
            and misaligns right-side statistics. */}
        {label != null && label !== "" ? (
          <span className="stp-label" title={labelTitle}>{label}</span>
        ) : (
          <span className="legacy-flex-spacer" />
        )}
        {summary != null && <span className="stp-sum">{summary}</span>}
        {time && <span className="stp-time">{time}</span>}
        {expandable && <Chevron open={isOpen} className="stp-chev" />}
      </button>
      {isOpen && (
        <div className="stp-detail" id={bodyId}>
          {children}
        </div>
      )}
    </div>
  );
}
