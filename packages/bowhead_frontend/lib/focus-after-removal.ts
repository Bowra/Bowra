/**
 * After confirmed deletion / successful undo, the row and its focused button disappear from the list, dropping focus to body
 * and losing keyboard/screen-reader users' position. Briefly after this call, whenever focus falls to body, move it to
 * `target()` (a stable list heading, primary page action, etc.). Stop on pointer-down or timeout; leave focus on other elements untouched.
 *
 * Independent of list-refresh timing: closing confirmation first returns focus to the row button; refreshing and removing the row later drops it to body,
 * with DOM changes triggering checks at both moments.
 */
let activeStop: (() => void) | null = null;

export function keepFocusAfterRemoval(
  target: () => HTMLElement | null | undefined,
  timeoutMs = 4000,
): () => void {
  if (typeof document === "undefined") return () => {};
  // Keep only the latest call active: when deleting two rows successively, the earlier call must not reclaim focus for its target.
  activeStop?.();
  let stopped = false;
  const lost = () => {
    const active = document.activeElement;
    return !active || active === document.body || !active.isConnected;
  };
  const check = () => {
    if (stopped || !lost()) return;
    const el = target();
    if (!el || !el.isConnected) return;
    // Do not stop after one transfer: confirmation closure returns focus to the row button; removing the row on refresh drops focus again.
    el.focus({ preventScroll: false });
  };
  const observer = new MutationObserver(check);
  const timer = window.setTimeout(() => stop(), timeoutMs);
  const onPointerDown = () => stop();
  function stop() {
    if (stopped) return;
    stopped = true;
    if (activeStop === stop) activeStop = null;
    observer.disconnect();
    window.clearTimeout(timer);
    document.removeEventListener("pointerdown", onPointerDown, true);
  }
  observer.observe(document.body, { childList: true, subtree: true });
  document.addEventListener("pointerdown", onPointerDown, true);
  activeStop = stop;
  queueMicrotask(check);
  return stop;
}
