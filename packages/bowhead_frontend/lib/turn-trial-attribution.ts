"use client";

/**
 * Which frozen draft answered a trial Turn (trial-tech-design §6). Live and replayed Turns carry it on their
 * Agent events (the turn store); a Turn restored from history without its events reads the stored binding
 * (`GET /turns/{id}/agent-binding`, the same `trial` member), so live, persisted and replayed views agree.
 */

import { useEffect, useState } from "react";

import { apiFetch } from "@/lib/api-client";
import { readTrialAttribution, type TrialAttribution } from "@/lib/agent-trial";
import { turnTrialAttribution, useTurn } from "@/lib/turn-store";

const stored = new Map<string, Promise<TrialAttribution | null>>();

/** The stored binding's trial member; null outside a trial and when the read fails (the label is then left out). */
export function fetchTurnTrialAttribution(turnId: string): Promise<TrialAttribution | null> {
  const cached = stored.get(turnId);
  if (cached) return cached;
  const request = apiFetch(`/api/v1/turns/${encodeURIComponent(turnId)}/agent-binding`, { credentials: "include", cache: "no-store" })
    .then(async (response) => {
      if (!response.ok) return null;
      const body = await response.json() as { executions?: Array<{ trial?: unknown }> };
      for (const execution of body.executions ?? []) {
        const trial = readTrialAttribution(execution.trial);
        if (trial) return trial;
      }
      return null;
    })
    .catch(() => null);
  stored.set(turnId, request);
  // A failed read is not remembered, so a later render can try again.
  void request.then((value) => { if (value === null) stored.delete(turnId); });
  return request;
}

/** Test hook. */
export function clearTurnTrialAttributionCache(): void {
  stored.clear();
}

export function useTurnTrialAttribution(turnId: string | null | undefined, enabled: boolean, settled: boolean): TrialAttribution | null {
  const live = turnTrialAttribution(useTurn(turnId ?? ""));
  const [read, setRead] = useState<{ turnId: string; value: TrialAttribution | null } | null>(null);
  useEffect(() => {
    if (!enabled || !settled || !turnId || live) return;
    let cancelled = false;
    void fetchTurnTrialAttribution(turnId).then((value) => {
      if (!cancelled) setRead({ turnId, value });
    });
    return () => { cancelled = true; };
  }, [enabled, live, settled, turnId]);
  if (!enabled || !turnId) return null;
  return live ?? (read?.turnId === turnId ? read.value : null);
}
