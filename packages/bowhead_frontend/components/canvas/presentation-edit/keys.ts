import type { KeyboardEvent } from "react";

/**
 * The key belongs to an input method's composition (picking a candidate, cancelling it), not to
 * the editor. Chrome on Windows reports such keys as `Process`, with key code 229.
 */
export function isComposing(event: KeyboardEvent<Element>): boolean {
  return event.nativeEvent.isComposing || event.keyCode === 229;
}
