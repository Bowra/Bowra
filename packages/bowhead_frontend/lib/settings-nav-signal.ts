/**
 * Signal that a row-end count of the sidebar settings directory may have changed (an approval, access
 * request or suggestion was decided). API clients call it after a successful mutation; the shared count
 * store (`lib/settings-nav-counts.ts`) refetches. Kept separate so the API modules do not import the store.
 */
const listeners = new Set<() => void>();

export function onSettingsNavCountsChanged(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

export function notifySettingsNavCountsChanged(): void {
  for (const listener of Array.from(listeners)) listener();
}
