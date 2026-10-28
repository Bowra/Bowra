/**
 * Coalesce concurrent reads for the same key: the first call sends the request; subsequent calls share its Promise until completion.
 * Remove it after success or failure so the next call requests again. Use only for side-effect-free GETs.
 */
const inFlight = new Map<string, Promise<unknown>>();

export function coalesceRequest<T>(key: string, load: () => Promise<T>): Promise<T> {
  const existing = inFlight.get(key) as Promise<T> | undefined;
  if (existing) return existing;
  const pending = load().finally(() => inFlight.delete(key));
  inFlight.set(key, pending);
  return pending;
}
