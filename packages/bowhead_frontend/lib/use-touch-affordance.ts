/**
 * useTouchAffordance — report-edit touch-mode criteria (mobile-tech-design §1.1).
 *
 * Screen width is the wrong touch criterion: tablets at 641–1023 also lack hover, outside the mobile breakpoint,
 * so after r3 correctly wrapped hover decorations in `@media (hover: hover)`, tablets could
 * edit but had no visible entry points. Ask directly whether the device supports hovering.
 *
 * `(hover: none)` = primary input cannot hover (touch). `(pointer: coarse)` is a parallel
 * fallback: some environments lack hover media features but honestly report pointer precision. Either match enables touch mode:
 * persistent ⋯ entry + persistent editable affordance, instead of waiting for hover that never occurs.
 *
 * SSR/no matchMedia → false (desktop mode), matching existing useMediaQuery conventions.
 */

"use client";

import { useMediaQuery } from "./use-media-query";

export function useTouchAffordance(): boolean {
  const noHover = useMediaQuery("(hover: none)");
  const coarsePointer = useMediaQuery("(pointer: coarse)");
  return noHover || coarsePointer;
}
