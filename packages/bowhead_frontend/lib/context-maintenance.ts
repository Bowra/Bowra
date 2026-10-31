import { composerChatActionSuffix, type ComposerChatAction } from "./composer-chat-action";

/** A context-only action cannot carry a user request or resume previous work. */
export interface ContextMaintenanceAction {
  contract: "context-maintenance.v1";
  expected_generation: string | null;
}

export interface ContextMaintenanceReceipt {
  session_id: string;
  turn_id: string;
  stream_url: string;
  execution_generation: number;
  terminal_protocol_version?: number | null;
  context_maintenance: ContextMaintenanceAction;
}

export function requireMaintenanceReceipt(value: unknown, context: ContextMaintenanceAction, sessionId: string): ContextMaintenanceReceipt {
  const receipt = value as Partial<ContextMaintenanceReceipt> | null;
  const action = parseContextMaintenanceAction(receipt?.context_maintenance);
  if (!receipt || !action || action.expected_generation !== context.expected_generation
    || receipt.session_id !== sessionId || typeof receipt.turn_id !== "string" || !/^t_[A-Za-z0-9_]+$/.test(receipt.turn_id)
    || receipt.execution_generation !== 1 || ![undefined, null, 1].includes(receipt.terminal_protocol_version)
    || receipt.stream_url !== `/api/v1/chat/stream/${receipt.turn_id}`)
    throw new Error("Context maintenance receipt does not match its original action");
  return receipt as ContextMaintenanceReceipt;
}

export function parseContextMaintenanceAction(value: unknown): ContextMaintenanceAction | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const input = value as Record<string, unknown>;
  if (Object.keys(input).sort().join(",") !== "contract,expected_generation"
    || input.contract !== "context-maintenance.v1"
    || (input.expected_generation !== null && (typeof input.expected_generation !== "string" || !/^mcg_[0-9a-f]{32}$/.test(input.expected_generation)))) return null;
  return { contract: input.contract, expected_generation: input.expected_generation };
}
export function parseMaintenanceChatAction(value: unknown, sessionId: string): ComposerChatAction | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const candidate = value as Record<string, unknown>;
  if (Object.keys(candidate).sort().join(",") !== "actionId,actionKind,attempted,sessionId,sourceRevision"
    || candidate.actionKind !== "start" || candidate.attempted !== true || candidate.sessionId !== sessionId
    || candidate.sourceRevision !== null || typeof candidate.actionId !== "string") return null;
  try { composerChatActionSuffix(candidate as ComposerChatAction); } catch { return null; }
  return structuredClone(candidate) as ComposerChatAction;
}
