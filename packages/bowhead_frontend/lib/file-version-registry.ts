/**
 * The newest version of each delivered file that this page has shown (116 recheck N6).
 *
 * A chat card learns "Updated to version N" from the history projection, which runs when the
 * session loads. A later Turn that delivers a newer version did not reach cards already on the
 * page, so they kept their old state until a reload. Every file card records the version it shows
 * here, and older cards of the same file read the newest one. Pruning is still known only from
 * the projection; nothing here is persisted.
 */
import { useSyncExternalStore } from "react";

import type { ChatInlineFileData } from "@/components/chat/step-list/ChatInlineFile";

export type ShownFileVersion = NonNullable<ChatInlineFileData["latest"]>;

const newest = new Map<string, ShownFileVersion>();
const listeners = new Set<() => void>();

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/** Record a version of the file whose first version is `lineageRoot`; keeps only the newest. */
export function recordFileVersion(lineageRoot: string, version: ShownFileVersion): void {
  const current = newest.get(lineageRoot);
  if (current && current.versionNo >= version.versionNo) return;
  newest.set(lineageRoot, version);
  for (const listener of listeners) listener();
}

/** The newest recorded version of this file, if any. */
export function useNewestFileVersion(lineageRoot: string | undefined): ShownFileVersion | undefined {
  return useSyncExternalStore(
    subscribe,
    () => (lineageRoot ? newest.get(lineageRoot) : undefined),
    () => undefined,
  );
}

/** Test helper: forget every recorded version. */
export function resetFileVersions(): void {
  newest.clear();
}
