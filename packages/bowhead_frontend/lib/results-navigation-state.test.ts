import { describe, expect, it, vi } from "vitest";

import {
  createResultsExplorerState,
  createResultsNavigationState,
  readResultsExplorerState,
  readResultsNavigationState,
  RESULTS_EXPLORER_HISTORY_KEY,
  RESULTS_NAVIGATION_HISTORY_KEY,
  withResultsHistoryState,
  withoutResultsHistoryState,
} from "./results-navigation-state";

const expected = { sessionId: "s_1", scope: "session" as const };

describe("Results namespaced history state", () => {
  it("preserves unrelated shell state while adding navigation and Explorer state", () => {
    const navigation = createResultsNavigationState({
      ...expected,
      entryId: "results_nav_1",
    });
    const explorer = createResultsExplorerState({
      ...expected,
      entryId: "results_explorer_1",
    });
    const merged = withResultsHistoryState(
      { nextRouter: { tree: "keep" }, bowheadPersonalSettingsModal: true },
      navigation,
      explorer,
    );

    expect(merged.nextRouter).toEqual({ tree: "keep" });
    expect(merged.bowheadPersonalSettingsModal).toBe(true);
    expect(merged[RESULTS_NAVIGATION_HISTORY_KEY]).toEqual(navigation);
    expect(merged[RESULTS_EXPLORER_HISTORY_KEY]).toEqual(explorer);
  });

  it("round-trips only the matching authenticated session and scope", () => {
    const navigation = createResultsNavigationState({
      ...expected,
      entryId: "results_nav_1",
      openingContext: {
        type: "report-inspection",
        reportEntryId: "results_report_1",
        anchor: "section_1",
        focusToken: "chart_1",
      },
    });
    const explorer = {
      ...createResultsExplorerState({
        ...expected,
        entryId: "results_explorer_1",
      }),
      query: "revenue",
      selected_key: "artifact:art_1",
      anchor: { key: "artifact:art_1", offset_px: 18 },
    };
    const state = withResultsHistoryState({}, navigation, explorer);

    expect(readResultsNavigationState(state, expected)).toEqual(navigation);
    expect(readResultsExplorerState(state, expected)).toEqual(explorer);
    expect(readResultsNavigationState(state, {
      sessionId: "s_other",
      scope: "session",
    })).toBeNull();
    expect(readResultsExplorerState(state, {
      sessionId: "s_1",
      scope: "report",
    })).toBeNull();
  });

  it("fails closed for malformed or overlong state", () => {
    const state = {
      [RESULTS_EXPLORER_HISTORY_KEY]: {
        ...createResultsExplorerState({
          ...expected,
          entryId: "results_explorer_1",
        }),
        query: "x".repeat(121),
      },
      [RESULTS_NAVIGATION_HISTORY_KEY]: {
        ...createResultsNavigationState({
          ...expected,
          entryId: "results_nav_1",
        }),
        opening_context: {
          type: "chat",
          messageToken: "unsafe token",
          panelMode: "chat",
        },
      },
    };

    expect(readResultsExplorerState(state, expected)).toBeNull();
    expect(readResultsNavigationState(state, expected)).toBeNull();
  });

  it("keeps Chat shell shape and reads pre-N4 v1 state conservatively", () => {
    const split = createResultsNavigationState({
      ...expected,
      entryId: "results_chat_split",
      openingContext: {
        type: "chat",
        messageToken: "turn_1",
        panelMode: "split",
      },
    });
    expect(readResultsNavigationState({
      [RESULTS_NAVIGATION_HISTORY_KEY]: split,
    }, expected)?.opening_context).toEqual({
      type: "chat",
      messageToken: "turn_1",
      panelMode: "split",
    });

    const legacyChat = {
      ...split,
      opening_context: { type: "chat", messageToken: "turn_1" },
    };
    expect(readResultsNavigationState({
      [RESULTS_NAVIGATION_HISTORY_KEY]: legacyChat,
    }, expected)?.opening_context).toEqual({
      type: "chat",
      messageToken: "turn_1",
      panelMode: "chat",
    });

    expect(readResultsNavigationState({
      [RESULTS_NAVIGATION_HISTORY_KEY]: {
        ...split,
        opening_context: {
          type: "chat",
          messageToken: "turn_1",
          panelMode: "canvas",
        },
      },
    }, expected)).toBeNull();
  });

  it("counts Unicode code points instead of UTF-16 units in Explorer queries", () => {
    const explorer = createResultsExplorerState({
      ...expected,
      entryId: "results_explorer_emoji",
    });
    const validState = {
      [RESULTS_EXPLORER_HISTORY_KEY]: {
        ...explorer,
        query: "😀".repeat(120),
      },
    };
    const invalidState = {
      [RESULTS_EXPLORER_HISTORY_KEY]: {
        ...explorer,
        query: "😀".repeat(121),
      },
    };

    expect(readResultsExplorerState(validState, expected)?.query).toHaveLength(240);
    expect(readResultsExplorerState(invalidState, expected)).toBeNull();
  });

  it("clears only Results namespaces on session reset", () => {
    const cleared = withoutResultsHistoryState({
      shell: "keep",
      [RESULTS_NAVIGATION_HISTORY_KEY]: { stale: true },
      [RESULTS_EXPLORER_HISTORY_KEY]: { stale: true },
    });

    expect(cleared).toEqual({ shell: "keep" });
  });

  it("uses a non-content entry identity and does not persist result metadata", () => {
    vi.stubGlobal("crypto", { randomUUID: () => "uuid-1" });
    const navigation = createResultsNavigationState(expected);
    const explorer = createResultsExplorerState(expected);

    expect(navigation.entry_id).toBe("results_uuid-1");
    expect(Object.keys(explorer).sort()).toEqual([
      "anchor",
      "entry_id",
      "expanded_groups",
      "filters",
      "focus_key",
      "query",
      "scope",
      "selected_key",
      "session_id",
      "v",
    ]);
    expect(JSON.stringify(explorer)).not.toMatch(/filename|content|cursor|url/i);
    expect(explorer.expanded_groups).toEqual(["deliverables", "evidence"]);
    vi.unstubAllGlobals();
  });
});
