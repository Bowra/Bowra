/**
 * code-languages — the tag a code box shows for a declared language (turn visibility FR5, E4).
 *
 * The backend declares the language a code Tool runs (`code_language` in tool_aliases.yaml) and sends it on the
 * writing step and on the run step's `code_input.language`. A language this table does not know gets no tag (and no
 * highlighting) rather than a guess. Language names are proper nouns, the same in every interface language.
 */
const LANGUAGE_TAGS: Record<string, string> = {
  python: "Python",
  sql: "SQL",
};

/** The tag for a declared language, or undefined when it is missing or unknown. */
export function codeLanguageTag(language: string | null | undefined): string | undefined {
  return language ? LANGUAGE_TAGS[language.toLowerCase()] : undefined;
}

/** The last `count` lines of code that is still being written, so the box follows the newest line. */
export function lastCodeLines(code: string, count: number): string {
  const lines = code.split("\n");
  return lines.length > count ? lines.slice(-count).join("\n") : code;
}

/** The newest non-empty line of code being written, trimmed, or "". */
export function newestCodeLine(code: string): string {
  const lines = code.split("\n");
  for (let i = lines.length - 1; i >= 0; i--) {
    const line = lines[i]!.trim();
    if (line) return line;
  }
  return "";
}
