"use client";

/**
 * Copies `text`; when the Clipboard API is unavailable or refused, shows it in a prompt for manual copying.
 * `promptText` replaces the text shown in that prompt (a prompt input is a single line, so multi-line text
 * should pass a single-line form); it defaults to `text`.
 */
export async function copyTextWithFallback(
  text: string,
  fallbackPrompt: string,
  promptText: string = text,
): Promise<boolean> {
  if (typeof window === "undefined") return false;
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    window.prompt(fallbackPrompt, promptText);
    return false;
  }
}
