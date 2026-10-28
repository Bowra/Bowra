/**
 * V3 PRD-B · /remember slash command parser + dispatcher.
 *
 * Syntax:  /remember [kind:]key=value
 *   kind ∈ {preference, constraint}, default "preference".
 *   value tries JSON.parse, falls back to string.
 *
 * Pure function exported for unit-testing; wrapped by composer.
 */

import { remember } from "./api";
import type { CandidateRefusalReason } from "./use-memory-candidates";

export type RememberKind = "preference" | "constraint";

export interface ParsedRemember {
  kind: RememberKind;
  key: string;
  value: unknown;
  valueIsJson: boolean;
}

/** Failure kinds: UI uses these for localized format/correction guidance, without displaying the English msg below. */
export type RememberFailure =
  | "syntax"
  | "empty_key"
  | "too_long"
  | "invalid"
  | "refused"
  | "scope_conflict"
  | "locked"
  | "failed";

export function parseRememberLine(
  line: string,
): { ok: true; parsed: ParsedRemember } | { ok: false; msg: string; reason: RememberFailure } {
  if (!line.startsWith("/remember")) {
    return { ok: false, msg: "not a /remember command", reason: "syntax" };
  }
  const body = line.slice("/remember".length).trim();
  if (!body) return { ok: false, msg: "syntax: /remember [kind:]key=value", reason: "syntax" };

  let kind: RememberKind = "preference";
  let rest = body;
  const colonIdx = body.indexOf(":");
  const eqIdx = body.indexOf("=");
  if (colonIdx > 0 && (eqIdx < 0 || colonIdx < eqIdx)) {
    const possibleKind = body.slice(0, colonIdx).trim();
    if (
      possibleKind === "preference"
      || possibleKind === "constraint"
    ) {
      kind = possibleKind;
      rest = body.slice(colonIdx + 1).trim();
    }
  }
  const eq = rest.indexOf("=");
  if (eq <= 0) return { ok: false, msg: "syntax: /remember [kind:]key=value", reason: "syntax" };
  const key = rest.slice(0, eq).trim();
  const valueRaw = rest.slice(eq + 1).trim();
  if (!key) return { ok: false, msg: "key is empty", reason: "empty_key" };

  let value: unknown = valueRaw;
  let isJson = false;
  try {
    value = JSON.parse(valueRaw);
    isJson = true;
  } catch {
    /* keep as string */
  }
  return { ok: true, parsed: { kind, key, value, valueIsJson: isJson } };
}

/** Write-gate refusal reasons (same stable enum as memory-candidate cards; reuse `memory_card.refused_*` copy). */
export type RememberRefusal = CandidateRefusalReason;
const REFUSALS: readonly string[] = ["one_off", "language_is_a_setting", "no_attribution", "contains_secret"];

/**
 * Classify save failures by status/error code (raw API wording never enters user-visible sentences).
 * Only 422 with `memory_write_gate_refused` is a policy refusal; 422 without an error code means
 * the request itself is invalid (name too long, wrong format).
 */
export function rememberFailure(error: unknown): { reason: RememberFailure; refusal?: RememberRefusal } {
  const record = error && typeof error === "object"
    ? (error as { status?: unknown; code?: unknown; body?: unknown })
    : {};
  if (record.code === "memory_write_gate_refused") {
    const detail = record.body && typeof record.body === "object"
      ? (record.body as { detail?: { reason?: unknown } }).detail
      : undefined;
    const reason = detail?.reason;
    return typeof reason === "string" && REFUSALS.includes(reason)
      ? { reason: "refused", refusal: reason as RememberRefusal }
      : { reason: "refused" };
  }
  if (record.status === 413) return { reason: "too_long" };
  if (record.status === 422) return { reason: "invalid" };
  if (record.status === 409 || record.code === "memory_scope_conflict") return { reason: "scope_conflict" };
  if (record.status === 423) return { reason: "locked" };
  return { reason: "failed" };
}

export async function dispatchRememberLine(
  line: string,
): Promise<
  | { ok: true; key: string }
  | { ok: false; msg: string; reason: RememberFailure; refusal?: RememberRefusal; error?: unknown }
> {
  const r = parseRememberLine(line);
  if (!r.ok) return r;
  try {
    await remember(r.parsed.kind, r.parsed.key, r.parsed.value);
    return { ok: true, key: r.parsed.key };
  } catch (e) {
    return { ok: false, msg: (e as Error).message, ...rememberFailure(e), error: e };
  }
}
