"use client";

import { useLayoutEffect, useRef, useState, type RefObject } from "react";

import { fitSteps, writeFitTokens, type FitStep } from "@/lib/toolbar-fit";

export interface ToolbarFitOptions {
  /** Steps in the order they give way. */
  steps: readonly FitStep[];
  /** Steps applied before the ordered ones (for example a mode row), computed from the current DOM. */
  first?: (el: HTMLElement) => FitStep[];
  /** True when the toolbar fits with the tokens currently written. */
  fits: (el: HTMLElement) => boolean;
  /** Off: no tokens are written (layouts that are not measured, such as the phone bar). */
  enabled: boolean;
}

const EMPTY: ReadonlySet<FitStep> = new Set();

function sameTokens(a: ReadonlySet<FitStep>, b: readonly FitStep[]): boolean {
  return a.size === b.length && b.every((token) => a.has(token));
}

/**
 * Measured priority overflow (narrow desktop layout design §3.2). Recomputes from the start whenever the
 * element resizes, its text or children change, or web fonts finish loading, and writes `data-fit` itself;
 * React never renders that attribute. Returns the applied tokens for the parts that must render
 * differently (menu entries). Each run ends in the same final layout for the same input, so the
 * observers see no net change and do not loop.
 */
export function useToolbarFit(ref: RefObject<HTMLElement | null>, options: ToolbarFitOptions): ReadonlySet<FitStep> {
  const [applied, setApplied] = useState<ReadonlySet<FitStep>>(EMPTY);
  const optionsRef = useRef(options);
  optionsRef.current = options;
  const appliedRef = useRef(applied);
  appliedRef.current = applied;
  const { enabled } = options;

  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    if (!enabled) {
      writeFitTokens(el, []);
      if (appliedRef.current.size) setApplied(EMPTY);
      return;
    }
    let frame = 0;
    const run = () => {
      frame = 0;
      const { steps, first, fits } = optionsRef.current;
      const apply = (tokens: readonly FitStep[]) => writeFitTokens(el, tokens);
      const start = first ? first(el) : [];
      const tokens = fitSteps(apply, () => fits(el), steps, start);
      if (!sameTokens(appliedRef.current, tokens)) {
        const next = new Set(tokens);
        appliedRef.current = next;
        setApplied(next);
      }
    };
    const schedule = () => {
      if (!frame) frame = requestAnimationFrame(run);
    };
    run();
    const resize = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(schedule);
    resize?.observe(el);
    const mutation = typeof MutationObserver === "undefined" ? null : new MutationObserver(schedule);
    mutation?.observe(el, { childList: true, subtree: true, characterData: true });
    let alive = true;
    document.fonts?.ready.then(() => {
      if (alive) schedule();
    }).catch(() => {});
    return () => {
      alive = false;
      if (frame) cancelAnimationFrame(frame);
      resize?.disconnect();
      mutation?.disconnect();
    };
  }, [enabled, ref]);

  return applied;
}
