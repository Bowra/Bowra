/**
 * Chat-workbench shortcuts (Workbench Spec §9).
 *
 * Maximize / restore results: ⌘⇧\ on macOS, Ctrl+Shift+\ on Windows / Linux.
 * - Check physical `code === "Backslash"`: with Shift, `key` is “|” on US keyboards,
 *   or “、” with Chinese IME punctuation; checking `key` alone misses it.
 * - Require Shift: sidebar ⌘\ / Ctrl+\ (unified-sidebar) omits Shift, keeping shortcuts distinct;
 *   the sidebar also skips shifted combinations, avoiding simultaneous triggers on layouts still reporting `key` as “\”.
 * - Exclude Alt (avoid IME/system-shortcut conflicts) and IME composition (isComposing / keyCode 229).
 */
export interface ShortcutEventLike {
  key: string;
  code?: string;
  metaKey: boolean;
  ctrlKey: boolean;
  shiftKey: boolean;
  altKey: boolean;
  isComposing?: boolean;
  keyCode?: number;
}

export function isResultsMaximizeShortcut(event: ShortcutEventLike): boolean {
  if (event.isComposing || event.keyCode === 229) return false;
  if (!(event.metaKey || event.ctrlKey)) return false;
  if (!event.shiftKey || event.altKey) return false;
  return event.code === "Backslash" || event.key === "\\" || event.key === "|";
}

const OVERLAY_SELECTOR = '[aria-modal="true"], [role="dialog"], [role="alertdialog"], [role="menu"], [role="listbox"]';

/**
 * Whether any overlay is open: modal/nonmodal dialogs (source popovers, rewrite bars, recent work, etc. with role=dialog),
 * menus and dropdown/autocomplete lists. Yield ⌘⇧\ to them (never silently change layout behind an overlay).
 * Count visible ones only: exclude `hidden` / `aria-hidden` subtrees, display:none and visibility:hidden.
 */
export function hasOpenOverlay(doc: Document = document): boolean {
  const view = doc.defaultView;
  return Array.from(doc.querySelectorAll<HTMLElement>(OVERLAY_SELECTOR)).some((el) => {
    if (el.closest('[hidden], [aria-hidden="true"]')) return false;
    if (!view) return true;
    for (let node: HTMLElement | null = el; node; node = node.parentElement) {
      const cs = view.getComputedStyle(node);
      if (cs.display === "none" || cs.visibility === "hidden") return false;
    }
    return true;
  });
}

/** Label in button hints/help: symbols on macOS, Ctrl+Shift+key elsewhere; default key is US-keyboard “\”. */
export function resultsMaximizeShortcutLabel(platform: string | undefined, key = "\\"): string {
  return /mac|iphone|ipad|ipod/i.test(platform ?? "") ? `⌘⇧${key}` : `Ctrl+Shift+${key}`;
}

/**
 * `aria-keyshortcuts` uses the same key as button hints: the printed character on the “\ key” when layout is available,
 * otherwise US-keyboard “\”. Use WAI-ARIA modifier names (Meta / Control / Shift); for a “+” or space key,
 * use Plus / Space per specification (otherwise interpreted as a separator).
 */
export function resultsMaximizeAriaKeyshortcuts(platform: string | undefined, key = "\\"): string {
  const name = key === "+" ? "Plus" : key === " " ? "Space" : key;
  return /mac|iphone|ipad|ipod/i.test(platform ?? "") ? `Meta+Shift+${name}` : `Control+Shift+${name}`;
}

/**
 * Character printed on the “\ key” (physical Backslash) in the current layout. Only Chromium-family browsers expose
 * `navigator.keyboard.getLayoutMap()`; return null if unavailable, so callers identify the US-keyboard \ position in hints.
 */
export async function backslashKeyOnLayout(
  nav: Navigator | undefined = typeof navigator === "undefined" ? undefined : navigator,
): Promise<string | null> {
  const keyboard = (nav as (Navigator & { keyboard?: { getLayoutMap?: () => Promise<Map<string, string>> } }) | undefined)?.keyboard;
  if (!keyboard?.getLayoutMap) return null;
  try {
    const key = (await keyboard.getLayoutMap()).get("Backslash");
    return key && key.trim() ? key : null;
  } catch {
    return null;
  }
}
