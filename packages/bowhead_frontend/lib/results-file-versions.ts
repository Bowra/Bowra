import type { ArtifactItem } from "@/components/canvas/data-tab-files";

import type { ResultItemDTO } from "./results-api";
import {
  readRepresentation,
  representationPath,
  type ArtifactRepresentation,
} from "./use-artifact-representation";
import { artifactItemFromResultDTO } from "./results-workspace-model";

/**
 * File versions (docs/sprints/file-versions-2026-11-10/tech-design.md R2): Results
 * lists a revised file once and shows its latest version. An earlier chat card
 * still opens the version it delivered, so the viewer needs that version's own
 * item while Results keeps the file's entry selected.
 */
export type EarlierFileVersion = {
  sessionId: string;
  entryKey: string;
  item: ArtifactItem;
};

/** Whether a located entry is the requested file's entry showing another version. */
export function isEarlierFileVersion(artifactId: string, entry: ResultItemDTO | null): entry is ResultItemDTO {
  return Boolean(
    entry?.lineage
    && entry.route.type === "artifact"
    && entry.route.artifact_id !== artifactId,
  );
}

/**
 * The viewer item for an earlier version. Its own metadata comes from that
 * version's details; origin and trust keep the entry's catalog projection, as
 * every version of one file comes from the same producer.
 */
export function earlierFileVersionItem(
  artifactId: string,
  entry: ResultItemDTO,
  details: ArtifactRepresentation["details"],
): ArtifactItem | null {
  const shown = artifactItemFromResultDTO(entry);
  if (!shown || !isEarlierFileVersion(artifactId, entry) || details?.id !== artifactId) return null;
  if (details.mime !== shown.mime) return null;
  return {
    ...shown,
    id: details.id,
    filename: details.filename,
    mime: details.mime,
    size: details.size,
    source: details.source,
    created_at: details.created_at,
    kind: details.kind ?? shown.kind,
    producer_kind: details.producer_kind ?? shown.producer_kind,
    created_by_turn: null,
  };
}

/** Reads an earlier version's details under the current Results scope. */
export async function loadEarlierFileVersion({
  artifactId,
  entry,
  scope,
  sessionId,
  signal,
}: {
  artifactId: string;
  entry: ResultItemDTO;
  scope: "session" | "report";
  sessionId: string;
  signal: AbortSignal;
}): Promise<EarlierFileVersion | null> {
  const details = await readRepresentation(
    representationPath(sessionId, artifactId, scope, "details"),
    "details",
    signal,
  );
  const item = earlierFileVersionItem(artifactId, entry, details.details);
  return item ? { sessionId, entryKey: entry.key, item } : null;
}
