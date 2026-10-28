"use client";

/** Configuration copilot five per-field states (PRD R3), shared hook extracted from scheduled.tsx (batch 1-d WP-2).
 *
 * Five states: `ai` (AI-filled) | `warn` (AI-filled, needs confirmation) | `kept` (manual, protected) |
 * `done` (accepted) | absent (untouched by AI). Three review-derived invariants preserved **verbatim** in extraction:
 *
 * 1. **Per-field baseline** (batch 0 R4 P1-1): snapshot pre-AI value on first AI fill;
 *    regeneration never overwrites existing baseline, or Restore after two generations returns first AI value, not user original.
 * 2. **Synchronous kept mirror** (batch 0 R4 P1-2): admit must immediately see manual changes during generation wait (real model 5–30s);
 *    `aiMeta` state closure goes stale, so record in ref.
 * 3. **Functional merge for commit**: never spread closure-captured `aiMeta`.
 *    ⚠️ Currently **untested** (mutation to closure-snapshot merge leaves all 10 scheduled-copilot tests green);
 *    no real failing scenario could be constructed (`applyDraft` fully synchronous; user events cannot interleave).
 *    Treat as defensive code: **never remove merely because it looks dead**; removal has no failing guard.
 */

import { useRef, useState } from "react";

export type CopilotFieldState = "ai" | "warn" | "kept" | "done";

export interface CopilotFieldEntry {
  state: CopilotFieldState;
  note?: string | null;
  why?: string | null;
}

/** Relevant part of generation result (independent of concrete form fields shape). */
export interface CopilotDraftLike {
  field_meta: Record<string, { origin: string; needs_confirm: boolean; note?: string | null }>;
  issues: { field: string; message: string }[];
}

export interface CopilotApplyReceipt {
  filled: number;
  warn: number;
  notes?: string[];
}

export interface CopilotApplySession<F extends string> {
  /** Whether backend field may apply to UI field: nongenerated/manual-edited → false (caller skips setState). */
  admit: (field: F, backendKey: string) => boolean;
  /** Commit current updates, returning summary receipt; `mappedKeys` are mapped backend keys (other issues enter notes). */
  commit: (mappedKeys: readonly string[]) => CopilotApplyReceipt;
}

export interface UseCopilotFieldsOptions<F extends string, S> {
  /** Flag off makes all no-op, zero host DOM difference. */
  enabled: boolean;
  fields: readonly F[];
  /** Snapshot current field value (baseline/restore). */
  snapshotOf: (field: F) => S;
  /** Restore snapshot into form state. */
  restoreSnapshot: (snapshot: S) => void;
}

export interface UseCopilotFields<F extends string> {
  aiMeta: Partial<Record<F, CopilotFieldEntry>>;
  /** Manual edit → kept (protected from generation/regeneration/clearing). */
  touchField: (field: F) => void;
  acceptField: (field: F) => void;
  acceptAll: () => void;
  revertField: (field: F) => void;
  /** Clear suggestions: ai/warn/done return to baseline; kept retains value/protection through next generation. */
  clearSuggestions: () => void;
  beginApply: (draft: CopilotDraftLike) => CopilotApplySession<F>;
}

export function useCopilotFields<F extends string, S>({
  enabled,
  fields,
  snapshotOf,
  restoreSnapshot,
}: UseCopilotFieldsOptions<F, S>): UseCopilotFields<F> {
  const [aiMeta, setAiMeta] = useState<Partial<Record<F, CopilotFieldEntry>>>({});
  const baselineRef = useRef<Partial<Record<F, S>>>({});
  const keptRef = useRef<Set<F>>(new Set());
  // Closure snapshotOf/restoreSnapshot are new each render; refs read latest functions,
  // preventing admit from snapshotting stale form values as baseline.
  const snapshotRef = useRef(snapshotOf);
  snapshotRef.current = snapshotOf;
  const restoreRef = useRef(restoreSnapshot);
  restoreRef.current = restoreSnapshot;

  const touchField = (field: F) => {
    if (!enabled) return;
    keptRef.current.add(field);
    setAiMeta((m) => {
      const entry = m[field];
      if (!entry || entry.state === "kept") return m;
      return { ...m, [field]: { state: "kept" as const } };
    });
  };

  const revertField = (field: F) => {
    const snapshot = baselineRef.current[field];
    if (snapshot !== undefined) restoreRef.current(snapshot);
    delete baselineRef.current[field];
    setAiMeta((m) => {
      const next = { ...m };
      delete next[field];
      return next;
    });
  };

  const acceptField = (field: F) => {
    setAiMeta((m) => (m[field] ? { ...m, [field]: { ...m[field]!, state: "done" as const, note: undefined } } : m));
  };

  const acceptAll = () => {
    setAiMeta(
      (m) =>
        Object.fromEntries(
          Object.entries(m).map(([key, value]) => {
            const entry = value as CopilotFieldEntry | undefined;
            return [key, entry && (entry.state === "ai" || entry.state === "warn") ? { ...entry, state: "done" as const, note: undefined } : entry];
          }),
        ) as Partial<Record<F, CopilotFieldEntry>>,
    );
  };

  const clearSuggestions = () => {
    for (const field of fields) {
      const entry = aiMeta[field];
      if (entry && entry.state !== "kept") revertField(field);
    }
    setAiMeta(
      (m) => Object.fromEntries(Object.entries(m).filter(([, value]) => (value as CopilotFieldEntry | undefined)?.state === "kept")) as Partial<Record<F, CopilotFieldEntry>>,
    );
  };

  const beginApply = (draft: CopilotDraftLike): CopilotApplySession<F> => {
    const updates: Partial<Record<F, CopilotFieldEntry>> = {};
    const issueFor = (key: string) => draft.issues.find((issue) => issue.field === key)?.message ?? null;

    const admit = (field: F, backendKey: string): boolean => {
      const meta = draft.field_meta[backendKey];
      if (!meta || meta.origin !== "generated") return false;
      if (keptRef.current.has(field)) return false; // Protect manual changes (synchronous mirror, no stale closure).
      if (!(field in baselineRef.current)) baselineRef.current[field] = snapshotRef.current(field);
      const note = meta.note ?? issueFor(backendKey);
      updates[field] = { state: meta.needs_confirm || issueFor(backendKey) ? "warn" : "ai", note, why: meta.note ?? undefined };
      return true;
    };

    const commit = (mappedKeys: readonly string[]): CopilotApplyReceipt => {
      // Functional merge keeps in-wait touchField kept flags from stale closure erasure (batch 0 R4 P1-2; header ⚠️).
      setAiMeta((prev) => {
        const next = { ...prev };
        for (const [key, value] of Object.entries(updates)) {
          if (!keptRef.current.has(key as F)) next[key as F] = value as CopilotFieldEntry;
        }
        return next;
      });
      const admitted = Object.values(updates) as (CopilotFieldEntry | undefined)[];
      const mapped = new Set(mappedKeys);
      return {
        filled: admitted.length,
        warn: admitted.filter((entry) => entry?.state === "warn").length,
        notes: draft.issues.filter((issue) => !mapped.has(issue.field)).map((issue) => issue.message),
      };
    };

    return { admit, commit };
  };

  return { aiMeta, touchField, acceptField, acceptAll, revertField, clearSuggestions, beginApply };
}
