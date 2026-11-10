/**
 * Remove internal canvas, section, revision and artifact IDs from trace text.
 *
 * Mirrors `analysis_payload.strip_internal_object_ids(..., artifacts=True)`. New step labels
 * arrive without these IDs; this covers labels recorded by earlier versions and the full
 * Agent task text, which the trace shows when a peer has no dispatch row (116 dogfood of
 * 1.7.21: "Read Chart · sec_4a5c8d7d2b6e", "…（section id: sec_4a5c8d7d2b6e）…").
 * A bracket holding only an ID (and its label) goes with it; the gap closes without a space
 * next to CJK text or punctuation and keeps one space between words.
 */
const ID = String.raw`(?:sec|cv|rv|art)_[0-9a-f]{8,}(?![A-Za-z0-9_])`;
const LABEL = String.raw`(?:(?:section|canvas|revision|artifact)(?:[ _]?id)?[ \t]*[=:：][ \t]*|(?:section|canvas|artifact)[ \t]+)?`;
const BRACKETED = new RegExp(String.raw`[ \t]*[(（\[【][ \t]*${LABEL}${ID}[ \t]*[)）\]】]`, "gi");
const BARE = new RegExp(String.raw`[ \t]*(?<![A-Za-z0-9_:/=.\-\[])${LABEL}${ID}`, "gi");
// Earlier dispatch labels were cut at the label's colon, leaving "…（section id" with no ID.
const CUT_LABEL = /[ \t]*[(（[【][ \t]*(?:section|canvas|revision|artifact)(?:[ _]?id)?[ \t]*[=:：]?[ \t]*$/i;
const REMOVED = "\u0000";
const REMOVED_RUN = /[ \t]*\u0000[\u0000 \t]*/g;

function joinsWithoutSpace(char: string): boolean {
  if (!char || char === "\r" || char === "\n" || /\p{P}/u.test(char)) return true;
  const code = char.charCodeAt(0);
  return (code >= 0x2e80 && code <= 0x9fff) || (code >= 0xf900 && code <= 0xfaff) || (code >= 0xff00 && code <= 0xffef);
}

export function stripInternalObjectIds(text: string): string {
  if (!text || text.includes(REMOVED)) return text;
  const marked = text.replace(BRACKETED, REMOVED).replace(BARE, REMOVED).replace(CUT_LABEL, REMOVED);
  if (!marked.includes(REMOVED)) return text;
  return marked.replace(REMOVED_RUN, (run: string, offset: number) => {
    const before = offset > 0 ? marked[offset - 1]! : "";
    const after = marked[offset + run.length] ?? "";
    return joinsWithoutSpace(before) || joinsWithoutSpace(after) ? "" : " ";
  });
}
