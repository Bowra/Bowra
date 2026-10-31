"use client";

import { createContext, useContext, type ReactNode } from "react";
import { useTranslations } from "next-intl";
import { useTurn } from "@/lib/turn-store";
import type { ContextActivity } from "@/lib/context-activity";
import { StatusGlyph } from "./block-primitives";
import { useTurnRunning } from "./turn-running";
import "./ContextActivity.css";

const ActivityContext = createContext<readonly ContextActivity[]>([]);

export function ContextActivityProvider({ turnId, children }: { turnId: string; children: ReactNode }) {
  const state = useTurn(turnId);
  return <ActivityContext.Provider value={Object.values(state?.contextActivities ?? {})}>{children}</ActivityContext.Provider>;
}

export function useContextActivities() {
  return useContext(ActivityContext);
}

/** A maintenance fact belongs to its Agent; it is neither a Tool nor an answer. */
export function ContextActivityRows({ subagentId }: { subagentId: string | null }) {
  const activities = useContextActivities();
  const running = useTurnRunning();
  const t = useTranslations("step_list.context_activity");
  return <>{activities.filter((activity) => activity.subagent_id === subagentId).map((activity) => {
    const phase = activity.phase === "running" && !running ? "interrupted" : activity.phase;
    return <div className="ag-context-activity" key={activity.operation_id} data-context-phase={phase}>
      <span aria-hidden="true"><StatusGlyph status={phase === "running" ? "run" : phase === "committed" ? "done" : "pend"} /></span>
      <span>{t(activity.reason === "no_change" ? "no_change" : phase)}</span>
    </div>;
  })}</>;
}
