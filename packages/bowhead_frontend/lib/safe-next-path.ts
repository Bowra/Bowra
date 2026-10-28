const CONTROL_CHARS_RE = /[\u0000-\u001f]/;

export function safeInternalPath(value: string | null | undefined): string | null {
  if (!value) return null;
  const path = value.trim();
  if (path.length > 2048) return null;
  if (!path.startsWith("/") || path.startsWith("//")) return null;
  if (path.includes("\\") || CONTROL_CHARS_RE.test(path)) return null;
  return path;
}
