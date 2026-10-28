"use client";

/**
 * TierDefaults — admin credit-tier default configuration.
 *
 * Show: default quota for PRO / ULTRA.
 * Edit: small modal (number input) → POST /api/v1/admin/team/tier-credits.
 * Copy: changes here do **not** automatically apply to existing users.
 */

import { useCallback, useEffect, useState } from "react";
import { useTranslations } from "next-intl";

import { DialogShell } from "@/components/ui/dialog";

import { fetchTeamCredits, updateTierCredits } from "@/lib/admin-credits-api";
import { adminCreditsErrorKey } from "@/lib/admin-credits-error";
import { CharIcon } from "@/components/char-icon";

interface TierCredits {
  pro: number;
  ultra: number;
}

interface Props {
  teamId: string | null;
  onChange: () => void;
  refreshTick: number;
}

export function TierDefaults({ teamId, onChange, refreshTick }: Props) {
  const t = useTranslations("admin.credits");
  const userFacingError = useCallback(
    (cause: unknown) => t(adminCreditsErrorKey(cause)),
    [t],
  );
  const [tierCredits, setTierCredits] = useState<TierCredits | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [editing, setEditing] = useState<"pro" | "ultra" | null>(null);

  useEffect(() => {
    if (!teamId) return;
    let cancelled = false;
    setLoading(true);
    setError(null);
    fetchTeamCredits()
      .then((data) => {
        if (!cancelled) {
          const tc = data.tier_credits || {};
          setTierCredits({
            pro: Number(tc.pro ?? 5000),
            ultra: Number(tc.ultra ?? 30000),
          });
        }
      })
      .catch((e: unknown) => {
        if (!cancelled) setError(userFacingError(e));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => { cancelled = true; };
  }, [refreshTick, teamId, userFacingError]);

  if (loading) {
    return (
      <section className="legacy-credits-card">
        <SectionHeader>{t("tier_defaults")}</SectionHeader>
        <div className="legacy-credits-tier-skeleton legacy-credits-tier-skeleton--first" />
        <div className="legacy-credits-tier-skeleton" />
      </section>
    );
  }

  if (error || !tierCredits) {
    return (
      <section className="legacy-credits-card">
        <SectionHeader>{t("tier_defaults")}</SectionHeader>
        <div
          role="alert"
          className="legacy-credits-summary-error"
        >
          {error || t("err_generic")}
        </div>
      </section>
    );
  }

  return (
    <section className="legacy-credits-card credits-tiers">
      <SectionHeader>{t("tier_defaults")}</SectionHeader>
      <TierRow
        label={t("tier_pro")}
        value={tierCredits.pro}
        onEdit={() => setEditing("pro")}
        editLabel={t("edit_button")}
      />
      <TierRow
        label={t("tier_ultra")}
        value={tierCredits.ultra}
        onEdit={() => setEditing("ultra")}
        editLabel={t("edit_button")}
      />
      <p className="legacy-credits-tier-hint">
        <CharIcon ch="ⓘ" /> {t("tier_defaults_hint")}
      </p>
      {editing && teamId && (
        <EditTierModal
          tier={editing}
          currentValue={editing === "pro" ? tierCredits.pro : tierCredits.ultra}
          allValues={tierCredits}
          onClose={() => setEditing(null)}
          onSaved={() => { setEditing(null); onChange(); }}
          userFacingError={userFacingError}
        />
      )}
    </section>
  );
}

// Original inline styles moved into legacy-appearance classes (styles/ui-controls-legacy.css, Page-by-page Acceptance B)
function SectionHeader({ children }: { children: React.ReactNode }) {
  return (
    <h2 className="legacy-credits-section-title">{children}</h2>
  );
}

function TierRow({ label, value, onEdit, editLabel }: {
  label: string; value: number; onEdit: () => void; editLabel: string;
}) {
  return (
    <div className="legacy-credits-tier-row">
      <div className="legacy-credits-tier-main">
        <span className="legacy-credits-tier-label">{label}</span>
        <span className="legacy-credits-tier-value">
          {value.toLocaleString()} <CharIcon ch="⚡" />
        </span>
      </div>
      <button
        type="button"
        onClick={onEdit}
        className="legacy-credits-tier-edit"
      >{editLabel}</button>
    </div>
  );
}

function EditTierModal({ tier, currentValue, allValues, onClose, onSaved, userFacingError }: {
  tier: "pro" | "ultra";
  currentValue: number;
  allValues: TierCredits;
  onClose: () => void;
  onSaved: () => void;
  userFacingError: (cause: unknown) => string;
}) {
  const t = useTranslations("admin.credits");
  const [value, setValue] = useState(String(currentValue));
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const save = async () => {
    const n = parseInt(value, 10);
    if (isNaN(n) || n < 1 || n > 999_999_999) {
      setErr(t("invalid_number"));
      return;
    }
    setSaving(true);
    setErr(null);
    try {
      const updated = { ...allValues, [tier]: n };
      await updateTierCredits(updated);
      onSaved();
    } catch (e: unknown) {
      setErr(userFacingError(e));
    } finally {
      setSaving(false);
    }
  };

  return (
    <DialogShell
      onClose={onClose}
      busy={saving}
      label={t("edit_default_aria", { tier })}
      backdropClassName="legacy-credits-modal-backdrop"
      panelClassName="legacy-credits-modal legacy-credits-modal--tier"
    >
      <h3 className="legacy-credits-tier-title">
        {tier.toUpperCase()} {t("edit_quota_label")}
      </h3>
      <input
        type="number"
        min={1} max={999_999_999}
        value={value}
        onChange={(e) => setValue(e.target.value)}
        disabled={saving}
        className="ui-input legacy-input-tier-default"
      />
      {err ? (
        <div className="legacy-credits-tier-error">
          {err}
        </div>
      ) : null}
      <div className="credits-modal-foot legacy-credits-modal-foot legacy-credits-modal-foot--spaced">
        <button
          type="button" onClick={onClose} disabled={saving}
          className="credits-btn credits-btn--secondary"
        >{t("cancel_button")}</button>
        <button
          type="button" onClick={save} disabled={saving}
          className="credits-btn credits-btn--primary"
        >{saving ? "..." : t("save_button")}</button>
      </div>
    </DialogShell>
  );
}

