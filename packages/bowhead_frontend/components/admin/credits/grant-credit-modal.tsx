"use client";

/**
 * GrantCreditModal — admin grants user credit (transferred from the team pool).
 *
 * lib/admin-credits-api automatically generates a UUID idempotency_key. Disable the button immediately on click
 * to prevent accidental double clicks. The backend deduplicates for 24h.
 */

import { useState } from "react";
import { useTranslations } from "next-intl";

import { DialogShell } from "@/components/ui/dialog";

import {
  grantCredit,
  type CreditUserRow,
} from "@/lib/admin-credits-api";
import { adminCreditsErrorKey } from "@/lib/admin-credits-error";
import { CharIcon } from "@/components/char-icon";

interface Props {
  user: CreditUserRow;
  onClose: () => void;
  onSuccess: (newBalance: number) => void;
}

export function GrantCreditModal({ user, onClose, onSuccess }: Props) {
  const t = useTranslations("admin.credits");
  const [amount, setAmount] = useState("1000");
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async () => {
    const n = parseInt(amount, 10);
    if (isNaN(n) || n < 1 || n > 1_000_000) {
      setError(t("grant_amount_invalid"));
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const out = await grantCredit(user.user_id, n, reason || undefined);
      onSuccess(out.new_balance);
    } catch (e: unknown) {
      setError(t(adminCreditsErrorKey(e)));
    } finally {
      setBusy(false);
    }
  };

  return (
    <DialogShell
      onClose={onClose}
      busy={busy}
      label={t("grant_aria")}
      backdropClassName="legacy-credits-modal-backdrop"
      panelClassName="legacy-credits-modal legacy-credits-modal--grant"
    >
      <h3 className="legacy-credits-modal-title">{t("grant_modal_title")}</h3>
      <p className="legacy-credits-modal-sub">
        {user.email} {t("grant_current_balance", { balance: user.credit_balance.toLocaleString() })}
      </p>

      <Field label={t("grant_amount_label")}>
        <input
          type="number"
          min={1} max={1_000_000}
          value={amount}
          onChange={(e) => setAmount(e.target.value)}
          disabled={busy}
          className="ui-input legacy-input-credits-modal"
          autoFocus
        />
      </Field>

      <Field label={t("grant_reason_label")}>
        <input
          type="text"
          maxLength={200}
          value={reason}
          onChange={(e) => setReason(e.target.value)}
          disabled={busy}
          placeholder={t("grant_reason_placeholder")}
          className="ui-input legacy-input-credits-modal"
        />
      </Field>

      <p className="legacy-credits-modal-hint"><CharIcon ch="ⓘ" /> {t("grant_pool_hint")}</p>

      {error ? (
        <div className="legacy-credits-modal-error legacy-credits-modal-error--grant">{error}</div>
      ) : null}

      <div className="credits-modal-foot legacy-credits-modal-foot">
        <button
          type="button" onClick={onClose} disabled={busy}
          className="credits-btn credits-btn--secondary"
        >{t("cancel_button")}</button>
        <button
          type="button" onClick={submit} disabled={busy}
          className="credits-btn credits-btn--primary"
        >{busy ? "..." : t("grant_button")}</button>
      </div>
    </DialogShell>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="legacy-credits-grant-field">
      <label className="legacy-credits-field-label">{label}</label>
      {children}
    </div>
  );
}

