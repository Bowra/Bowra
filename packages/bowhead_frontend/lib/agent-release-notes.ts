/**
 * Release notes the server writes itself are stored as fixed English values (kept for compatibility); show
 * them in the reader's language. Notes an Owner typed are shown as written.
 */
const SYSTEM_RELEASE_NOTE_KEYS: Record<string, "release_note_system_initial_private" | "release_note_system_initial_sharing"> = {
  "Initial private release": "release_note_system_initial_private",
  "Initial sharing request": "release_note_system_initial_sharing",
};

export function releaseNoteText(
  note: string | null | undefined,
  t: (key: "release_no_note" | "release_note_system_initial_private" | "release_note_system_initial_sharing") => string,
): string {
  if (!note) return t("release_no_note");
  const key = SYSTEM_RELEASE_NOTE_KEYS[note];
  return key ? t(key) : note;
}
