/**
 * FontSize — custom Tiptap mark extension adding the fontSize attr to textStyle.
 *
 * Follow @tiptap/extension-font-family:
 *   - not a separate mark; add an attribute to the textStyle mark
 *   - parseHTML extracts font-size from inline styles
 *   - renderHTML emits style="font-size: ..."
 *
 * Usage (report-view useEditor):
 *   import { FontSize } from "@/lib/canvas-pm-adapter/extensions/font-size";
 *   extensions: [TextStyle, FontFamily, FontSize, ...]
 *
 * Commands (for the toolbar):
 *   editor.chain().setFontSize("18px").run()
 *   editor.chain().unsetFontSize().run()
 */

import { Extension } from "@tiptap/core";

declare module "@tiptap/core" {
  interface Commands<ReturnType> {
    fontSize: {
      setFontSize: (fontSize: string) => ReturnType;
      unsetFontSize: () => ReturnType;
    };
  }
}

export const FontSize = Extension.create({
  name: "fontSize",

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
          fontSize: {
            default: null as string | null,
            parseHTML: (element: HTMLElement) => {
              const v = element.style.fontSize;
              return v ? v.replace(/['"]/g, "").trim() : null;
            },
            renderHTML: (attributes: Record<string, unknown>) => {
              if (!attributes.fontSize) return {};
              return { style: `font-size: ${attributes.fontSize}` };
            },
          },
        },
      },
    ];
  },

  addCommands() {
    return {
      setFontSize:
        (fontSize: string) =>
        ({ chain }) =>
          chain().setMark("textStyle", { fontSize }).run(),
      unsetFontSize:
        () =>
        ({ chain }) =>
          chain()
            .setMark("textStyle", { fontSize: null })
            .run(),
    };
  },
});
