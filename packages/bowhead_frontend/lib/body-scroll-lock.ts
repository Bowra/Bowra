/**
 * Reference-counted page scroll lock: shared by mobile session drawers, sidebar overlays, `DialogShell` dialogs and UI-refresh menu bottom sheets.
 *
 * Why unify: dialogs previously used floating-ui `FloatingOverlay` `lockScroll`, which unconditionally wrote
 * `body.style.overflow = ''` on unlock; opening and closing a dialog (or menu bottom sheet) inside an open drawer also
 * cleared the drawer's lock, letting the background scroll again. Count holders here, restoring the pre-lock value only after the last releases.
 *
 * Both holder kinds write `overflow: hidden !important`: on narrow screens (< 1200), `globals.css` permits body
 * scrolling with `!important`, so a lock without important cannot override it on phones (old dialogs copied floating-ui without important, allowing
 * the background to scroll behind an open mobile dialog). Desktop body already has `overflow: hidden`; important leaves its computed value unchanged.
 * - Drawers/sidebar overlays (default): lock only, without compensation.
 * - Dialog overlays (`compensate`): additionally reuse floating-ui's original compensation—measure scrollbar width before adding `padding` and
 *   `--floating-ui-scrollbar-width`; fix body position on iOS (which ignores body's `overflow: hidden`) and restore
 *   the original scroll position on unlock. Count compensation separately, removing it when the last compensating holder releases.
 */

let plainCount = 0;
let compensateCount = 0;
let savedOverflow: { value: string; priority: string } | null = null;
let releaseCompensation: (() => void) | null = null;

const SCROLLBAR_PROPERTY = "--floating-ui-scrollbar-width";

function applyCompensation(): () => void {
  const platform =
    (navigator as Navigator & { userAgentData?: { platform?: string } }).userAgentData?.platform || navigator.platform;
  const isIOS = /iP(hone|ad|od)|iOS/.test(platform) || (platform === "MacIntel" && navigator.maxTouchPoints > 1);
  const bodyStyle = document.body.style;
  const scrollbarX = Math.round(document.documentElement.getBoundingClientRect().left) + document.documentElement.scrollLeft;
  const paddingProp = scrollbarX ? "paddingLeft" : "paddingRight";
  const scrollbarWidth = window.innerWidth - document.documentElement.clientWidth;
  const scrollX = bodyStyle.left ? parseFloat(bodyStyle.left) : window.scrollX;
  const scrollY = bodyStyle.top ? parseFloat(bodyStyle.top) : window.scrollY;
  bodyStyle.setProperty(SCROLLBAR_PROPERTY, `${scrollbarWidth}px`);
  if (scrollbarWidth) bodyStyle[paddingProp] = `${scrollbarWidth}px`;
  if (isIOS) {
    const offsetLeft = window.visualViewport?.offsetLeft || 0;
    const offsetTop = window.visualViewport?.offsetTop || 0;
    Object.assign(bodyStyle, {
      position: "fixed",
      top: `${-(scrollY - Math.floor(offsetTop))}px`,
      left: `${-(scrollX - Math.floor(offsetLeft))}px`,
      right: "0",
    });
  }
  return () => {
    bodyStyle[paddingProp] = "";
    bodyStyle.removeProperty(SCROLLBAR_PROPERTY);
    if (isIOS) {
      Object.assign(bodyStyle, { position: "", top: "", left: "", right: "" });
      window.scrollTo(scrollX, scrollY);
    }
  };
}

/** Set body overflow for current holders; with none, restore the pre-lock value. */
function syncOverflow(): void {
  const bodyStyle = document.body.style;
  if (plainCount + compensateCount === 0) {
    if (savedOverflow?.value) bodyStyle.setProperty("overflow", savedOverflow.value, savedOverflow.priority);
    else bodyStyle.removeProperty("overflow");
    savedOverflow = null;
    return;
  }
  bodyStyle.setProperty("overflow", "hidden", "important");
}

export function lockBodyScroll({ compensate = false }: { compensate?: boolean } = {}): () => void {
  const bodyStyle = document.body.style;
  if (plainCount + compensateCount === 0) {
    savedOverflow = { value: bodyStyle.getPropertyValue("overflow"), priority: bodyStyle.getPropertyPriority("overflow") };
  }
  if (compensate) {
    compensateCount += 1;
    // Measure the scrollbar before changing overflow (same order as original floating-ui).
    if (compensateCount === 1) releaseCompensation = applyCompensation();
  } else {
    plainCount += 1;
  }
  syncOverflow();
  let released = false;
  return () => {
    if (released) return;
    released = true;
    if (compensate) {
      compensateCount -= 1;
      if (compensateCount === 0) {
        releaseCompensation?.();
        releaseCompensation = null;
      }
    } else {
      plainCount -= 1;
    }
    syncOverflow();
  };
}
