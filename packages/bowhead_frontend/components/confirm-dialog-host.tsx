"use client";

import { useEffect, useState } from "react";

import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import {
  settleConfirm,
  subscribeConfirm,
  type PendingConfirm,
} from "@/lib/confirm-dialog-store";

/** Render confirmations queued by `requestConfirm()`; mount alongside ToastContainer in the authenticated shell. */
export function ConfirmDialogHost() {
  const [pending, setPending] = useState<PendingConfirm | null>(null);
  useEffect(() => subscribeConfirm(setPending), []);
  if (!pending) return null;
  return (
    <ConfirmDialog
      key={pending.id}
      title={pending.title}
      description={pending.description}
      confirmLabel={pending.confirmLabel}
      cancelLabel={pending.cancelLabel}
      tone={pending.tone ?? "danger"}
      onCancel={() => settleConfirm(pending.id, false)}
      onConfirm={() => settleConfirm(pending.id, true)}
    />
  );
}
