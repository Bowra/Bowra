"use client";

import { createContext, useContext } from "react";

/**
 * Session turn ordinals (turn id -> "Turn N", same count as the chat topbar) for components deep inside the
 * workbench, such as the report history accordion. Provided by the chat page (P2 batch 3); null elsewhere
 * (shared reports, report library), where callers keep their own numbering.
 */
export const SessionTurnOrdinalsContext = createContext<ReadonlyMap<string, number> | null>(null);

export function useSessionTurnOrdinals(): ReadonlyMap<string, number> | null {
  return useContext(SessionTurnOrdinalsContext);
}
