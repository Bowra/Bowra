/**
 * Layer calculation (docs/design/组件统一-技术设计.md, “图层”). Inline styles alone let jsdom getComputedStyle
 * read the values; e2e/layer-stacking.spec.ts (chromium + webkit) verifies real-page stacking.
 */
import { afterEach, describe, expect, it } from "vitest";

import { liftedZIndex, rootStackingLayer } from "./layer";

afterEach(() => {
  document.body.innerHTML = "";
});

function el(style: string, parent: HTMLElement = document.body): HTMLElement {
  const node = document.createElement("div");
  node.setAttribute("style", style);
  parent.appendChild(node);
  return node;
}

describe("rootStackingLayer", () => {
  it("takes the z-index of the outermost stacking context, not the nearest number", () => {
    // Outer fixed without z-index (like .v2-app-root): even 9999 inside stays in layer 0.
    const app = el("position: fixed; inset: 0");
    const inner = el("position: fixed; z-index: 9999", app);
    const button = el("", inner);
    expect(rootStackingLayer(button)).toBe(0);
  });

  it("reads a portaled overlay's own z-index", () => {
    const drawer = el("position: fixed; inset: 0; z-index: 9998");
    const button = el("", el("", drawer));
    expect(rootStackingLayer(button)).toBe(9998);
  });

  it("ignores positioned ancestors without z-index", () => {
    const wrap = el("position: relative");
    expect(rootStackingLayer(el("", wrap))).toBe(0);
  });
});

describe("liftedZIndex", () => {
  it("lifts an overlay that is not above its anchor's root layer", () => {
    const host = el("position: fixed; z-index: 9999");
    const anchor = el("", host);
    const dialog = el("position: fixed; z-index: 1300");
    expect(liftedZIndex(dialog, [anchor])).toBe(10000);
  });

  it("keeps the overlay's own layer when it is already higher", () => {
    const host = el("position: fixed; z-index: 1200");
    const anchor = el("", host);
    const menu = el("position: fixed; z-index: 10100");
    expect(liftedZIndex(menu, [anchor])).toBeNull();
  });

  it("uses the highest anchor and skips detached ones", () => {
    const low = el("", el("position: fixed; z-index: 50"));
    const high = el("", el("position: fixed; z-index: 2000"));
    const detached = document.createElement("div");
    const dialog = el("position: fixed; z-index: 1300");
    expect(liftedZIndex(dialog, [low, detached, high, null])).toBe(2001);
  });
});
