/**
 * Skill-only `/` starts travel as an ordinary Lead Turn with `selected_skills`
 * (docs/sprints/skill-invocation-2026-11-17/tech-design.md §10.2). The reference
 * source document stays on the client for draft cleanup and recovery; the wire
 * carries the same `/name` display text plus the exact Skill identities.
 */
import type { InputDocument, ResourceRef } from "./composer-input-document";

export type SelectedSkillRef = Extract<ResourceRef, { kind: "skill" }>;

/** Matches the Gateway `ChatSendRequest.selected_skills` bound. */
export const MAX_SELECTED_SKILLS = 3;

const SKILL_ID = /^[A-Za-z0-9_.:-]+$/;
const SKILL_VERSION = /^sha256:[0-9a-f]{64}$/;

/** Send state that decides between the ordinary and the reference path. */
export interface SelectedSkillRouteContext {
  /** `/auth/me` `capabilities.composer.skill_via_lead`. */
  skillViaLead: boolean;
  submissionKind: "start" | "replay" | "steer";
  /** A Terminal chat action owns this send. */
  chatAction: boolean;
  wireSnapshot: {
    planningMode?: "planning" | "off" | null;
    draftChatBinding?: unknown;
    workspaceSettingsBinding?: unknown;
    coachCanvasBinding?: unknown;
    documentRevisionTarget?: unknown;
    contextMaintenance?: unknown;
  };
}

/**
 * The exact Skills of an input whose only references are 1–3 distinct Skills
 * without parameter values, in document order; null for any other input.
 */
export function skillSelectionFromInputDocument(
  document: InputDocument,
): SelectedSkillRef[] | null {
  const references = document.nodes.filter((node) => node.type === "reference");
  if (references.length === 0 || references.length > MAX_SELECTED_SKILLS) return null;
  const selected: SelectedSkillRef[] = [];
  const identities = new Set<string>();
  for (const node of references) {
    const resource = node.resource;
    if (resource.kind !== "skill") return null;
    // Parameter values have no ordinary-wire carrier; keep them on the reference path.
    if (node.parameters && Object.keys(node.parameters).length > 0) return null;
    const identity = JSON.stringify([resource.source_kind, resource.id]);
    if (identities.has(identity)) return null;
    identities.add(identity);
    selected.push({
      kind: "skill",
      source_kind: resource.source_kind,
      id: resource.id,
      version: resource.version,
    });
  }
  return selected;
}

/**
 * Decide the route of a reference start once, before its journal is written.
 * Returns the Skills to send as `selected_skills`, or null to keep today's
 * `chat-references.v1` path. Recovery never calls this: it resends the frozen choice.
 */
export function selectedSkillsForStart(
  document: InputDocument,
  context: SelectedSkillRouteContext,
): SelectedSkillRef[] | null {
  const wire = context.wireSnapshot;
  if (
    !context.skillViaLead
    || context.submissionKind !== "start"
    || context.chatAction
    || wire.planningMode === "planning"
    || wire.draftChatBinding
    || wire.workspaceSettingsBinding
    || wire.coachCanvasBinding
    || wire.documentRevisionTarget
    || wire.contextMaintenance
  ) return null;
  return skillSelectionFromInputDocument(document);
}

/** Validate a stored or frozen selection; throws on anything but 1–3 exact, distinct Skill refs. */
export function parseSelectedSkills(value: unknown): SelectedSkillRef[] {
  if (!Array.isArray(value) || value.length === 0 || value.length > MAX_SELECTED_SKILLS)
    throw new Error("selected_skills_invalid");
  const identities = new Set<string>();
  return value.map((item) => {
    const ref = item as Record<string, unknown> | null;
    if (
      ref === null
      || typeof ref !== "object"
      || Array.isArray(ref)
      || Object.keys(ref).length !== 4
      || ref.kind !== "skill"
      || !["builtin", "team", "user"].includes(ref.source_kind as string)
      || typeof ref.id !== "string"
      || ref.id.length === 0
      || ref.id.length > 512
      || !SKILL_ID.test(ref.id)
      || typeof ref.version !== "string"
      || !SKILL_VERSION.test(ref.version)
    ) throw new Error("selected_skills_invalid");
    const identity = JSON.stringify([ref.source_kind, ref.id]);
    if (identities.has(identity)) throw new Error("selected_skills_invalid");
    identities.add(identity);
    return {
      kind: "skill",
      source_kind: ref.source_kind as SelectedSkillRef["source_kind"],
      id: ref.id,
      version: ref.version,
    };
  });
}

/** A frozen selection must be exactly the one its source document yields. */
export function selectedSkillsMatchDocument(
  value: unknown,
  document: InputDocument,
): boolean {
  let parsed: SelectedSkillRef[];
  try {
    parsed = parseSelectedSkills(value);
  } catch {
    return false;
  }
  const expected = skillSelectionFromInputDocument(document);
  return expected !== null && JSON.stringify(parsed) === JSON.stringify(expected);
}
