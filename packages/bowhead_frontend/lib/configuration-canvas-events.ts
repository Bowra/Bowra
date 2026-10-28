import type { SettingsCanvasTarget } from "./settings-canvas-target";

/** Shared Tool projections: live dispatch and persisted replay enter the same store. */
const opens = new Map<string, { turnId: string; target: SettingsCanvasTarget; source: "live" | "replay" }>();
const proposals = new Map<string, readonly string[]>();
const listeners = new Set<() => void>();
let revision = 0;
function notify(): void { revision += 1; listeners.forEach((listener) => listener()); }
export function subscribeConfigurationCanvas(listener: () => void): () => void {
  listeners.add(listener);
  return () => { listeners.delete(listener); };
}
export function configurationCanvasVersion(): number { return revision; }
export function configurationCanvasOpen(turnId: string | null) { return turnId ? opens.get(turnId) ?? null : null; }
const EMPTY_IDS: readonly string[] = [];
export function configurationCanvasProposalIds(turnId: string | null): readonly string[] {
  return turnId ? proposals.get(turnId) ?? EMPTY_IDS : EMPTY_IDS;
}
export function projectConfigurationCanvasOpen(payload: Record<string, unknown>, source: "live" | "replay" = "live"): void {
  const pointer = payload.target as Record<string, unknown> | undefined;
  if (typeof payload.turn_id !== "string" || !payload.turn_id || (pointer?.page !== "workspace_i18n" && pointer?.page !== "space_general")) return;
  const values = [pointer.user_id, pointer.team_id, pointer.workspace_id];
  if (!values.every((value) => typeof value === "string" && value.length > 0 && value.length <= 256)) return;
  const target: SettingsCanvasTarget = { page: pointer.page, userId: pointer.user_id as string, teamId: pointer.team_id as string, workspaceId: pointer.workspace_id as string };
  const previous = opens.get(payload.turn_id);
  if (previous && JSON.stringify(previous.target) === JSON.stringify(target)) return;
  opens.set(payload.turn_id, { turnId: payload.turn_id, target, source });
  notify();
}
export function projectConfigurationCanvasProposal(turnId: string, proposalId: string): void {
  const current = proposals.get(turnId) ?? EMPTY_IDS;
  if (current.includes(proposalId)) return;
  proposals.set(turnId, [...current, proposalId]);
  notify();
}
