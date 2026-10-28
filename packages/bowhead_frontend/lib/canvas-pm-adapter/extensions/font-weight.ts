/**
 * FontWeight — custom Tiptap mark extension adding the fontWeight attr to textStyle.
 *
 * Same pattern as FontSize, for granular 400/500/600/700 weights (StarterKit Bold extension
 * only detects ≥500 → bold mark, without distinguishing medium/semibold/bold).
 *
 * Usage:
 *   editor.chain().setFontWeight("500").run()  // medium
 *   editor.chain().setFontWeight("700").run()  // bold
 */

import { Extension } from "@tiptap/core";

declare module "@tiptap/core" {
  interface Commands<ReturnType> {
    fontWeight: {
      setFontWeight: (fontWeight: string) => ReturnType;
      unsetFontWeight: () => ReturnType;
    };
  }
}

export const FontWeight = Extension.create({
  name: "fontWeight",

  addOptions() {
    return {
      types: ["textStyle"],
    };
  },

  addGlobalAttributes() {
    return [
      {
        types: this.options.types,
        attributes: {
          fontWeight: {
            default: null as string | null,
            parseHTML: (element: HTMLElement) => {
              const v = element.style.fontWeight;
              return v ? v.trim() : null;
            },
            renderHTML: (attributes: Record<string, unknown>) => {
              if (!attributes.fontWeight) return {};
              return { style: `font-weight: ${attributes.fontWeight}` };
            },
          },
        },
      },
    ];
  },

  addCommands() {
    return {
      setFontWeight:
        (fontWeight: string) =>
        ({ chain }) =>
          chain().setMark("textStyle", { fontWeight }).run(),
      unsetFontWeight:
        () =>
        ({ chain }) =>
          chain()
            .setMark("textStyle", { fontWeight: null })
            .run(),
    };
  },
});
