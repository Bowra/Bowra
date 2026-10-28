"use client";

import { useState } from "react";
import { useTranslations } from "next-intl";

import { DialogShell } from "@/components/ui/dialog";

import { updateTeamCreditQuota } from "@/lib/admin-credits-api";
import { adminCreditsErrorKey } from "@/lib/admin-credits-error";

interface Props {
  currentQuota: number;
  onClose: () => void;
  onSuccess: () => void;
}

export function TeamQuotaModal({ currentQuota, onClose, onSuccess }: Props) {
  const t = useTranslations("admin.credits");
  const [newQuota, setNewQuota] = useState(currentQuota);
  const [reason, setReason] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const canSubmit = reason.trim().length >= 10 && newQuota >= 0 && !submitting;

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!canSubmit) return;
    setSubmitting(true);
    setError(null);
    try {
      await updateTeamCreditQuota({ new_quota: newQuota, reason: reason.trim() });
      onSuccess();
    } catch (cause) {
      setError(t(adminCreditsErrorKey(cause)));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <DialogShell
      onClose={onClose}
      busy={submitting}
      labelledBy="team-quota-modal-title"
      backdropClassName="legacy-credits-quota-backdrop"
      panelClassName="legacy-credits-quota-modal"
    >
      <form onSubmit={submit}>
        <h2 id="team-quota-modal-title" className="legacy-credits-quota-title">
          {t("team_quota_title")}
        </h2>
        <Field label={t("team_quota_current")}>
          <input type="text" value={currentQuota.toLocaleString()} disabled className="ui-input legacy-input-team-quota" />
        </Field>
        <Field label={t("team_quota_new")}>
          <input
            type="number"
            min="0"
            step="1000"
            value={newQuota}
            onChange={(event) => setNewQuota(Number.parseInt(event.target.value, 10) || 0)}
            required
            className="ui-input legacy-input-team-quota"
          />
        </Field>
        <Field label={t("team_quota_reason")}>
          <textarea
            rows={3}
            maxLength={500}
            value={reason}
            onChange={(event) => setReason(event.target.value)}
            placeholder={t("team_quota_reason_placeholder")}
            required
            className="ui-input legacy-input-team-quota legacy-credits-quota-textarea"
          />
          <span className="legacy-credits-quota-count">
            {reason.length < 10 ? t("team_quota_reason_remaining", { n: 10 - reason.length }) : `${reason.length} / 500`}
          </span>
        </Field>
        {error ? <p role="alert" className="legacy-credits-quota-error">{error}</p> : null}
        <div className="credits-modal-foot legacy-credits-modal-foot">
          <button type="button" onClick={onClose} className="legacy-credits-quota-btn">{t("cancel_button")}</button>
          <button type="submit" disabled={!canSubmit} className="legacy-credits-quota-btn legacy-credits-quota-btn--primary">
            {submitting ? t("team_quota_submitting") : t("team_quota_confirm", { from: currentQuota.toLocaleString(), to: newQuota.toLocaleString() })}
          </button>
        </div>
      </form>
    </DialogShell>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return <label className="legacy-credits-quota-field">{label}{children}</label>;
}

