/**
 * After a save the Results view opens the new version in a fresh viewer. The editor already holds
 * that version's rendered PDF, so the viewer takes it from here instead of rendering the deck again
 * (a render costs 7–12 s, design §10.1), and reopens in the full view the person was using.
 */

export interface PresentationHandoff {
  pdfData?: ArrayBuffer;
  expanded: boolean;
  /** Open straight into edit mode (moving from an upload to the file already made from it). */
  edit?: boolean;
}

const TTL_MS = 60_000;
const handoffs = new Map<string, { value: PresentationHandoff; at: number }>();

export function stashPresentationHandoff(artifactId: string, value: PresentationHandoff): void {
  const now = Date.now();
  for (const [key, entry] of handoffs) if (now - entry.at > TTL_MS) handoffs.delete(key);
  handoffs.set(artifactId, { value, at: now });
}

/** Reads without removing, so a double-invoked initializer sees the same value; clear after mount. */
export function readPresentationHandoff(artifactId: string): PresentationHandoff | undefined {
  const entry = handoffs.get(artifactId);
  if (!entry || Date.now() - entry.at > TTL_MS) return undefined;
  return entry.value;
}

export function clearPresentationHandoff(artifactId: string): void {
  handoffs.delete(artifactId);
}

/**
 * Decks open in the editor. The Results list can briefly drop the edited version (a newer version
 * arrived, the list is being located again); the viewer stays mounted for an edited deck, so the
 * editor and its unsaved edits survive until the save reports the conflict (design §6.7).
 */
const editing = new Map<string, number>();

export function markPresentationEditing(artifactId: string, on: boolean): void {
  const count = (editing.get(artifactId) ?? 0) + (on ? 1 : -1);
  if (count > 0) editing.set(artifactId, count);
  else editing.delete(artifactId);
}

export function isPresentationEditing(artifactId: string): boolean {
  return editing.has(artifactId);
}
