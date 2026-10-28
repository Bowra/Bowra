/** Content-free native document events. Opening never grants read or write access. */
export type DocumentDraftOpen = Readonly<{
  version: 1;
  turn_id: string;
  session_id: string;
  user_id: string;
  team_id: string;
  workspace_id: string;
  job_id: string;
  draft_digest: string;
}>;

const fields = ["version", "turn_id", "session_id", "user_id", "team_id", "workspace_id", "job_id", "draft_digest"];
const envelope = ["seq", "evt_seq", "card_id", "parent_agent_id"];
const identity = (value: unknown): value is string =>
  typeof value === "string" && value.trim() === value && value.length > 0 && value.length <= 128;

export function parseDocumentDraftOpen(value: unknown, turnId: string): DocumentDraftOpen | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const item = value as Record<string, unknown>;
  if (item.version !== 1 || item.turn_id !== turnId || !identity(turnId)
    || !Object.keys(item).every((key) => fields.includes(key) || envelope.includes(key))
    || ![item.session_id, item.user_id, item.team_id, item.workspace_id].every(identity)
    || typeof item.job_id !== "string" || !/^djob_[A-Za-z0-9_]{1,123}$/.test(item.job_id)
    || typeof item.draft_digest !== "string" || !/^[0-9a-f]{64}$/.test(item.draft_digest)
    || (item.card_id !== undefined && item.card_id !== turnId)) return null;
  return Object.freeze(Object.fromEntries(fields.map((key) => [key, item[key]]))) as DocumentDraftOpen;
}

const opens = new Map<string, DocumentDraftOpen>();
const listeners = new Set<() => void>();
let revision = 0;
export const subscribeDocumentCanvas = (listener: () => void) => { listeners.add(listener); return () => { listeners.delete(listener); }; };
export const documentCanvasVersion = () => revision;
export const documentCanvasOpen = (turnId: string | null | undefined) => turnId ? opens.get(turnId) ?? null : null;

/** The native document lane admits one immutable job per Turn. Replays cannot replace it. */
export function projectDocumentDraftOpen(payload: unknown, turnId: string) {
  const pointer = parseDocumentDraftOpen(payload, turnId);
  if (!pointer || opens.has(turnId)) return;
  opens.set(turnId, pointer);
  if (opens.size > 128) opens.delete(opens.keys().next().value!);
  revision += 1;
  for (const listener of listeners) listener();
}
