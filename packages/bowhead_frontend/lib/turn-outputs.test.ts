/** Turn visibility FR12: output counts are read from the stored answer; anything malformed is dropped. */
import { describe, expect, it } from "vitest";

import { extractTurnOutputs } from "./turn-outputs";

describe("extractTurnOutputs", () => {
  it("reads the stored counts in order", () => {
    expect(extractTurnOutputs({ outputs: [{ kind: "report", count: 1 }, { kind: "chart", count: 3 }] })).toEqual([
      { kind: "report", count: 1 },
      { kind: "chart", count: 3 },
    ]);
  });

  it("drops unknown kinds and bad counts, and treats a missing field as none", () => {
    expect(
      extractTurnOutputs({
        outputs: [{ kind: "hologram", count: 1 }, { kind: "chart", count: 0 }, { kind: "image", count: 1.5 }, { kind: "pdf", count: "2" }, null, { kind: "pdf", count: 2 }],
      }),
    ).toEqual([{ kind: "pdf", count: 2 }]);
    for (const payload of [undefined, null, "text", {}, { outputs: "x" }]) expect(extractTurnOutputs(payload)).toEqual([]);
  });
});
