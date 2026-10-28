"use client";

import {
  resetResultsCapabilitiesForTests,
  useResultsCapabilities,
  useResultsCapabilitiesSnapshot,
} from "@/lib/use-results-capabilities";

export function useResultsWorkspaceCapability(): boolean {
  return useResultsCapabilities().workspace;
}

/**
 * Consume the shared workspace capability without owning its network load.
 * Nested result affordances use this because ChatWorkbench/TurnContainer
 * already load the single capability snapshot.
 */
export function useResultsWorkspaceCapabilitySnapshot(): boolean {
  return useResultsCapabilitiesSnapshot().workspace;
}

/** Test-only reset for the module-level request de-duplication store. */
export function resetResultsWorkspaceCapabilityForTests(): void {
  resetResultsCapabilitiesForTests();
}
