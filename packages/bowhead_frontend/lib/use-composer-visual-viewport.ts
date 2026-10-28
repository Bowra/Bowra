"use client";

import { useEffect, type RefObject } from "react";

const KEYBOARD_THRESHOLD_PX = 120;

export function composerVisualViewportInsets({
  layoutHeight,
  visualHeight,
  offsetTop,
}: {
  layoutHeight: number;
  visualHeight: number;
  offsetTop: number;
}): { top: number; bottom: number } {
  return {
    top: Math.max(0, Math.round(offsetTop)),
    bottom: Math.max(0, Math.round(layoutHeight - (offsetTop + visualHeight))),
  };
}

export function composerKeyboardInset({
  layoutHeight,
  visualHeight,
  offsetTop,
  scale,
  textareaFocused,
}: {
  layoutHeight: number;
  visualHeight: number;
  offsetTop: number;
  scale: number;
  textareaFocused: boolean;
}): number {
  if (!textareaFocused || scale !== 1) return 0;
  const occluded = Math.max(0, layoutHeight - (offsetTop + visualHeight));
  return occluded >= KEYBOARD_THRESHOLD_PX ? Math.round(occluded) : 0;
}

/** Keep the active Composer and its modal sheet inside the visual viewport. */
export function useComposerVisualViewport(
  composerRef: RefObject<HTMLElement | null>,
) {
  useEffect(() => {
    const visualViewport = window.visualViewport;
    if (!visualViewport) return;
    let frame = 0;
    const targets = () => {
      const root = composerRef.current;
      const homeDock = root?.closest<HTMLElement>(".home-work-composer-dock") ?? null;
      if (homeDock) return [homeDock];
      const workbenchWrap = root?.closest<HTMLElement>(".v2-composer-wrap") ?? null;
      return workbenchWrap ? [workbenchWrap] : [];
    };
    const update = () => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(() => {
        const textareaFocused = composerRef.current?.contains(document.activeElement) === true
          && document.activeElement?.tagName === "TEXTAREA";
        const inset = composerKeyboardInset({
          layoutHeight: window.innerHeight,
          visualHeight: visualViewport.height,
          offsetTop: visualViewport.offsetTop,
          scale: visualViewport.scale,
          textareaFocused,
        });
        const viewportInsets = composerVisualViewportInsets({
          layoutHeight: window.innerHeight,
          visualHeight: visualViewport.height,
          offsetTop: visualViewport.offsetTop,
        });
        for (const target of targets()) {
          target.style.setProperty("--bh-kb-inset", `${inset}px`);
        }
        document.documentElement.style.setProperty(
          "--bh-visual-viewport-top",
          `${viewportInsets.top}px`,
        );
        document.documentElement.style.setProperty(
          "--bh-visual-viewport-bottom",
          `${viewportInsets.bottom}px`,
        );
      });
    };
    const clear = () => {
      cancelAnimationFrame(frame);
      for (const target of targets()) target.style.removeProperty("--bh-kb-inset");
      document.documentElement.style.removeProperty("--bh-visual-viewport-top");
      document.documentElement.style.removeProperty("--bh-visual-viewport-bottom");
    };
    visualViewport.addEventListener("resize", update);
    visualViewport.addEventListener("scroll", update);
    window.addEventListener("focusin", update);
    window.addEventListener("focusout", update);
    window.addEventListener("orientationchange", update);
    update();
    return () => {
      visualViewport.removeEventListener("resize", update);
      visualViewport.removeEventListener("scroll", update);
      window.removeEventListener("focusin", update);
      window.removeEventListener("focusout", update);
      window.removeEventListener("orientationchange", update);
      clear();
    };
  }, [composerRef]);
}
