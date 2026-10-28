"use client";

import { type RefObject, useId } from "react";

import { DialogShell } from "@/components/ui/dialog";

interface MemoryActionConfirmDialogProps {
  busy: boolean;
  cancelLabel: string;
  confirmLabel: string;
  description: string;
  eyebrow: string;
  onCancel: () => void;
  onConfirm: () => void;
  returnFocusRef?: RefObject<HTMLElement | null>;
  title: string;
  tone?: "default" | "danger";
}

export function MemoryActionConfirmDialog({
  busy,
  cancelLabel,
  confirmLabel,
  description,
  eyebrow,
  onCancel,
  onConfirm,
  returnFocusRef,
  title,
  tone = "default",
}: MemoryActionConfirmDialogProps) {
  const titleId = useId();
  const descriptionId = useId();

  return (
    <DialogShell
      mount="inline"
      role="alertdialog"
      busy={busy}
      closeOnBackdrop={false}
      returnFocusRef={returnFocusRef}
      onClose={onCancel}
      labelledBy={titleId}
      panelProps={{ "aria-describedby": descriptionId }}
      backdropClassName="memory-modal-overlay"
      panelClassName={[
        "memory-modal-box",
        "memory-v2-edit-dialog",
        tone === "danger" ? "memory-v2-danger-dialog" : "",
      ].filter(Boolean).join(" ")}
    >
      <div>
        <span className="memory-v2-eyebrow">{eyebrow}</span>
        <h2 id={titleId}>{title}</h2>
        <p id={descriptionId}>{description}</p>
      </div>
      <div className="memory-modal-actions">
        <button type="button" onClick={onCancel} disabled={busy}>
          {cancelLabel}
        </button>
        <button
          type="button"
          className={tone === "danger" ? "danger" : "primary"}
          onClick={onConfirm}
          disabled={busy}
        >
          {confirmLabel}
        </button>
      </div>
    </DialogShell>
  );
}
