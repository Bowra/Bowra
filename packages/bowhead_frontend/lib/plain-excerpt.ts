/**
 * Markdown answer text as one plain line, for short quoted excerpts (alignment check rows) where markup
 * such as `**bold**`, `# headings`, list bullets or links would otherwise show as raw symbols. The text is
 * never rendered as HTML: this only drops the markup and keeps the words, so a judge's quote taken from
 * the same answer can still be found and highlighted inside it.
 */
export function plainExcerpt(text: string): string {
  return text
    .replace(/<br\s*\/?>/gi, " ")
    .replace(/```[^\n]*\n?/g, "")
    .replace(/!\[([^\]]*)\]\([^)]*\)/g, "$1")
    .replace(/\[([^\]]+)\]\([^)]*\)/g, "$1")
    .replace(/^\s{0,3}#{1,6}\s+/gm, "")
    .replace(/^\s{0,3}>\s?/gm, "")
    .replace(/^\s*(?:[-*+]|\d+[.)])\s+/gm, "")
    .replace(/^[\s:|-]*-{3,}[\s:|-]*$/gm, "")
    .replace(/(\*\*|__)(?=\S)([\s\S]*?\S)\1/g, "$2")
    .replace(/(^|[^\w*])\*(?=\S)([^*\n]*?\S)\*(?!\w)/g, "$1$2")
    .replace(/(^|[^\w_])_(?=\S)([^_\n]*?\S)_(?!\w)/g, "$1$2")
    .replace(/~~(?=\S)([\s\S]*?\S)~~/g, "$1")
    .replace(/`([^`\n]+)`/g, "$1")
    .replace(/\s+/g, " ")
    .trim();
}
