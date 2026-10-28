export interface AppLinkClickLike {
  defaultPrevented: boolean;
  button: number;
  metaKey: boolean;
  ctrlKey: boolean;
  shiftKey: boolean;
  altKey: boolean;
}

/** Let the browser own modified/non-primary clicks so links keep native tab/window behavior. */
export function shouldHandleAppLinkClick(event: AppLinkClickLike): boolean {
  return (
    !event.defaultPrevented &&
    event.button === 0 &&
    !event.metaKey &&
    !event.ctrlKey &&
    !event.shiftKey &&
    !event.altKey
  );
}

/**
 * Same test for a link item inside a menu: the menu may already have called `preventDefault()` while
 * closing, so only the button and modifier keys decide whether the app navigates in place.
 */
export function shouldHandleMenuLinkSelect(event: Omit<AppLinkClickLike, "defaultPrevented">): boolean {
  return shouldHandleAppLinkClick({ ...event, defaultPrevented: false, button: event.button ?? 0 });
}
