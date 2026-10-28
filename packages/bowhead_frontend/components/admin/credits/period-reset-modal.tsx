"use client";

/**
 * PeriodResetModal — period reset: preview + 24h cooldown + audit snapshot.
 *
 * Flow:
 *   1. On opening, immediately call periodResetPreview(mode); show affected-user count + custom-override list
 *   2. User clicks Execute Reset → call periodResetConfirm(mode)
 *   3. 429 → show cooldown countdown, disable button
 *   4. Success → close modal + onSuccess
 */

import { useEffect, useState } from "react";
import { useTranslations } from "next-intl";

import { DialogShell } from "@/components/ui/dialog";

import {
  AdminCreditsError,
  periodResetConfirm,
  periodResetPreview,
  type PeriodResetPreview,
} from "@/lib/admin-credits-api";
import { adminCreditsErrorKey } from "@/lib/admin-credits-error";
import { CharIcon } from "@/components/char-icon";

type Mode = "balance_only" | "balance_and_quota";

interface Props {
  mode: Mode;
  onClose: () => void;
  onSuccess: () => void;
  onSwitchMode: (mode: Mode) => void;
}

export function PeriodResetModal({ mode, onClose, onSuccess, onSwitchMode }: Props) {
  const t = useTranslations("admin.credits");
  const [preview, setPreview] = useState<PeriodResetPreview | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [cooldownSec, setCooldownSec] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);

  // load preview on mount + mode change
  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    setCooldownSec(null);
    periodResetPreview(mode)
      .then((data) => {
        if (!cancelled) setPreview(data.preview);
      })
      .catch((e: unknown) => {
        if (cancelled) return;
        if (e instanceof AdminCreditsError && e.status === 429) {
          setCooldownSec(e.retryAfter ?? 0);
          if (!e.retryAfter) setError(t(adminCreditsErrorKey(e)));
        } else {
          setError(t(adminCreditsErrorKey(e)));
        }
      })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [mode, t]);

  // cooldown countdown
  useEffect(() => {
    if (cooldownSec == null || cooldownSec <= 0) return;
    const timer = setInterval(() => {
      setCooldownSec((s) => (s == null || s <= 0 ? s : s - 1));
    }, 1000);
    return () => clearInterval(timer);
  }, [cooldownSec]);

  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      await periodResetConfirm(mode);
      onSuccess();
    } catch (e: unknown) {
      if (e instanceof AdminCreditsError && e.status === 429) {
        setCooldownSec(e.retryAfter ?? 0);
        if (!e.retryAfter) setError(t(adminCreditsErrorKey(e)));
      } else {
        setError(t(adminCreditsErrorKey(e)));
      }
    } finally {
      setBusy(false);
    }
  };

  const title = mode === "balance_only"
    ? t("reset_modal_title_balance")
    : t("reset_modal_title_full");

  const formatWait = (sec: number): string => {
    if (sec <= 0) return "0s";
    const h = Math.floor(sec / 3600);
    const m = Math.floor((sec % 3600) / 60);
    const s = sec % 60;
    if (h > 0) return `${h}h ${m}m`;
    if (m > 0) return `${m}m ${s}s`;
    return `${s}s`;
  };

  return (
    <DialogShell
      onClose={onClose}
      busy={busy}
      label={t("period_reset_aria")}
      backdropClassName="legacy-credits-modal-backdrop"
      panelClassName="legacy-credits-modal legacy-credits-modal--reset"
    >
      <h3 className="legacy-credits-modal-title legacy-credits-modal-title--reset">{title}</h3>

      {/* mode switcher */}
      <div className="legacy-credits-mode-tabs">
        <ModeTab
          active={mode === "balance_only"}
          onClick={() => onSwitchMode("balance_only")}
          label={t("period_reset_balance_only")}
        />
        <ModeTab
          active={mode === "balance_and_quota"}
          onClick={() => onSwitchMode("balance_and_quota")}
          label={t("period_reset_full")}
        />
      </div>

      {loading ? (
        <div className="legacy-credits-reset-skeleton" />
      ) : cooldownSec != null && cooldownSec > 0 ? (
        <div className="legacy-credits-reset-cooldown">
          ⏱ {t("reset_cooldown_active", { wait: formatWait(cooldownSec) })}
        </div>
      ) : preview ? (
        <PreviewBody preview={preview} />
      ) : null}

      {error ? (
        <div className="legacy-credits-modal-error">{error}</div>
      ) : null}

      <p className="legacy-credits-modal-hint legacy-credits-modal-hint--reset">
        <CharIcon ch="⚠" /> {t("reset_irreversible")}
      </p>

      {/* credits-modal-foot is only a footer marker, without styles: only danger buttons inside dialog footers are solid red */}
      <div className="credits-modal-foot legacy-credits-modal-foot">
        <button
          type="button" onClick={onClose} disabled={busy}
          className="credits-btn credits-btn--secondary"
        >{t("cancel_button")}</button>
        <button
          type="button" onClick={submit}
          disabled={busy || loading || (cooldownSec != null && cooldownSec > 0)}
          className="credits-btn credits-btn--danger"
        >{busy ? "..." : t("reset_confirm")}</button>
      </div>
    </DialogShell>
  );
}

function ModeTab({ active, onClick, label }: {
  active: boolean; onClick: () => void; label: string;
}) {
  return (
    <button
      type="button" onClick={onClick}
      className={active ? "legacy-credits-mode-tab is-active" : "legacy-credits-mode-tab"}
    >{label}</button>
  );
}

function PreviewBody({ preview }: { preview: PeriodResetPreview }) {
  const t = useTranslations("admin.credits");
  const customCount = preview.custom_quota_overrides.length;
  return (
    <div className="legacy-credits-reset-preview">
      <div className="legacy-credits-reset-affected">
        {t("reset_affected", { count: preview.users.length })}
      </div>
      {customCount > 0 ? (
        <div className="legacy-credits-reset-overrides">
          <CharIcon ch="⚠" /> {t("reset_custom_overrides", { n: customCount })}
          <ul className="legacy-credits-reset-overrides-list">
            {preview.custom_quota_overrides.slice(0, 5).map((o) => (
              <li key={o.user_id}>
                {o.email}: {o.current_quota.toLocaleString()} → {o.new_quota.toLocaleString()}
              </li>
            ))}
            {preview.custom_quota_overrides.length > 5 ? (
              <li className="legacy-credits-reset-overrides-more">{t("and_n_more", { n: preview.custom_quota_overrides.length })}</li>
            ) : null}
          </ul>
        </div>
      ) : null}
      <div className="legacy-credits-reset-balance">
        team_balance: {preview.team_balance_before.toLocaleString()} → {preview.team_balance_after.toLocaleString()}
      </div>
    </div>
  );
}

