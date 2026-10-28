import type { Locator } from "@playwright/test";

/**
 * Layer-stacking probe (docs/design/组件统一-技术设计.md “图层”):
 * - `covered`: sample 5 points in the layer rectangle (center and corners, inset at least 4px and clear of the corner radius) with `elementFromPoint`;
 *   record hit-element descriptions where the hit is neither the layer nor its descendant. Empty array = uncovered.
 * - `clipped`: fraction of the layer rectangle's pixel area clipped by ancestor overflow (0 = fully visible).
 */
export interface LayerProbe {
  covered: string[];
  clipped: number;
  rect: { x: number; y: number; width: number; height: number };
}

export async function probeLayer(target: Locator): Promise<LayerProbe> {
  return target.evaluate((node) => {
    const el = node as HTMLElement;
    const rect = el.getBoundingClientRect();
    // Corner samples sit inside the rounded corner: a 4px inset alone falls outside a radius above ~13px.
    const radius = Number.parseFloat(getComputedStyle(el).borderTopLeftRadius) || 0;
    const inset = Math.max(4, Math.ceil(radius * (1 - Math.SQRT1_2)) + 2);
    const points: Array<[number, number]> = [
      [rect.left + rect.width / 2, rect.top + rect.height / 2],
      [rect.left + inset, rect.top + inset],
      [rect.right - inset, rect.top + inset],
      [rect.left + inset, rect.bottom - inset],
      [rect.right - inset, rect.bottom - inset],
    ];
    const describe = (hit: Element | null) => {
      if (!hit) return "(nothing)";
      const cls = typeof hit.className === "string" && hit.className ? `.${hit.className.trim().split(/\s+/).join(".")}` : "";
      const testId = hit.getAttribute("data-testid");
      return `${hit.tagName.toLowerCase()}${cls}${testId ? `[data-testid=${testId}]` : ""}`;
    };
    // Modal dialogs make the background inert; inert elements do not participate in hit testing, so a background layer above the dialog is
    // passed through by elementFromPoint and appears uncovered. Temporarily remove inert to measure visual stacking.
    const inertNodes = Array.from(document.querySelectorAll("[inert]"));
    for (const inertNode of inertNodes) inertNode.removeAttribute("inert");
    const covered: string[] = [];
    for (const [x, y] of points) {
      if (x < 0 || y < 0 || x >= window.innerWidth || y >= window.innerHeight) {
        covered.push(`(offscreen ${Math.round(x)},${Math.round(y)})`);
        continue;
      }
      const hit = document.elementFromPoint(x, y);
      if (!hit || (hit !== el && !el.contains(hit))) covered.push(describe(hit));
    }
    for (const inertNode of inertNodes) inertNode.setAttribute("inert", "");
    // Walk the containing-block chain: only overflow on containing blocks (and their containing blocks…) clips the element;
    // absolute skips unpositioned ancestors; fixed is clipped only by ancestors establishing containing blocks via transform / filter, etc.
    const establishesFixedCb = (style: CSSStyleDeclaration) =>
      style.transform !== "none" ||
      style.filter !== "none" ||
      (style.backdropFilter ?? "none") !== "none" ||
      style.perspective !== "none" ||
      /paint|layout|strict|content/.test(style.contain) ||
      /transform|filter|perspective/.test(style.willChange);
    const containingBlock = (child: HTMLElement): HTMLElement | null => {
      const position = getComputedStyle(child).position;
      for (let parent = child.parentElement; parent; parent = parent.parentElement) {
        if (parent === document.body || parent === document.documentElement) return null;
        const style = getComputedStyle(parent);
        if (position === "fixed") {
          if (establishesFixedCb(style)) return parent;
          continue;
        }
        if (position === "absolute") {
          if (style.position !== "static" || establishesFixedCb(style)) return parent;
          continue;
        }
        return parent;
      }
      return null;
    };
    let visible = { left: rect.left, top: rect.top, right: rect.right, bottom: rect.bottom };
    for (let cb = containingBlock(el); cb; cb = containingBlock(cb)) {
      const style = getComputedStyle(cb);
      const clipsX = style.overflowX !== "visible";
      const clipsY = style.overflowY !== "visible";
      if (!clipsX && !clipsY) continue;
      const box = cb.getBoundingClientRect();
      visible = {
        left: clipsX ? Math.max(visible.left, box.left) : visible.left,
        right: clipsX ? Math.min(visible.right, box.right) : visible.right,
        top: clipsY ? Math.max(visible.top, box.top) : visible.top,
        bottom: clipsY ? Math.min(visible.bottom, box.bottom) : visible.bottom,
      };
    }
    const area = rect.width * rect.height;
    const visibleArea = Math.max(0, visible.right - visible.left) * Math.max(0, visible.bottom - visible.top);
    return {
      covered,
      clipped: area > 0 ? Math.round((1 - visibleArea / area) * 100) / 100 : 0,
      rect: { x: rect.x, y: rect.y, width: rect.width, height: rect.height },
    };
  });
}
