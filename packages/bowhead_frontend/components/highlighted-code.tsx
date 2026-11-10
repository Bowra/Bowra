"use client";

import { useMemo } from "react";
import { highlightCodeToReact } from "@/lib/code-highlight";

/**
 * Syntax-highlighted contents for a `<code>` / `<pre>` element. Renders the original text unchanged when the
 * language is unknown, plain, or the input is too large, so callers keep their existing container and copy text.
 */
export function HighlightedCode({ code, lang }: { code: string; lang?: string | null }) {
  const nodes = useMemo(() => highlightCodeToReact(code, lang), [code, lang]);
  return <>{nodes ?? code}</>;
}
