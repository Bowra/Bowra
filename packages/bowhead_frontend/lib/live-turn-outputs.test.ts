import { describe, expect, it } from "vitest";

import type { StepOutputArtifact } from "@/components/chat/step-list/types";

import { liveTurnOutputs } from "./live-turn-outputs";
import type { Step } from "./turn-store";

const step = (outputs: StepOutputArtifact[]) => ({ outputArtifacts: outputs }) as unknown as Step;

describe("liveTurnOutputs (chat running state R2.1, R2.3)", () => {
  it("shows a delivered file as soon as it is saved and updates the same card for its next version", () => {
    const out = liveTurnOutputs([
      step([{ artifactId: "art_v1", filename: "deck.pptx", kind: "deliverable", lineageId: "art_v1", versionNo: 1 }]),
      step([{ artifactId: "art_csv", filename: "sales.csv", kind: "sandbox_output" }]),
      step([{ artifactId: "art_v2", filename: "deck.pptx", kind: "deliverable", lineageId: "art_v1", versionNo: 2 }]),
    ]);
    expect(out.files.map((card) => [card.key, card.output.artifactId, card.updated])).toEqual([["art_v1", "art_v2", true]]);
    expect(out.workFiles).toBe(1);
  });

  it("marks a new version of a file from an earlier Turn as updated", () => {
    const out = liveTurnOutputs([step([{ artifactId: "art_v3", filename: "page.html", kind: "deliverable", lineageId: "art_v1", versionNo: 3 }])]);
    expect(out.files[0]).toMatchObject({ key: "art_v1", updated: true });
  });

  it("counts work files once, leaves slide previews, uploads and unlabelled entries out", () => {
    const out = liveTurnOutputs([
      step([
        { artifactId: "art_out", kind: "tool_data", dataRole: "stdout" },
        { artifactId: "art_slide", kind: "deck_preview" },
        { artifactId: "art_up", kind: "user_upload" },
        { artifactId: "art_old_server", filename: "x.html" },
      ]),
      step([{ artifactId: "art_out", kind: "tool_data", dataRole: "stdout" }]),
    ]);
    expect(out).toEqual({ files: [], workFiles: 1 });
  });

  it("is empty before any step saved a file", () => {
    expect(liveTurnOutputs(undefined)).toEqual({ files: [], workFiles: 0 });
  });
});
