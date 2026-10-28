"use client";

import { useCallback } from "react";
import { useTranslations } from "next-intl";

/**
 * Display helpers for memory surfaces (Settings › Memory, chat memory cards).
 *
 * The gateway sends a ready-made Chinese `label` with every scope ("仅供你使用 · 所有空间"), which showed untranslated
 * in English (116 1.5.19). The label is composed here from the structured fields instead, in the UI language.
 */
export interface MemoryScopeLike {
  type?: "global" | "space";
  space_name?: string | null;
  availability?: "available" | "unavailable";
  /** Server-composed label; used only when the structured type is missing (older payloads). */
  label?: string | null;
}

export function useMemoryScopeLabel(): (scope: MemoryScopeLike | null | undefined) => string | null {
  const t = useTranslations("memory_page.v2");
  return useCallback(
    (scope: MemoryScopeLike | null | undefined) => {
      if (!scope) return null;
      if (scope.type !== "global" && scope.type !== "space") return scope.label ?? null;
      if (scope.type === "global") return t("scope_label_global");
      const name = scope.space_name?.trim();
      if (scope.availability === "unavailable" || !name) return t("scope_label_space_unavailable");
      return t("scope_label_space", { name });
    },
    [t],
  );
}

// ASCII boundaries, as in the gateway: an id glued to CJK text ("记忆mem_x矛盾") must still be found.
const MEMORY_ID = /[ \t]*(?<![A-Za-z0-9_])mem_[0-9a-f]{6,32}(?![A-Za-z0-9_])/g;
const EMPTY_BRACKETS = /[ \t]*(\(\s*\)|（\s*）|\[\s*\])/g;

/**
 * Last line of defence for model-written reasons: never render a raw memory id (`mem_…`). The gateway already names
 * the referenced memory by its text; anything that still carries an id has the id removed.
 */
export function withoutMemoryIds(text: string | null | undefined): string | null {
  if (!text) return null;
  const cleaned = text.replace(MEMORY_ID, "").replace(EMPTY_BRACKETS, "").replace(/[ \t]{2,}/g, " ").trim();
  return cleaned || null;
}
