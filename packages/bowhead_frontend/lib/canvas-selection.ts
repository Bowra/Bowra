export interface CanvasSelection {
  key: string;
  kind: "agent_draft" | "agent_entry" | "settings" | "operation" | "resource" | "document_draft";
}

/** Resolve only retained, admitted views; selection is never domain write authority. */
export function resolveCanvasSelection(
  objects: readonly CanvasSelection[],
  preferredKeys: readonly (string | null | undefined)[],
): CanvasSelection | null {
  for (const key of preferredKeys) {
    const object = objects.find((candidate) => candidate.key === key);
    if (object) return object;
  }
  return objects[0] ?? null;
}
