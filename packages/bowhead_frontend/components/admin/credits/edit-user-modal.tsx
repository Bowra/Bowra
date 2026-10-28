"use client";

/**
 * EditUserModal — admin edits user tier / quota / balance.
 *
 * Linked behavior:
 *   - Changing tier checks reset quota to the new tier's default by default
 *   - Changing quota leaves raise balance to quota unchecked by default
 * Cap:
 *   - quota: 1 .. 999_999_999
 *   - balance: 0 .. 999_999_999, may exceed quota (overdraft warning)
 *   - balance change > 100,000 → second confirmation
 */

import { useState } from "react";
import { useTranslations } from "next-intl";

import {
  patchCreditUser,
  type CreditUserRow,
  type PatchUserBody,
} from "@/lib/admin-credits-api";
import { adminCreditsErrorKey } from "@/lib/admin-credits-error";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import type { CreditCapabilities } from "@/lib/auth-api";
import { CharIcon } from "@/components/char-icon";

interface Props {
  user: CreditUserRow;
  capabilities: CreditCapabilities | null;
  onClose: () => void;
  onSaved: (updated: CreditUserRow) => void;
}

export function EditUserModal({ user, capabilities, onClose, onSaved }: Props) {
  const t = useTranslations("admin.credits");
  const canEditTier = !!capabilities?.credit_user_tier_write;
  const canEditQuota = !!capabilities?.credit_user_quota_write;

  const [tier, setTier] = useState<"pro" | "ultra">(user.tier);
  const [quota, setQuota] = useState(String(user.credit_quota));
  const [balance, setBalance] = useState(String(user.credit_balance));
  const [resetQuotaWithTier, setResetQuotaWithTier] = useState(true);
  const [pullBalanceToQuota, setPullBalanceToQuota] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [confirmLargeSave, setConfirmLargeSave] = useState(false);

  const tierChanged = canEditTier && tier !== user.tier;
  const quotaNum = parseInt(quota, 10);
  const balanceNum = parseInt(balance, 10);
  const quotaChanged = canEditQuota && !isNaN(quotaNum) && quotaNum !== user.credit_quota;
  const balanceChanged = canEditQuota && !isNaN(balanceNum) && balanceNum !== user.credit_balance;
  const hasChanges = tierChanged || quotaChanged || balanceChanged;
  const balanceOverdraft = !isNaN(balanceNum) && !isNaN(quotaNum) && balanceNum > quotaNum;
  const balanceLargeChange = balanceChanged
    && Math.abs(balanceNum - user.credit_balance) > 100_000;

  const validate = (): string | null => {
    if (quotaChanged) {
      if (isNaN(quotaNum) || quotaNum < 1 || quotaNum > 999_999_999) {
        return t("quota_invalid");
      }
    }
    if (balanceChanged) {
      if (isNaN(balanceNum) || balanceNum < 0 || balanceNum > 999_999_999) {
        return t("balance_invalid");
      }
    }
    return null;
  };

  const performSave = async () => {
    if (!hasChanges) return;
    const err = validate();
    if (err) { setError(err); return; }

    setSaving(true);
    setError(null);
    setConfirmLargeSave(false);
    try {
      const body: PatchUserBody = {};
      if (tierChanged) {
        body.tier = tier;
        body.reset_quota_with_tier = resetQuotaWithTier;
      }
      if (quotaChanged) {
        body.credit_quota = quotaNum;
        body.pull_balance_to_quota = pullBalanceToQuota;
      }
      if (balanceChanged) body.credit_balance = balanceNum;
      const out = await patchCreditUser(user.user_id, body);
      onSaved(out.user);
    } catch (e: unknown) {
      setError(t(adminCreditsErrorKey(e)));
    } finally {
      setSaving(false);
    }
  };

  const save = async () => {
    if (!hasChanges) return;
    const err = validate();
    if (err) { setError(err); return; }
    if (balanceLargeChange) {
      setConfirmLargeSave(true);
      return;
    }
    await performSave();
  };

  return (
    <div
      role="dialog"
      aria-label={t("edit_aria")}
      onClick={onClose}
      className="legacy-credits-modal-backdrop"
    >
      <div onClick={(e) => e.stopPropagation()} className="legacy-credits-modal legacy-credits-modal--edit">
        <h3 className="legacy-credits-modal-title">{t("edit_modal_title")}</h3>
        <p className="legacy-credits-modal-sub">
          {user.email}{user.display_name ? ` · ${user.display_name}` : ""}
        </p>

        <Field label={t("edit_tier_label")}>
          <select
            value={tier}
            onChange={(e) => setTier(e.target.value as "pro" | "ultra")}
            disabled={saving || !canEditTier}
            className="ui-input legacy-input-credits-modal"
          >
            <option value="pro">{t("tier_pro")}</option>
            <option value="ultra">{t("tier_ultra")}</option>
          </select>
          {tierChanged ? (
            <Checkbox
              checked={resetQuotaWithTier}
              onChange={setResetQuotaWithTier}
              label={t("edit_reset_quota_with_tier")}
              hint={t("edit_reset_quota_with_tier_hint")}
              disabled={saving}
            />
          ) : null}
        </Field>

        <Field label={t("edit_quota_label")}>
          <input
            type="number"
            min={1} max={999_999_999}
            value={quota}
            onChange={(e) => setQuota(e.target.value)}
            disabled={saving || !canEditQuota}
            className="ui-input legacy-input-credits-modal"
          />
          {quotaChanged ? (
            <Checkbox
              checked={pullBalanceToQuota}
              onChange={setPullBalanceToQuota}
              label={t("edit_pull_balance_to_quota")}
              hint={t("edit_pull_balance_to_quota_hint")}
              disabled={saving}
            />
          ) : null}
        </Field>

        <Field label={t("edit_balance_label")}>
          <input
            type="number"
            min={0} max={999_999_999}
            value={balance}
            onChange={(e) => setBalance(e.target.value)}
            disabled={saving || !canEditQuota}
            className="ui-input legacy-input-credits-modal"
          />
          {balanceOverdraft ? (
            <div className="legacy-credits-edit-overdraft">
              <CharIcon ch="⚠" /> {t("edit_balance_warning_overdraft")}
            </div>
          ) : null}
        </Field>

        {error ? (
          <div className="legacy-credits-modal-error">{error}</div>
        ) : null}

        <div className="credits-modal-foot legacy-credits-modal-foot legacy-credits-modal-foot--spaced">
          <button
            type="button" onClick={onClose} disabled={saving}
            className="credits-btn credits-btn--secondary"
          >{t("cancel_button")}</button>
          <button
            type="button" onClick={save} disabled={saving || !hasChanges}
            className="credits-btn credits-btn--primary"
          >{saving ? "..." : t("save_button")}</button>
        </div>
        {confirmLargeSave ? (
          <ConfirmDialog
            title={t("save_button")}
            description={t("edit_save_warning_large_balance")}
            confirmLabel={t("save_button")}
            cancelLabel={t("cancel_button")}
            tone="primary"
            busy={saving}
            onCancel={() => setConfirmLargeSave(false)}
            onConfirm={() => void performSave()}
          />
        ) : null}
      </div>
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="legacy-credits-edit-field">
      <label className="legacy-credits-field-label">{label}</label>
      {children}
    </div>
  );
}

function Checkbox({ checked, onChange, label, hint, disabled }: {
  checked: boolean; onChange: (v: boolean) => void;
  label: string; hint?: string; disabled?: boolean;
}) {
  return (
    <label className={`legacy-credits-edit-check${disabled ? " is-disabled" : ""}`}>
      <input
        type="checkbox"
        checked={checked}
        onChange={(e) => onChange(e.target.checked)}
        disabled={disabled} className="legacy-credits-edit-check-input"
      />
      <span>{label}</span>
      {hint ? (
        <div className="legacy-credits-edit-check-hint">
          {hint}
        </div>
      ) : null}
    </label>
  );
}

