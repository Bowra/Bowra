import { afterEach, describe, expect, it, vi } from "vitest";
// The two telemetry channels were consolidated into `./metrics` (2026-08-08); interactions use trackInteraction.
import { trackInteraction as track } from "./metrics";

import {
  CANVAS_FOCUS_DATA_EVENT,
  CANVAS_OPEN_RESULT_EVENT,
  requestCanvasDataFocus,
  requestCanvasResultOpen,
  workspaceDataFocusHref,
  type CanvasFocusDataDetail,
  type CanvasOpenResultDetail,
} from "./canvas-workspace-focus";

vi.mock("./metrics", async (importOriginal) => ({
  ...(await importOriginal<typeof import("./metrics")>()),
  trackInteraction: vi.fn(),
}));

afterEach(() => {
  vi.mocked(track).mockClear();
  window.__BOWHEAD_PUBLIC_CONTEXT__ = undefined;
});

describe("Results Workspace entry events", () => {
  it("keeps the exact artifact ID and additive navigation options", () => {
    const listener = vi.fn();
    window.addEventListener(CANVAS_FOCUS_DATA_EVENT, listener);

    requestCanvasDataFocus("art_exact:1", {
      entryPoint: "report",
      history: "replace",
      mode: "details",
      reportContext: {
        label: "各市场净营收",
        sectionId: "sec_market_revenue",
      },
    });

    expect(listener).toHaveBeenCalledTimes(1);
    const event = listener.mock.calls[0][0] as CustomEvent<CanvasFocusDataDetail>;
    expect(event.detail).toEqual({
      artifactId: "art_exact:1",
      entryPoint: "report",
      history: "replace",
      mode: "details",
      reportContext: {
        label: "各市场净营收",
        sectionId: "sec_market_revenue",
      },
    });
    expect(track).toHaveBeenCalledWith(
      "bowhead_canvas_data_focus_total",
      {
        action: "request",
        entry_point: "report",
        result_type: "artifact",
      },
    );
    window.removeEventListener(CANVAS_FOCUS_DATA_EVENT, listener);
  });

  it("opens non-artifact composite results through the generic event", () => {
    const listener = vi.fn();
    window.addEventListener(CANVAS_OPEN_RESULT_EVENT, listener);

    requestCanvasResultOpen(
      { type: "code" },
      { entryPoint: "source_panel" },
    );

    const event = listener.mock.calls[0][0] as CustomEvent<CanvasOpenResultDetail>;
    expect(event.detail).toEqual({
      target: { type: "code" },
      entryPoint: "source_panel",
    });
    expect(track).toHaveBeenCalledWith(
      "bowhead_canvas_data_focus_total",
      {
        action: "request",
        entry_point: "source_panel",
        result_type: "code",
      },
    );
    window.removeEventListener(CANVAS_OPEN_RESULT_EVENT, listener);
  });

  it("builds canonical deep links and preserves the original ID", () => {
    expect(workspaceDataFocusHref("s_1", "art_exact:1")).toBe(
      "/?session=s_1&result=art_exact%3A1&mode=preview",
    );
  });
});
