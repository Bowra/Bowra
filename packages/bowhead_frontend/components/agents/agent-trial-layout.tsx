"use client";

import { createContext, useContext } from "react";

/**
 * The trial chat's layout (trial-tech-design §7, canvas ⑪): the chat on the left, the Agent editor in the Canvas.
 * Send already saves first, so inside it the Canvas release band and save footer drop their primary buttons to
 * secondary and Send is the screen's only primary action.
 */
export const AgentTrialLayoutContext = createContext(false);

export function useAgentTrialLayout(): boolean {
  return useContext(AgentTrialLayoutContext);
}
