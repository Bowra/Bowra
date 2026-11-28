/**
 * What `/chat/send` says about a trial message, for the trial chat (trial-tech-design §4.1, slice 2 notes):
 * the accepted send's `agent_trial_notice` (TD1: `agent_trial_candidate_under_test`) and a refusal's
 * `agent_trial_*` code, so the chat can show the notice or recheck whether the draft was released.
 * A tiny in-memory bus: the send path publishes, the trial chat subscribes; nothing is stored.
 */

export type AgentTrialSendSignal =
  | { kind: "notice"; code: string }
  | { kind: "refused"; code: string };

const listeners = new Set<(signal: AgentTrialSendSignal) => void>();

export function subscribeAgentTrialSend(listener: (signal: AgentTrialSendSignal) => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

function publish(signal: AgentTrialSendSignal): void {
  for (const listener of [...listeners]) listener(signal);
}

/** Read an accepted send response; publishes only a trial notice. */
export function publishAgentTrialSendResponse(response: unknown): void {
  const notice = response && typeof response === "object" ? (response as { agent_trial_notice?: unknown }).agent_trial_notice : undefined;
  if (typeof notice === "string" && notice) publish({ kind: "notice", code: notice });
}

/** A refused send; publishes only trial codes. */
export function publishAgentTrialSendRefusal(code: string | null | undefined): void {
  if (code && code.startsWith("agent_trial_")) publish({ kind: "refused", code });
}
