/**
 * Client mirror of `agent_change_proposal_store.apply_operations` (JSON Pointer add / replace / remove on
 * object members), used to preview what an atomic group would write into the draft. The shared fixture
 * `eval/fixtures/agent_change/apply_operations_v1.json` runs against both implementations so they can't drift.
 */
import type { AgentChangeProposal, AgentWorkingCopyValue } from "./agents-api";

export type ProposalOperation = AgentChangeProposal["operations"][number];

/** The server raises the same code for every malformed or inapplicable operation. */
export class ProposalOperationError extends Error {
  readonly code = "agent_change_proposal_invalid_patch";

  constructor() {
    super("agent_change_proposal_invalid_patch");
    this.name = "ProposalOperationError";
  }
}

type JsonObject = Record<string, unknown>;

function isObject(value: unknown): value is JsonObject {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function pointerParts(path: string): string[] {
  if (!path.startsWith("/") || path.endsWith("/")) throw new ProposalOperationError();
  return path.slice(1).split("/").map((part) => part.replaceAll("~1", "/").replaceAll("~0", "~"));
}

function resolveParent(document: JsonObject, path: string, create: boolean): [JsonObject, string] {
  const parts = pointerParts(path);
  let current: unknown = document;
  for (const part of parts.slice(0, -1)) {
    if (!isObject(current)) throw new ProposalOperationError();
    if (!Object.hasOwn(current, part)) {
      if (!create) throw new ProposalOperationError();
      current[part] = {};
    }
    current = current[part];
  }
  if (!isObject(current)) throw new ProposalOperationError();
  return [current, parts[parts.length - 1]];
}

function clone<T>(value: T): T {
  return value === undefined ? value : structuredClone(value);
}

/** Applies the operations in order to a copy of `document`; the input is never changed. */
export function applyOperations(document: JsonObject, operations: readonly ProposalOperation[]): JsonObject {
  const result = clone(document);
  for (const operation of operations) {
    const [parent, key] = resolveParent(result, String(operation.path), operation.op === "add");
    if (operation.op === "remove") {
      if (!Object.hasOwn(parent, key)) throw new ProposalOperationError();
      delete parent[key];
    } else {
      if (operation.op === "replace" && !Object.hasOwn(parent, key)) throw new ProposalOperationError();
      parent[key] = clone(operation.value);
    }
  }
  return result;
}

export const MISSING: unique symbol = Symbol("missing");
export type PointerValue = unknown | typeof MISSING;

/** The value at a JSON Pointer, or `MISSING` when a member on the way is absent. */
export function valueAtPointer(document: unknown, path: string): PointerValue {
  let current: unknown = document;
  for (const part of pointerParts(path)) {
    if (!isObject(current) || !Object.hasOwn(current, part)) return MISSING;
    current = current[part];
  }
  return current;
}

/** The three members a proposal patches, from a saved draft or a released version. */
export type ProposalBaseDocument = Pick<AgentWorkingCopyValue, "definition" | "listing" | "audience">;

/** The document a proposal patches: the same three members the server reads from the working copy. */
export function proposalDocument(workingCopy: ProposalBaseDocument): JsonObject {
  return { definition: workingCopy.definition, listing: workingCopy.listing, audience: workingCopy.audience };
}

export type FieldDiff =
  | { kind: "text"; path: string; before: string | null; after: string | null }
  | { kind: "list"; path: string; added: string[]; removed: string[]; kept: number }
  | { kind: "value"; path: string; before: PointerValue; after: PointerValue };

/** Stable key for a list item: its name when it has one, otherwise its JSON. */
export function listItemKey(item: unknown): string {
  if (typeof item === "string" || typeof item === "number" || typeof item === "boolean") return String(item);
  if (isObject(item)) {
    const skill = item.skill_ref;
    if (isObject(skill) && typeof skill.name === "string") return skill.name;
    for (const field of ["name", "stable_ref", "ref", "id"]) {
      if (typeof item[field] === "string") return item[field] as string;
    }
  }
  return JSON.stringify(item);
}

function fieldDiff(path: string, before: PointerValue, after: PointerValue): FieldDiff {
  const textual = (value: PointerValue) => value === MISSING || value === null || typeof value === "string";
  if (textual(before) && textual(after) && (typeof before === "string" || typeof after === "string")) {
    return { kind: "text", path, before: typeof before === "string" ? before : null, after: typeof after === "string" ? after : null };
  }
  const listy = (value: PointerValue) => value === MISSING || value === null || Array.isArray(value);
  if (listy(before) && listy(after) && (Array.isArray(before) || Array.isArray(after))) {
    const was = new Set((Array.isArray(before) ? before : []).map(listItemKey));
    const now = new Set((Array.isArray(after) ? after : []).map(listItemKey));
    return {
      kind: "list",
      path,
      added: [...now].filter((item) => !was.has(item)),
      removed: [...was].filter((item) => !now.has(item)),
      kept: [...now].filter((item) => was.has(item)).length,
    };
  }
  return { kind: "value", path, before, after };
}

export type GroupPreview = { ok: true; rows: FieldDiff[] } | { ok: false; rows: FieldDiff[] };

/**
 * Before and after for each field an atomic group touches, against the loaded draft. When the group no
 * longer applies to the draft, the rows show the proposed values only and `ok` is false.
 */
export function previewAtomicGroup(
  document: JsonObject | null,
  proposal: Pick<AgentChangeProposal, "operations" | "atomic_groups">,
  groupId: string,
): GroupPreview {
  const group = proposal.atomic_groups.find((item) => item.id === groupId);
  if (!group) return { ok: false, rows: [] };
  const operations = group.operation_indexes.map((index) => proposal.operations[index]).filter((item): item is ProposalOperation => Boolean(item));
  const paths = [...new Set(operations.map((operation) => operation.path))];
  const proposed = (path: string): PointerValue => {
    const last = [...operations].reverse().find((operation) => operation.path === path);
    return !last || last.op === "remove" ? MISSING : last.value;
  };
  if (!document) return { ok: false, rows: paths.map((path) => fieldDiff(path, MISSING, proposed(path))) };
  try {
    const after = applyOperations(document, operations);
    return {
      ok: true,
      rows: paths.map((path) => {
        try {
          return fieldDiff(path, valueAtPointer(document, path), valueAtPointer(after, path));
        } catch {
          return fieldDiff(path, MISSING, proposed(path));
        }
      }),
    };
  } catch {
    return {
      ok: false,
      rows: paths.map((path) => {
        let before: PointerValue = MISSING;
        try {
          before = valueAtPointer(document, path);
        } catch {
          // A malformed pointer has no current value.
        }
        return fieldDiff(path, before, proposed(path));
      }),
    };
  }
}
