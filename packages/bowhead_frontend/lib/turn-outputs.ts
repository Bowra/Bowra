/**
 * turn-outputs — what a finished Turn produced, counted by kind (turn visibility FR12).
 *
 * The backend stores `outputs` with the answer (`lead.final.payload`), so live, reloaded and replayed views
 * read the same counts. A payload without it (earlier Turns, older servers) yields [], and the finished line
 * keeps its old wording.
 */

export const TURN_OUTPUT_KINDS = [
  "report",
  "chart",
  "web_page",
  "presentation",
  "pdf",
  "document",
  "spreadsheet",
  "image",
  "file",
] as const;

export type TurnOutputKind = (typeof TURN_OUTPUT_KINDS)[number];

export interface TurnOutput {
  kind: TurnOutputKind;
  count: number;
}

const KNOWN = new Set<string>(TURN_OUTPUT_KINDS);

/** The output counts of a final payload, in the backend's order; unknown kinds and bad counts are dropped. */
export function extractTurnOutputs(finalPayload: unknown): TurnOutput[] {
  if (!finalPayload || typeof finalPayload !== "object") return [];
  const outputs = (finalPayload as { outputs?: unknown }).outputs;
  if (!Array.isArray(outputs)) return [];
  return outputs.flatMap((item): TurnOutput[] => {
    if (!item || typeof item !== "object") return [];
    const { kind, count } = item as { kind?: unknown; count?: unknown };
    if (typeof kind !== "string" || !KNOWN.has(kind)) return [];
    if (typeof count !== "number" || !Number.isInteger(count) || count <= 0) return [];
    return [{ kind: kind as TurnOutputKind, count }];
  });
}
