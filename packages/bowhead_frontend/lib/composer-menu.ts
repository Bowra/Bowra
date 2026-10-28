"use client";

import { useCallback, useLayoutEffect, useRef, type RefObject } from "react";

/** Maximum composer-toolbar overlay-menu height (models, planning mode), in px; shrink further when less space is available. */
export const COMPOSER_MENU_MAX_HEIGHT = 320;

/**
 * After composer overlays (toolbar menus, ＋ panel) close, the trigger may be disabled (sending, read-only)
 * or unmounted with the toolbar, dropping focus to body. Fall back in order: caller-specified element → composer
 * input (textarea or reference editor) / status row / explicitly marked fallback → composer root.
 */
export function composerFocusFallbackTarget(
  composerRoot: Element | null,
  ...preferred: Array<HTMLElement | null | undefined>
): HTMLElement | null {
  const fallback =
    composerRoot?.querySelector<HTMLElement>(
      'textarea, [contenteditable], [role="status"], [data-composer-focus-fallback]',
    ) ?? null;
  return (
    [...preferred.filter((candidate) => candidate !== composerRoot), fallback, composerRoot].find(
      (candidate): candidate is HTMLElement =>
        candidate instanceof HTMLElement &&
        candidate.isConnected &&
        !candidate.matches(":disabled, [inert], [aria-hidden='true']"),
    ) ?? null
  );
}

export function focusComposerFallback(
  composerRoot: Element | null,
  ...preferred: Array<HTMLElement | null | undefined>
): void {
  composerFocusFallbackTarget(composerRoot, ...preferred)?.focus();
}

/**
 * `returnFocusFallback` for `useMenu`. Record the composer root after every commit: when menu and toolbar
 * unmount, the ref is already cleared, but we still need to know which composer to return to.
 */
export function useComposerFocusFallback(
  anchorRef: RefObject<HTMLElement | null>,
  fallbackFocusRef?: RefObject<HTMLElement | null>,
): () => HTMLElement | null {
  const rootRef = useRef<Element | null>(null);
  useLayoutEffect(() => {
    rootRef.current = anchorRef.current?.closest("[data-composer-surface]") ?? null;
  });
  return useCallback(
    () => composerFocusFallbackTarget(rootRef.current, fallbackFocusRef?.current),
    [fallbackFocusRef],
  );
}
