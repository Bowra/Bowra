"use client";

/**
 * SettingsAnalysisDepth — team-admin knob for agent thoroughness.
 *
 * One intent control (Quick / Standard / Deep) that the backend maps to a profile of
 * internal agent budgets (peer iteration caps etc.) — admins tune intent, the
 * system owns the numbers. Team-scoped; mirrors the theme-policy team-admin
 * pattern (fetchMe gate → get → put).
 */

import { useEffect, useState } from "react";
import { useTranslations } from "next-intl";

import {
  getTeamAnalysisDepth,
  setTeamAnalysisDepth,
  type AnalysisDepth,
} from "@/lib/api";
import { fetchMe } from "@/lib/auth-api";

const OPTION_KEYS: AnalysisDepth[] = ["fast", "standard", "thorough"];

export function SettingsAnalysisDepth() {
  const t = useTranslations("settings.analysis_depth");
  const OPTIONS: { key: AnalysisDepth; label: string; desc: string }[] = OPTION_KEYS.map((key) => ({
    key,
    label: t(`${key}_label`),
    desc: t(`${key}_desc`),
  }));
  const [canManage, setCanManage] = useState(false);
  const [depth, setDepth] = useState<AnalysisDepth>("standard");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    void fetchMe()
      .then(async (me) => {
        const ok = Boolean(
          me.is_team_admin || me.team_role === "owner" || me.team_role === "team_admin",
        );
        if (cancelled) return;
        setCanManage(ok);
        if (!ok) return;
        const res = await getTeamAnalysisDepth();
        if (!cancelled && res?.value) setDepth(res.value);
      })
      .catch(() => {
        if (!cancelled) setCanManage(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  if (!canManage) return null;

  const save = async (next: AnalysisDepth) => {
    if (next === depth || busy) return;
    const prev = depth;
    setDepth(next); // optimistic
    setBusy(true);
    setError(null);
    try {
      await setTeamAnalysisDepth(next);
    } catch (err) {
      setDepth(prev);
      setError(t("save_failed"));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section style={{ marginTop: 24 }}>
      <h2
        style={{
          fontFamily: "var(--font-v2-serif)",
          fontSize: "var(--fs-lg)",
          fontWeight: 400,
          margin: "0 0 4px",
        }}
      >
        {t("title")}
      </h2>
      <p style={{ fontSize: "0.75rem", color: "var(--c-ink-2)", lineHeight: 1.6, margin: "0 0 12px" }}>
        {t("subtitle")}
      </p>
      <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
        {OPTIONS.map((opt) => {
          const active = opt.key === depth;
          return (
            <button
              key={opt.key}
              type="button"
              disabled={busy}
              onClick={() => void save(opt.key)}
              style={{
                textAlign: "left",
                cursor: busy ? "default" : "pointer",
                padding: "12px 14px",
                borderRadius: 8,
                border: `1px solid ${active ? "var(--c-accent, var(--c-ink))" : "var(--c-line)"}`,
                background: active ? "var(--c-surface-2)" : "var(--c-surface)",
                boxShadow: active ? "inset 0 0 0 1px var(--c-accent, var(--c-ink))" : "none",
                opacity: busy && !active ? 0.6 : 1,
                transition: "border-color .15s, background .15s",
              }}
            >
              <div
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: 8,
                  fontSize: "0.875rem",
                  fontWeight: active ? 600 : 500,
                  color: "var(--c-ink)",
                }}
              >
                <span
                  aria-hidden
                  style={{
                    width: 14,
                    height: 14,
                    borderRadius: "50%",
                    border: `1.5px solid ${active ? "var(--c-accent, var(--c-ink))" : "var(--c-line-2, var(--c-line))"}`,
                    background: active
                      ? "radial-gradient(circle, var(--c-accent, var(--c-ink)) 0 4px, transparent 5px)"
                      : "transparent",
                    flexShrink: 0,
                  }}
                />
                {opt.label}
                {opt.key === "standard" ? (
                  <span style={{ fontSize: "0.6875rem", color: "var(--c-ink-2)" }}>{t("default_badge")}</span>
                ) : null}
              </div>
              <div style={{ fontSize: "0.75rem", color: "var(--c-ink-2)", lineHeight: 1.5, marginTop: 4, marginLeft: 22 }}>
                {opt.desc}
              </div>
            </button>
          );
        })}
      </div>
      {error ? (
        <p style={{ fontSize: "0.75rem", color: "var(--c-err)", margin: "8px 0 0" }}>{error}</p>
      ) : null}
    </section>
  );
}
