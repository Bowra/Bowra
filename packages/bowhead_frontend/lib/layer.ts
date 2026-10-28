/**
 * Overlay layers (docs/design/组件统一-技术设计.md “图层”).
 *
 * app/globals.css `--z-*` variables define layer values; only nested-overlay elevation calculations live here:
 * whether a body-mounted overlay covers its opening location depends on the z-index of that location's **root stacking context**,
 * rather than the opening element's or nearest ancestor's own number; once any ancestor establishes a stacking context,
 * no number inside escapes it (e.g. fixed `.v2-app-root`, isolated `.canvas-report-shell`).
 */

const STACKING_WILL_CHANGE = /(^|,)\s*(transform|opacity|filter|backdrop-filter|perspective|clip-path|mask|isolation|mix-blend-mode|z-index|position)\s*(,|$)/;

function isNone(value: string | undefined | null): boolean {
  return !value || value === "none";
}

/** Whether the element establishes a stacking context (common CSS-spec conditions). */
export function establishesStackingContext(style: CSSStyleDeclaration, parentStyle: CSSStyleDeclaration | null): boolean {
  const position = style.position;
  const hasZ = style.zIndex !== "auto" && style.zIndex !== "";
  if (position === "fixed" || position === "sticky") return true;
  if ((position === "relative" || position === "absolute") && hasZ) return true;
  if (hasZ && parentStyle && /(^|-)(flex|grid)$/.test(parentStyle.display)) return true;
  const opacity = Number.parseFloat(style.opacity);
  if (Number.isFinite(opacity) && opacity < 1) return true;
  if (!isNone(style.transform) || !isNone(style.filter) || !isNone(style.perspective)) return true;
  if (!isNone(style.getPropertyValue("backdrop-filter")) || !isNone(style.getPropertyValue("-webkit-backdrop-filter"))) return true;
  if (!isNone(style.clipPath) || !isNone(style.getPropertyValue("mask-image"))) return true;
  if (style.isolation === "isolate") return true;
  if (style.mixBlendMode && style.mixBlendMode !== "normal") return true;
  if (style.willChange && STACKING_WILL_CHANGE.test(style.willChange)) return true;
  if (/(layout|paint|strict|content)/.test(style.contain ?? "")) return true;
  return false;
}

/**
 * Element's layer in the root stacking context: walk upward to the outermost stacking-context ancestor (including itself) and take its
 * z-index (no numeric value counts as 0). A body-mounted overlay needs z-index at least one higher to cover this element.
 */
export function rootStackingLayer(element: Element | null | undefined): number {
  if (!element || typeof window === "undefined") return 0;
  let layer = 0;
  let node: Element | null = element;
  while (node && node !== document.documentElement && node !== document.body) {
    const parent: Element | null = node.parentElement;
    const style = window.getComputedStyle(node);
    const parentStyle = parent && parent !== document.documentElement ? window.getComputedStyle(parent) : null;
    if (establishesStackingContext(style, parentStyle)) {
      const z = Number.parseInt(style.zIndex, 10);
      layer = Number.isFinite(z) ? z : 0;
    }
    node = parent;
  }
  return layer;
}

/**
 * If the overlay's own z-index is no higher than any anchor's root layer (parent overlay, opening element), return
 * highest anchor layer + 1; otherwise null means retain its own layer value.
 */
export function liftedZIndex(own: HTMLElement, anchors: Array<Element | null | undefined>): number | null {
  const floor = Math.max(0, ...anchors.filter((anchor): anchor is Element => Boolean(anchor?.isConnected)).map(rootStackingLayer));
  if (floor <= 0) return null;
  const current = Number.parseInt(window.getComputedStyle(own).zIndex, 10);
  const ownLayer = Number.isFinite(current) ? current : 0;
  return floor >= ownLayer ? floor + 1 : null;
}
