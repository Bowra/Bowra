import { readFileSync } from "node:fs";
import path from "node:path";

import { describe, expect, it } from "vitest";

import {
  canProjectResultsExplorerRoute,
  canPinResultsNavigator,
  explorerOwnsArtifactSelection,
  projectResultsRoute,
  reportResultsTitle,
  shouldResetResultsSession,
} from "./chat-workbench";

describe("Results Workspace navigator layout", () => {
  it("pins from measured results-pane width, never from viewport width alone", () => {
    expect(canPinResultsNavigator(859, false)).toBe(false);
    expect(canPinResultsNavigator(860, false)).toBe(true);
    expect(canPinResultsNavigator(1200, true)).toBe(false);
  });
});

describe("Results Workspace session identity", () => {
  it("clears an established session when transition passes through null", () => {
    expect(shouldResetResultsSession("s_1", null)).toBe(true);
  });

  it("clears a direct session replacement", () => {
    expect(shouldResetResultsSession("s_1", "s_2")).toBe(true);
  });

  it("preserves a deep link while the initial session hydrates", () => {
    expect(shouldResetResultsSession(null, "s_1")).toBe(false);
  });
});

describe("Results Workspace route projection", () => {
  it("keeps the exact artifact and viewer mode", () => {
    expect(projectResultsRoute({
      type: "artifact",
      artifactId: "art_exact",
      mode: "details",
      explicit: true,
      source: "canonical",
      surface: "viewer",
    }, false)).toEqual({
      tab: "data",
      surface: "viewer",
      artifactId: "art_exact",
      viewerMode: "details",
      unavailable: false,
      opened: true,
      revealPane: true,
    });
  });

  it("does not open an empty pane for the default report route", () => {
    expect(projectResultsRoute({
      type: "report",
      explicit: false,
      source: "default",
      surface: "viewer",
    }, false)).toMatchObject({
      tab: "report",
      opened: false,
      revealPane: false,
    });
  });

  it("reveals an explicitly selected report", () => {
    expect(projectResultsRoute({
      type: "report",
      explicit: true,
      source: "canonical",
      surface: "viewer",
    }, false)).toMatchObject({
      tab: "report",
      opened: true,
      revealPane: true,
    });
  });

  it("fails closed for code in report-only mode", () => {
    expect(projectResultsRoute({
      type: "code",
      explicit: true,
      source: "canonical",
      surface: "viewer",
    }, true)).toMatchObject({
      tab: "report",
      artifactId: null,
      unavailable: true,
      opened: true,
    });
  });

  it("keeps artifact deep links in report-only mode for authorization to decide", () => {
    expect(projectResultsRoute({
      type: "artifact",
      artifactId: "art_shared",
      mode: "preview",
      explicit: true,
      source: "canonical",
      surface: "viewer",
    }, true)).toMatchObject({
      tab: "data",
      artifactId: "art_shared",
      unavailable: false,
    });
  });

  it("only projects the Explorer surface when Scale is effective", () => {
    const route = {
      type: "explorer" as const,
      explicit: true as const,
      source: "canonical" as const,
      surface: "explorer" as const,
    };

    expect(projectResultsRoute(route, false, false)).toMatchObject({
      tab: "report",
      surface: "viewer",
      opened: true,
      revealPane: true,
    });
    expect(projectResultsRoute(route, false, true)).toMatchObject({
      tab: "report",
      surface: "explorer",
      opened: true,
      revealPane: true,
    });
  });

  it("keeps an explicit Explorer route pending until capabilities resolve", () => {
    expect(canProjectResultsExplorerRoute({ resolved: false, scaleNav: false })).toBe(true);
    expect(canProjectResultsExplorerRoute({ resolved: true, scaleNav: false })).toBe(false);
    expect(canProjectResultsExplorerRoute({ resolved: true, scaleNav: true })).toBe(true);
  });

  it("lets Explorer exclusively restore its selected artifact", () => {
    expect(explorerOwnsArtifactSelection({
      artifactId: "art_deep",
      scaleNav: true,
      selectionKey: "artifact:art_deep",
      surface: "explorer",
    })).toBe(true);
    expect(explorerOwnsArtifactSelection({
      artifactId: "art_deep",
      scaleNav: true,
      selectionKey: "artifact:art_other",
      surface: "explorer",
    })).toBe(false);
    expect(explorerOwnsArtifactSelection({
      artifactId: "art_deep",
      scaleNav: true,
      selectionKey: "artifact:art_deep",
      surface: "viewer",
    })).toBe(false);
  });
});

describe("Results Workspace report identity", () => {
  it("uses the current report H1 instead of the session/canvas prompt", () => {
    expect(reportResultsTitle(
      [
        { id: "old", kind: "h1", text: "旧报告", superseded: true },
        { id: "current", kind: "h1", text: "  商超零售经营尽调报告  " },
      ],
      "对本商超做一次经营尽调",
      "Report",
    )).toBe("商超零售经营尽调报告");
  });

  it("falls back to a meaningful canvas title and then the translated label", () => {
    expect(reportResultsTitle([], "  Q3 review  ", "Report")).toBe("Q3 review");
    expect(reportResultsTitle([], "   ", "Report")).toBe("Report");
  });

  // ★ P0 regression guard — caught by dev 1.0.77 dogfood (`dogfood-dev-1.0.77-2026-08-08.md`).
  //
  // `ReportSection`'s h1 variant declares required `text: string`, so typecheck passes;
  // but it really can be missing at runtime: when FE misses a section's
  // `section_added` on refresh/reconnect, `canvas-store.ts` late-join upsert takes a kind-bearing patch and
  // **forcibly inserts** it using `as unknown as ReportSection`; `kind:"h1"` without text creates this shape.
  //
  // Result is not wrong display but **the entire chat route crashing to a blank screen with `route_render_failed`**
  // (confirmed path: propose plan → refresh → approve execution; crashes accompanied by
  // `[canvas-reconcile] drift {localCount:4, serverCount:7}`）。
  //
  // Thus fixtures must **deliberately create type-invalid sections**, bypassing TS with `as unknown as`;
  // exactly what canvas-store's forced cast does at runtime. Valid types would guard nothing.
  it("★ h1 段缺 text 时不崩（类型说必填，运行时会缺——late-join upsert 强插的）", () => {
    const sections = [
      { id: "ghost", kind: "h1" }, // ← No text: shape created by late-join upsert.
      { id: "real", kind: "h1", text: "  商超零售经营尽调报告  " },
    ] as unknown as Parameters<typeof reportResultsTitle>[0];
    expect(() => reportResultsTitle(sections, null, "Report")).not.toThrow();
    // More than not crashing: skip the missing-text section and continue to find the real title.
    expect(reportResultsTitle(sections, null, "Report")).toBe("商超零售经营尽调报告");
  });

  it("★ 全部 h1 都缺 text 时回落，不返回 undefined/空串", () => {
    const sections = [
      { id: "a", kind: "h1" },
      { id: "b", kind: "h1" },
    ] as unknown as Parameters<typeof reportResultsTitle>[0];
    expect(reportResultsTitle(sections, null, "Report")).toBe("Report");
    expect(reportResultsTitle(sections, "Q4 review", "Report")).toBe("Q4 review");
  });
});

describe("Results Workspace mobile touch targets", () => {
  it("keeps the effective mobile Chat/Results tabs at least 44px high", () => {
    const css = readFileSync(
      path.resolve(__dirname, "../app/globals.css"),
      "utf8",
    );
    const rules = Array.from(
      css.matchAll(/\.v2-ws-mobile-tab\s*\{([^}]*)\}/g),
      (match) => match[1],
    );

    expect(rules.length).toBeGreaterThan(0);
    for (const rule of rules) expect(rule).toContain("min-height: 44px");
  });
});
