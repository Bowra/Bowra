import { afterEach, describe, expect, it } from "vitest";

import {
  getCanvasSnapshot,
  hydrateCanvas,
  resetCanvas,
} from "./canvas-store";

describe("hydrateCanvas empty report identity", () => {
  afterEach(() => resetCanvas());

  it("keeps the hydrated session identity without exposing an empty canvas", () => {
    hydrateCanvas({
      canvas: {
        id: "cv_hidden_only",
        sessionId: "s_hidden_only",
        title: "Chat-only internal canvas",
      },
      sections: [],
    });

    expect(getCanvasSnapshot()).toMatchObject({
      canvasId: null,
      sessionId: "s_hidden_only",
      title: "Chat-only internal canvas",
      status: "idle",
      sections: [],
    });
  });
});
