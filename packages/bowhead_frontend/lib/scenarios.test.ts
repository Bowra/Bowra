import { readdirSync, readFileSync } from "node:fs";
import path from "node:path";

import { describe, expect, it } from "vitest";

import { pickScenarioFromText, SCENARIOS } from "./scenarios";

describe("mock scenario routing", () => {
  it("does not revive the retired Debate scenario from natural-language intent", () => {
    const scenarioId = pickScenarioFromText("请从正反两面辩论一下这个方案");

    expect(scenarioId).not.toBe("debate");
    expect(SCENARIOS[scenarioId]).toBeDefined();
  });

  it("carries a configured Agent display name through the peer scenario", () => {
    const peerEvent = SCENARIOS.peer.timeline.find(
      (event) => event.kind === "peer_dispatched",
    );
    expect(peerEvent?.kind).toBe("peer_dispatched");
    if (!peerEvent || peerEvent.kind !== "peer_dispatched") return;
    expect(peerEvent.peer.displayName).toBe("营收分析专家");
  });
});

/**
 * scenarios.ts is a development / e2e mock fixture (untranslated), referenced only by mock branches. The following sites read it only with
 * NEXT_PUBLIC_BOWHEAD_SSE_MODE=mock; confirm new importers also read only in mock mode before adding them to the allowlist.
 */
const MOCK_ONLY_IMPORTERS = ["app/(app)/page.tsx", "lib/use-sessions.ts", "lib/use-turn-runner.ts"];

describe("mock scenario fixture boundary", () => {
  it("is imported only by the mock-mode call sites", () => {
    const root = path.resolve(__dirname, "..");
    const importers: string[] = [];
    const walk = (dir: string): void => {
      for (const entry of readdirSync(path.join(root, dir), { withFileTypes: true })) {
        const rel = path.posix.join(dir, entry.name);
        if (entry.isDirectory()) {
          if (entry.name === "node_modules" || entry.name.startsWith(".")) continue;
          walk(rel);
        } else if (/\.(tsx?|jsx?)$/.test(entry.name) && !/\.(test|spec)\./.test(entry.name)) {
          const source = readFileSync(path.join(root, rel), "utf8");
          if (/from\s+["'](?:@\/lib\/|\.\.?\/(?:[\w.-]+\/)*)scenarios["']/.test(source)) importers.push(rel);
        }
      }
    };
    for (const dir of ["app", "components", "lib", "hooks", "types"]) {
      try {
        walk(dir);
      } catch {
        // directory absent
      }
    }
    expect(importers.sort()).toEqual([...MOCK_ONLY_IMPORTERS].sort());
  });
});
