import { describe, expect, it } from "vitest";

import type { ArtifactItem } from "@/components/canvas/data-tab-files";
import { terminalAutoOpenArtifact } from "./results-terminal-reconciliation";

function artifact(overrides: Partial<ArtifactItem>): ArtifactItem {
  return {
    id: "art_1",
    filename: "result.pdf",
    mime: "application/pdf",
    size: 1,
    source: "canvas_export",
    created_at: "2026-08-07T00:00:00Z",
    preview_available: true,
    kind: "deliverable",
    created_by_turn: "turn_1",
    ...overrides,
  };
}

describe("terminalAutoOpenArtifact", () => {
  it("returns one new primary deliverable owned by the terminal turn", () => {
    const result = terminalAutoOpenArtifact(
      new Set(["art_old"]),
      [
        artifact({ id: "art_old" }),
        artifact({ id: "art_data", kind: "tool_data" }),
        artifact({ id: "art_new" }),
      ],
      "turn_1",
    );
    expect(result?.id).toBe("art_new");
  });

  it("does not guess when ownership is missing or multiple deliverables exist", () => {
    expect(terminalAutoOpenArtifact(
      new Set(),
      [artifact({ id: "art_unknown", created_by_turn: null })],
      "turn_1",
    )).toBeNull();
    expect(terminalAutoOpenArtifact(
      new Set(),
      [artifact({ id: "art_a" }), artifact({ id: "art_b" })],
      "turn_1",
    )).toBeNull();
  });
});
