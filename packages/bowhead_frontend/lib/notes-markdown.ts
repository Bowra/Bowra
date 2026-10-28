/**
 * V3.5 PRD-C · Notes markdown parser (lightweight).
 *
 * Recognizes (must stay in sync with backend memory_notes.parse_notes_lines):
 *   📌 ...      → pinned bullet
 *   • ...  or - ...   → regular bullet
 *   anything else (incl. blank lines)         → free text / blank
 */

export type NotesLineKind = "pinned" | "bullet" | "freetext" | "blank";

export interface NotesLine {
  kind: NotesLineKind;
  lineIndex: number;     // index in original split("\n") output
  bulletIndex: number | null;  // 0-based among pinned+bullet (null for freetext/blank)
  text: string;          // stripped text (without bullet prefix)
}

export function parseNotes(content: string): NotesLine[] {
  if (!content) return [];
  const lines = content.split("\n");
  const out: NotesLine[] = [];
  let bulletCount = 0;
  for (let i = 0; i < lines.length; i++) {
    const raw = lines[i];
    const trimmed = raw.trim();
    if (!trimmed) {
      out.push({ kind: "blank", lineIndex: i, bulletIndex: null, text: "" });
      continue;
    }
    if (trimmed.startsWith("📌")) {
      out.push({
        kind: "pinned",
        lineIndex: i,
        bulletIndex: bulletCount++,
        text: trimmed.slice(2).trim(),
      });
    } else if (trimmed.startsWith("• ") || trimmed.startsWith("- ")) {
      out.push({
        kind: "bullet",
        lineIndex: i,
        bulletIndex: bulletCount++,
        text: trimmed.slice(2).trim(),
      });
    } else {
      out.push({
        kind: "freetext",
        lineIndex: i,
        bulletIndex: null,
        text: trimmed,
      });
    }
  }
  return out;
}

export function countChars(content: string): number {
  // Match Python len() = code points. JS .length counts UTF-16 code units;
  // [...content].length counts code points (handling surrogate pairs).
  return [...(content || "")].length;
}

export function countPinned(content: string): number {
  return parseNotes(content).filter((l) => l.kind === "pinned").length;
}

export function countBullets(content: string): number {
  return parseNotes(content).filter(
    (l) => l.kind === "bullet" || l.kind === "pinned",
  ).length;
}

export function countFreeTextLines(content: string): number {
  return parseNotes(content).filter((l) => l.kind === "freetext").length;
}
