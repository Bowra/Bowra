import { describe, expect, it } from "vitest";

import {
  composerKeyboardInset,
  composerVisualViewportInsets,
} from "./use-composer-visual-viewport";

describe("composerKeyboardInset", () => {
  it("lifts a focused composer above an overlay keyboard", () => {
    expect(composerKeyboardInset({
      layoutHeight: 844,
      visualHeight: 480,
      offsetTop: 44,
      scale: 1,
      textareaFocused: true,
    })).toBe(320);
  });

  it("ignores address-bar movement, pinch zoom, and non-input focus", () => {
    const base = { layoutHeight: 844, visualHeight: 800, offsetTop: 0 };
    expect(composerKeyboardInset({ ...base, scale: 1, textareaFocused: true })).toBe(0);
    expect(composerKeyboardInset({ ...base, visualHeight: 420, scale: 1.2, textareaFocused: true })).toBe(0);
    expect(composerKeyboardInset({ ...base, visualHeight: 420, scale: 1, textareaFocused: false })).toBe(0);
  });
});

describe("composerVisualViewportInsets", () => {
  it("keeps modal viewport bounds independent from textarea focus", () => {
    expect(composerVisualViewportInsets({
      layoutHeight: 844,
      visualHeight: 480,
      offsetTop: 44,
    })).toEqual({ top: 44, bottom: 320 });
  });

  it("rounds browser viewport geometry and never returns negative insets", () => {
    expect(composerVisualViewportInsets({
      layoutHeight: 844,
      visualHeight: 820.4,
      offsetTop: 24.2,
    })).toEqual({ top: 24, bottom: 0 });
  });
});
