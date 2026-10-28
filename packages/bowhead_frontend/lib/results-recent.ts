import type { ResultsScope } from "@/lib/results-api";

const MAX_RECENT_RESULTS = 3;
const RESULT_KEY = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$/;
const recentByScope = new Map<string, string[]>();

export function resultsRecentScopeKey({
  principalId,
  scope,
  sessionId,
}: {
  principalId: string;
  scope: ResultsScope;
  sessionId: string;
}): string {
  return [principalId, sessionId, scope]
    .map((value) => `${value.length}:${value}`)
    .join("|");
}

export function readRecentResults(scopeKey: string): string[] {
  return [...(recentByScope.get(scopeKey) ?? [])];
}

export function recordRecentResult(scopeKey: string, key: string): string[] {
  if (!scopeKey || !RESULT_KEY.test(key)) return readRecentResults(scopeKey);
  const next = [
    key,
    ...(recentByScope.get(scopeKey) ?? []).filter((item) => item !== key),
  ].slice(0, MAX_RECENT_RESULTS);
  recentByScope.set(scopeKey, next);
  return [...next];
}

export function removeRecentResult(scopeKey: string, key: string): string[] {
  const next = (recentByScope.get(scopeKey) ?? []).filter((item) => item !== key);
  if (next.length > 0) recentByScope.set(scopeKey, next);
  else recentByScope.delete(scopeKey);
  return [...next];
}

export function clearRecentResults(scopeKey: string): void {
  recentByScope.delete(scopeKey);
}

export function resetRecentResultsForTests(): void {
  recentByScope.clear();
}
