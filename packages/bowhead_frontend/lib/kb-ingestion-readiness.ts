"use client";

import { useEffect, useState } from "react";

import { fetchWithRefresh } from "@/lib/auth-api";

export type KbIngestionReadiness = "checking" | "ready" | "missing" | "unavailable";

export async function fetchKbIngestionReadiness(): Promise<Exclude<KbIngestionReadiness, "checking">> {
  try {
    const response = await fetchWithRefresh("/api/v1/team/kb-ingestion-readiness");
    if (!response.ok) return "unavailable";
    const body = (await response.json()) as { state?: string };
    return body.state === "ready" || body.state === "missing" ? body.state : "unavailable";
  } catch {
    return "unavailable";
  }
}

export function useKbIngestionReadiness(enabled = true) {
  const [state, setState] = useState<KbIngestionReadiness>("checking");
  const [retryKey, setRetryKey] = useState(0);
  useEffect(() => {
    if (!enabled) {
      setState("checking");
      return;
    }
    let active = true;
    setState("checking");
    void fetchKbIngestionReadiness().then((result) => {
      if (active) setState(result);
    });
    return () => { active = false; };
  }, [enabled, retryKey]);
  return { state, retry: () => setRetryKey((key) => key + 1) };
}
