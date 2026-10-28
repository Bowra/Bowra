/**
 * Canvas PM Adapter — public API.
 *
 * P3.1 output, for P3.2+ use.
 *
 * Main entry points:
 *   canvasToPMDoc(sections) → JSONContent     // forward before rendering
 *   pmDocToSections(doc, orig) → SectionPatch[] // backward + diff after user edits
 *
 * Tiptap registration:
 *   import { CanvasDocument, getAllCanvasExtensions } from "@/lib/canvas-pm-adapter";
 *   useEditor({ extensions: [CanvasDocument, ...getAllCanvasExtensions()] })
 */

export { canvasToPMDoc, sectionToPMNode, pmDocHasContent, shouldRefuseSetContent } from "./canvas-to-pm";
export { pmDocToSections, pmNodeToSection, type SectionPatch } from "./pm-to-canvas";
export { markdownToPMNodes, pmNodesToMarkdown } from "./markdown-pm";
export { normalizeDocForEditor } from "./editor-doc-normalize";
export {
  CanvasDocument,
  ALL_KINDS,
  ATOMIC_KINDS,
  TEXT_BASED_KINDS,
  RECURSIVE_KINDS,
  TOTAL_KIND_COUNT,
  KIND_TO_NODE_NAME,
  NODE_NAME_TO_KIND,
  isAtomicKind,
  isTextBasedKind,
  isRecursiveKind,
  makeAtomicSectionNode,
  makeTextBasedSectionNode,
  makeRecursiveSectionNode,
  type SectionKind,
  type AtomicKind,
  type TextBasedKind,
  type RecursiveKind,
} from "./schema";

export { getAllCanvasExtensions } from "./extensions";
export {
  CanvasContextProvider,
  useCanvasContext,
  type CanvasContextValue,
  type CanvasContextProviderProps,
} from "./canvas-context";
export {
  patchCanvasSection,
  type PatchResult,
} from "./canvas-patch";
export type {
  SourceRef,
  CitationKind,
  CitationDecorationData,
} from "./citation-types";
