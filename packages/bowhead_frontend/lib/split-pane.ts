// layout-redesign P3 — pure split-ratio math for the desktop chat|canvas SplitPane.
// React-free on purpose so the clamp/snap logic is unit-testable without a DOM.
// Grounded in tech-design §3.3 (review P2: clamp in PIXELS, not a fixed fraction —
// the split area already subtracts rail 56 + ctx 260, so px thresholds map to a
// window-dependent fraction) + the v10 shell.jsx Divider (default chat fraction 0.4).

export const SPLIT_RATIO_KEY = "bowhead.split_ratio";
export const DEFAULT_SPLIT_RATIO = 0.4; // chat fraction in split (v10 shell.jsx:35)
export const MIN_CHAT_PX = 360;
export const MIN_CANVAS_PX = 420;
/** Drag this far PAST a min (in px) → snap to the single-pane state instead of clamping. */
export const SNAP_SLACK_PX = 56;

export type SplitResolve =
  | { kind: "ratio"; ratio: number }
  | { kind: "snap"; to: "chat" | "canvas" };

/**
 * Map a pointer x during a divider drag to either a clamped chat fraction or a snap intent.
 * - `to: "canvas"` → chat dragged below its min → canvas takes over (canvas-full).
 * - `to: "chat"`   → canvas dragged below its min → chat takes over (chat-only).
 * Otherwise the chat width is clamped to `[minChat, areaWidth - minCanvas]` and returned as a fraction.
 */
export function resolveSplit(
  clientX: number,
  areaLeft: number,
  areaWidth: number,
  opts: { minChat?: number; minCanvas?: number; snapSlack?: number } = {},
): SplitResolve {
  const minChat = opts.minChat ?? MIN_CHAT_PX;
  const minCanvas = opts.minCanvas ?? MIN_CANVAS_PX;
  const slack = opts.snapSlack ?? SNAP_SLACK_PX;
  const chatPx = clientX - areaLeft;
  if (chatPx < minChat - slack) return { kind: "snap", to: "canvas" };
  if (areaWidth - chatPx < minCanvas - slack) return { kind: "snap", to: "chat" };
  const hi = Math.max(minChat, areaWidth - minCanvas); // guard degenerate (area < minChat+minCanvas)
  const clampedChat = Math.min(hi, Math.max(minChat, chatPx));
  return { kind: "ratio", ratio: clampedChat / areaWidth };
}

/** Read the persisted chat fraction (SSR-safe); falls back to default for missing/invalid/out-of-range. */
export function readSplitRatio(): number {
  if (typeof window === "undefined") return DEFAULT_SPLIT_RATIO;
  try {
    const raw = window.localStorage.getItem(SPLIT_RATIO_KEY);
    const n = raw == null ? NaN : Number(raw);
    return Number.isFinite(n) && n > 0 && n < 1 ? n : DEFAULT_SPLIT_RATIO;
  } catch {
    return DEFAULT_SPLIT_RATIO;
  }
}

export function writeSplitRatio(ratio: number): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(SPLIT_RATIO_KEY, String(ratio));
  } catch {
    /* privacy mode / quota — ignore, ratio just won't persist */
  }
}

/** Split-divider keyboard-resize step in pixels (Workbench Spec §9: ←/→ each move 16px). */
export const SPLIT_KEY_STEP_PX = 16;

/** Allowed chat-column pixel range for current area width (minimum widths 360 / 420; if too narrow, collapse both bounds to one point). */
export function chatWidthBounds(
  areaWidth: number,
  opts: { minChat?: number; minCanvas?: number } = {},
): { min: number; max: number } {
  const minChat = opts.minChat ?? MIN_CHAT_PX;
  const minCanvas = opts.minCanvas ?? MIN_CANVAS_PX;
  return { min: minChat, max: Math.max(minChat, areaWidth - minCanvas) };
}

/**
 * Keyboard resize: arrows step 16px; Home / End select chat-column minimum / maximum width.
 * Clamp within bounds, unlike dragging below minimum into single-column mode—holding an arrow must not hide an entire column
 * (HIG split views: never let the divider disappear). Return null for unknown area width (0) or other keys.
 */
export function keyboardSplitRatio(
  key: string,
  chatPx: number,
  areaWidth: number,
  opts: { minChat?: number; minCanvas?: number; step?: number } = {},
): number | null {
  if (!(areaWidth > 0)) return null;
  const { min, max } = chatWidthBounds(areaWidth, opts);
  const step = opts.step ?? SPLIT_KEY_STEP_PX;
  let next: number;
  switch (key) {
    case "ArrowLeft":
      next = chatPx - step;
      break;
    case "ArrowRight":
      next = chatPx + step;
      break;
    case "Home":
      next = min;
      break;
    case "End":
      next = max;
      break;
    default:
      return null;
  }
  return Math.min(max, Math.max(min, next)) / areaWidth;
}
