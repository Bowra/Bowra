import type { CoachResourceReference } from "./coach-resource-editors";

export type CoachCanvasResourceTarget =
  | { kind: "skill"; id: string; source_kind: "user" | "team" | "builtin"; revision: string }
  | { kind: "plugin"; id: string; version: number; revision: number; content_hash: string }
  | { kind: "agent"; id: string; version: number; live_release_id: string;
      working_copy: { id: string; version: number; base_release_id: string | null } | null };
export type CoachEditorSnapshot = { target: CoachCanvasResourceTarget; dirty: boolean; pending_write: boolean };
export type CoachCanvasBinding = CoachEditorSnapshot & {
  schema_version: 1; user_id: string; team_id: string; workspace_id: string; session_id: string;
};
export type CoachEditorPort = {
  requestClose: (afterClose?: () => void) => void;
  capture?: () => CoachEditorSnapshot | null;
  /** Save the editor's input the way its own Save does; resolves whether it was saved. */
  save?: () => Promise<boolean>;
  /** Discard the editor's input after the editor's own confirmation; resolves whether it was discarded. */
  discard?: () => Promise<boolean>;
};

export class CoachCanvasNotReadyError extends Error {
  constructor(readonly code: "local_changes" | "page_not_ready") { super(code); }
}

/** Freeze the original editor's baseline, never a newer background projection. */
export function captureCoachCanvasBinding(
  scope: Pick<CoachCanvasBinding, "user_id" | "team_id" | "workspace_id" | "session_id">,
  reference: CoachResourceReference,
  port: CoachEditorPort | null,
): CoachCanvasBinding {
  const captured = port?.capture?.();
  if (!captured || !Object.values(scope).every((value) => value.length > 0)
    || captured.target.id !== reference.id
    || (captured.target.kind === "plugin" ? "capability" : captured.target.kind) !== reference.kind
    || captured.target.kind === "skill" && captured.target.source_kind !== reference.source_kind)
    throw new CoachCanvasNotReadyError("page_not_ready");
  if (captured.dirty || captured.pending_write) throw new CoachCanvasNotReadyError("local_changes");
  return structuredClone({ schema_version: 1, ...scope, ...captured });
}

export function parseCoachCanvasBinding(value: unknown): CoachCanvasBinding | null {
  const object = (value: unknown): value is Record<string, unknown> => !!value && typeof value === "object" && !Array.isArray(value);
  const keys = (value: Record<string, unknown>, allowed: string[]) => Object.keys(value).every((key) => allowed.includes(key)) && allowed.every((key) => Object.hasOwn(value, key));
  const text = (value: unknown) => typeof value === "string" && value.length > 0 && value.length <= 160;
  const version = (value: unknown) => Number.isSafeInteger(value) && Number(value) > 0 && Number(value) <= 999_999_999;
  if (!object(value) || !keys(value, ["schema_version", "user_id", "team_id", "workspace_id", "session_id", "target", "dirty", "pending_write"])
    || value.schema_version !== 1 || ![value.user_id, value.team_id, value.workspace_id, value.session_id].every(text)
    || typeof value.dirty !== "boolean" || typeof value.pending_write !== "boolean" || !object(value.target)) return null;
  const target = value.target;
  if (!text(target.id)) return null;
  if (target.kind === "skill") {
    if (!keys(target, ["kind", "id", "source_kind", "revision"]) || !["user", "team", "builtin"].includes(String(target.source_kind)) || !text(target.revision)) return null;
  } else if (target.kind === "plugin") {
    if (!keys(target, ["kind", "id", "version", "revision", "content_hash"]) || !/^tcap_[0-9a-f]{32}$/.test(String(target.id))
      || !version(target.version) || !version(target.revision) || !/^sha256:[0-9a-f]{64}$/.test(String(target.content_hash))) return null;
  } else if (target.kind === "agent") {
    if (!keys(target, ["kind", "id", "version", "live_release_id", "working_copy"]) || !/^osa_[A-Za-z0-9_-]+$/.test(String(target.id))
      || !version(target.version) || !text(target.live_release_id)) return null;
    const copy = target.working_copy;
    if (copy !== null && (!object(copy) || !keys(copy, ["id", "version", "base_release_id"]) || !text(copy.id)
      || !version(copy.version) || !(copy.base_release_id === null || text(copy.base_release_id)))) return null;
  } else return null;
  return structuredClone(value) as CoachCanvasBinding;
}
