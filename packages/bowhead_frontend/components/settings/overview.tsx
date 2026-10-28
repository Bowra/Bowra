"use client";

import { useTranslations } from "next-intl";
import { useSkills } from "@/lib/use-skills";

export function SettingsOverview() {
  const t = useTranslations("settings");
  const tov = useTranslations("settings.overview");
  // B4: share the hook with /profile to prevent inconsistent counts (Profile 0/0 vs Settings 25/25 bug).
  const { counts: skillCounts } = useSkills();
  // B4: from the shared useSkills hook (same source as /profile).
  const { enabled: enabledSkills, total: totalSkills } = skillCounts;

  return (
    <div>
      <div style={{
        display: "flex", justifyContent: "space-between", alignItems: "flex-start",
        gap: 16, marginBottom: 8,
      }}>
        <h1 style={{
          fontFamily: "var(--font-v2-serif)", fontSize: "1.75rem",
          fontWeight: 400, margin: 0,
        }}>
          {t("title")}
        </h1>
      </div>
      <p style={{ color: "var(--c-ink-2)", fontSize: "0.8125rem", marginBottom: 28 }}>
        {t("subtitle")}
      </p>

      <div style={{
        // U7: auto-fit so the 3 KPI cards sit in one row on desktop but stack
        // to a single column on narrow / mobile widths (412px) instead of cramming.
        display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(200px, 1fr))", gap: 14,
      }}>
        <Card label={tov("skills")} value={`${enabledSkills} / ${totalSkills}`} sub={tov("skills_sub")} />
      </div>
    </div>
  );
}

function Card({ label, value, sub }: { label: string; value: string; sub: string }) {
  return (
    <div style={{
      padding: 16,
      border: "1px solid var(--c-line)",
      borderRadius: 12,
      background: "var(--c-surface)",
    }}>
      <div style={{
        fontSize: "0.625rem", color: "var(--c-ink-3)",
        textTransform: "uppercase", letterSpacing: "0.08em",
        fontFamily: "var(--font-v2-mono)", marginBottom: 8,
      }}>
        {label}
      </div>
      <div style={{
        fontFamily: "var(--font-v2-serif)", fontSize: "1.375rem", marginBottom: 2,
      }}>
        {value}
      </div>
      <div style={{ fontSize: "0.625rem", color: "var(--c-ink-3)" }}>{sub}</div>
    </div>
  );
}
