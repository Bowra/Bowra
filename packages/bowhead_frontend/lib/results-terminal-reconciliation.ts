import type { ArtifactItem } from "@/components/canvas/data-tab-files";

function isPrimaryDeliverable(item: ArtifactItem): boolean {
  return item.kind === "deliverable" || item.kind === "canvas_export";
}

/** Return the sole new, turn-owned primary deliverable eligible for auto-open. */
export function terminalAutoOpenArtifact(
  baselineIds: ReadonlySet<string>,
  snapshot: readonly ArtifactItem[],
  turnId: string,
): ArtifactItem | null {
  const candidates = snapshot.filter((item) =>
    !baselineIds.has(item.id)
    && item.created_by_turn === turnId
    && isPrimaryDeliverable(item),
  );
  return candidates.length === 1 ? candidates[0] ?? null : null;
}
