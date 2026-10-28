/**
 * Collection of 23 PM Extensions for Tiptap registration.
 *
 * Usage:
 *   import { CanvasDocument } from "@/lib/canvas-pm-adapter";
 *   import { getAllCanvasExtensions } from "@/lib/canvas-pm-adapter/extensions";
 *
 *   useEditor({
 *     extensions: [CanvasDocument, ...getAllCanvasExtensions()],
 *   })
 */

import { ALL_ATOMIC_EXTENSIONS } from "./atomic";
import { ALL_TEXT_BASED_EXTENSIONS } from "./text-based";
import { ALL_RECURSIVE_EXTENSIONS } from "./recursive";
// report-engine Phase-1: inline figure reference node (figure_ref).
import { FigureRefExtension } from "./figure-ref";

export * from "./atomic";
export * from "./text-based";
export * from "./recursive";
export { FigureRefExtension } from "./figure-ref";

/** Return all section extensions (19 atomic + 3 text-based + 1 recursive + figure_ref). */
export function getAllCanvasExtensions() {
  return [
    ...ALL_ATOMIC_EXTENSIONS,
    ...ALL_TEXT_BASED_EXTENSIONS,
    ...ALL_RECURSIVE_EXTENSIONS,
    FigureRefExtension,
  ];
}
