"use client";

import { useId, useRef, useState, type ReactNode } from "react";
import { useTranslations } from "next-intl";

import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { Field } from "@/components/ui/field";

/**
 * Single-line text-input dialog (replaces the native browser prompt); confirmation is disabled if
 * trimming leaves an empty value. The field takes initial focus with its text selected, so typing
 * replaces it; Enter confirms (not while an input method is composing) and Escape cancels.
 */
export function PromptDialog({
  title,
  label,
  initialValue = "",
  confirmLabel,
  cancelLabel,
  busy = false,
  maxLength,
  onCancel,
  onConfirm,
}: {
  title: ReactNode;
  label: ReactNode;
  initialValue?: string;
  confirmLabel: ReactNode;
  cancelLabel?: ReactNode;
  busy?: boolean;
  /** Character limit enforced by the field itself. */
  maxLength?: number;
  onCancel: () => void;
  onConfirm: (value: string) => void;
}) {
  const tCommon = useTranslations("common");
  const fieldId = useId();
  const [value, setValue] = useState(initialValue);
  const selectedOnceRef = useRef(false);
  const trimmed = value.trim();
  const canConfirm = !busy && trimmed.length > 0;
  return (
    <Dialog
      title={title}
      onClose={onCancel}
      busy={busy}
      footer={(
        <>
          <Button onClick={onCancel} disabled={busy}>
            {cancelLabel ?? tCommon("cancel")}
          </Button>
          <Button
            variant="primary"
            onClick={() => onConfirm(trimmed)}
            disabled={!canConfirm}
          >
            {confirmLabel}
          </Button>
        </>
      )}
    >
      <Field id={fieldId} label={label}>
        <input
          id={fieldId}
          name="settings_prompt_value"
          value={value}
          disabled={busy}
          maxLength={maxLength}
          data-dialog-initial-focus=""
          onFocus={(event) => {
            if (selectedOnceRef.current) return;
            selectedOnceRef.current = true;
            event.currentTarget.select();
          }}
          onChange={(event) => setValue(event.target.value)}
          onKeyDown={(event) => {
            // keyCode 229: Safari ends an IME composition with an Enter whose isComposing is already false.
            if (event.key !== "Enter" || event.nativeEvent.isComposing || event.keyCode === 229) return;
            event.preventDefault();
            if (canConfirm) onConfirm(trimmed);
          }}
          className="settings-prompt-input"
        />
      </Field>
    </Dialog>
  );
}
