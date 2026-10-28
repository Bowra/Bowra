"use client";

import { useCallback, useEffect, useId, useRef, useState, type ReactNode, type RefObject } from "react";
import { useTranslations } from "next-intl";

import { Button } from "@/components/ui/button";
import { Dialog, type DialogPresentationOption } from "@/components/ui/dialog";

/** In-app confirmation (replaces browser-native confirm). Initially focus Cancel,
 *  so accidental Enter does not execute a dangerous action; `children` extend the area between description and buttons (e.g. extra switches). */
export function ConfirmDialog({
  title,
  description,
  confirmLabel,
  cancelLabel,
  tone = "danger",
  busy = false,
  onCancel,
  onConfirm,
  children,
  returnFocusRef,
  presentation,
}: {
  title: ReactNode;
  description?: ReactNode;
  confirmLabel: ReactNode;
  cancelLabel?: ReactNode;
  /** `discard`: Canvas close guard (C14) — the destructive action is a red-text secondary button. */
  tone?: "danger" | "primary" | "discard";
  busy?: boolean;
  onCancel: () => void;
  onConfirm: () => void;
  children?: ReactNode;
  /** Return focus here after close (e.g. to the menu button when the menu that opened this has already closed). */
  returnFocusRef?: RefObject<HTMLElement | null>;
  /** Narrow viewports: `sheet` turns the confirmation into a bottom action sheet (Agent CI Spec part 2, section 6). */
  presentation?: DialogPresentationOption;
}) {
  const tCommon = useTranslations("common");
  const descriptionId = useId();
  return (
    <Dialog
      title={title}
      describedBy={description ? descriptionId : undefined}
      onClose={onCancel}
      busy={busy}
      layer="top"
      returnFocusRef={returnFocusRef}
      presentation={presentation}
      footer={(
        <>
          <Button onClick={onCancel} disabled={busy} data-dialog-initial-focus="">
            {cancelLabel ?? tCommon("cancel")}
          </Button>
          <Button
            variant={tone === "danger" ? "danger" : tone === "discard" ? "secondary" : "primary"}
            className={tone === "discard" ? "settings-button--destructive-text" : undefined}
            onClick={onConfirm}
            disabled={busy}
          >
            {confirmLabel}
          </Button>
        </>
      )}
    >
      {description ? <div id={descriptionId} className="settings-dialog__description">{description}</div> : null}
      {children}
    </Dialog>
  );
}

export interface ConfirmDialogOptions {
  title: ReactNode;
  description?: ReactNode;
  confirmLabel: ReactNode;
  cancelLabel?: ReactNode;
  tone?: "danger" | "primary" | "discard";
  returnFocusRef?: RefObject<HTMLElement | null>;
  presentation?: DialogPresentationOption;
}

/**
 * Imperative confirmation (replaces browser-native confirm): `if (!(await confirm({...}))) return;`,
 * and render the returned `dialog` in the component. Calling again cancels the previous pending request;
 * unmounting the component likewise cancels a pending request.
 */
export function useConfirmDialog(): {
  confirm: (options: ConfirmDialogOptions) => Promise<boolean>;
  dialog: ReactNode;
} {
  const [pending, setPending] = useState<ConfirmDialogOptions | null>(null);
  const resolveRef = useRef<((ok: boolean) => void) | null>(null);

  const settle = useCallback((ok: boolean) => {
    const resolve = resolveRef.current;
    resolveRef.current = null;
    setPending(null);
    resolve?.(ok);
  }, []);

  const confirm = useCallback((options: ConfirmDialogOptions) => {
    resolveRef.current?.(false);
    return new Promise<boolean>((resolve) => {
      resolveRef.current = resolve;
      setPending(options);
    });
  }, []);

  useEffect(() => () => {
    resolveRef.current?.(false);
    resolveRef.current = null;
  }, []);

  const dialog = pending ? (
    <ConfirmDialog
      title={pending.title}
      description={pending.description}
      confirmLabel={pending.confirmLabel}
      cancelLabel={pending.cancelLabel}
      tone={pending.tone ?? "danger"}
      returnFocusRef={pending.returnFocusRef}
      presentation={pending.presentation}
      onCancel={() => settle(false)}
      onConfirm={() => settle(true)}
    />
  ) : null;

  return { confirm, dialog };
}
