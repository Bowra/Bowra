"use client";

/**
 * The top of the expanded trace (chat running state R3.2, R3.3): the style switcher, and under 胶囊, 泳道 or 时间轴 an
 * overview of the whole Turn in that style. The full trace stays below it with every step's detail; selecting a chip,
 * dot or segment brings that step's row into view and opens it (opening its helper's block first when needed).
 */
import { useMemo } from "react";
import { useTranslations } from "next-intl";

import type { PeerTerminal } from "@/lib/turn-store";
import { useRunningViewStyle } from "@/lib/running-view-style";

import type { FoldedNote } from "./folded-notes";
import { activeHelperCount, buildRunGroups } from "./run-groups";
import { formatElapsed } from "./RunningBand";
import { RunStyleSwitcher } from "./RunStyleSwitcher";
import { RunStyleView } from "./RunStyles";
import type { StepView } from "./types";

function selectorValue(value: string): string {
  return typeof CSS !== "undefined" && CSS.escape ? CSS.escape(value) : value.replace(/["\\]/g, "\\$&");
}

/** Bring a step's trace row into view and open it; a returned helper's block opens first when it is folded. */
export function revealTraceStep(root: ParentNode, step: StepView): void {
  const find = () => root.querySelector<HTMLElement>(`.stp[data-step-id="${selectorValue(step.stepId)}"]`);
  const open = (row: HTMLElement) => {
    row.scrollIntoView({ block: "center" });
    const head = row.querySelector<HTMLButtonElement>(".stp-head");
    if (head && !head.disabled) {
      if (head.getAttribute("aria-expanded") === "false") head.click();
      head.focus({ preventScroll: true });
    } else {
      // A step without detail has a disabled head: the row itself takes focus so the reader lands on it.
      row.setAttribute("tabindex", "-1");
      row.focus({ preventScroll: true });
    }
    row.setAttribute("data-revealed", "1");
    window.setTimeout(() => row.removeAttribute("data-revealed"), 1600);
  };
  const row = find();
  if (row) {
    open(row);
    return;
  }
  const block = step.subagentId
    ? root.querySelector<HTMLButtonElement>(`.agb[data-agent-key="${selectorValue(step.subagentId)}"] > .agb-head`)
    : null;
  if (!block || block.getAttribute("aria-expanded") !== "false") return;
  block.click();
  requestAnimationFrame(() => {
    const opened = find();
    if (opened) open(opened);
  });
}

export function TraceStyleOverview({
  steps,
  peerNames,
  peerDisplayNames,
  peerTasks,
  peerTerminalById,
  notes,
  running,
}: {
  steps: StepView[];
  peerNames: Record<string, string>;
  peerDisplayNames?: Record<string, string>;
  peerTasks?: Record<string, string>;
  peerTerminalById?: Record<string, PeerTerminal>;
  notes: readonly FoldedNote[];
  running: boolean;
}) {
  const t = useTranslations("step_list");
  const { style } = useRunningViewStyle();
  const groups = useMemo(
    () => (style === "list" ? [] : buildRunGroups({ steps, peerNames, peerTasks, peerDisplayNames, peerTerminalById, notes })),
    [style, steps, peerNames, peerTasks, peerDisplayNames, peerTerminalById, notes],
  );
  return (
    <div className="ag-trace__style" data-testid="trace-style-overview">
      <RunStyleSwitcher className="ag-trace__switch" />
      {groups.length > 0 ? (
        <div className="ag-trace__overview">
          <RunStyleView
            style={style}
            expanded
            running={running}
            nowMs={null}
            turnStartMs={null}
            groups={groups}
            activeHelpers={activeHelperCount(groups)}
            stepElapsedMs={() => null}
            formatTime={(ms) => formatElapsed(ms, t)}
            owner={null}
            onSelectStep={(step) => {
              const trace = document.querySelector(`.ag-trace [data-step-id="${selectorValue(step.stepId)}"]`)?.closest(".ag-trace")
                ?? document.querySelector(`.agb[data-agent-key="${selectorValue(step.subagentId ?? "")}"]`)?.closest(".ag-trace")
                ?? document;
              revealTraceStep(trace, step);
            }}
          />
        </div>
      ) : null}
    </div>
  );
}
