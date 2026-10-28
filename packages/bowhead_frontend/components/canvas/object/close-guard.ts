"use client";

import { useCallback } from "react";
import { useTranslations } from "next-intl";

import { useConfirmDialog, type ConfirmDialogOptions } from "@/components/ui/confirm-dialog";

type Translate = (key: string, values?: Record<string, string | number>) => string;

/**
 * C14 · the one close guard copy for every Canvas object: shown only with unsaved changes;
 * “Keep editing” is the default focus, “Discard changes” is red text on a secondary button.
 * Use with `useTranslations("canvas_object")`.
 */
export function canvasCloseGuardOptions(t: Translate, name: string): ConfirmDialogOptions & {
  title: string; description: string; confirmLabel: string; cancelLabel: string; tone: "discard";
} {
  return {
    title: t("guard_title"),
    description: t("guard_body", { name }),
    confirmLabel: t("guard_discard"),
    cancelLabel: t("guard_keep"),
    tone: "discard",
  };
}

/** `confirmDiscard(name)` resolves true when the user chooses to discard; render `dialog` once. */
export function useCanvasCloseGuard() {
  const t = useTranslations("canvas_object");
  const { confirm, dialog } = useConfirmDialog();
  const confirmDiscard = useCallback((name: string) => confirm(canvasCloseGuardOptions(t, name)), [confirm, t]);
  return { confirmDiscard, dialog };
}
