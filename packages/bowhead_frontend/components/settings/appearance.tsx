"use client";

import { Building, Check, Lock } from "@/components/icons";
import { PersonalAppearanceSettings } from "@/components/settings/personal-appearance-settings";
import { Button } from "@/components/ui/button";
import type { ColorProfileId } from "@/lib/theme-config";

const ACCENT_OPTIONS: Array<{
  value: ColorProfileId;
  labelKey: string;
  swatch: string;
}> = [
  {
    value: "bowhead",
    labelKey: "default",
    swatch: "linear-gradient(135deg, #b04a2a, #62B89C)",
  },
  { value: "ocean", labelKey: "ocean", swatch: "#2f6f84" },
  { value: "forest", labelKey: "forest", swatch: "#4b6f44" },
  { value: "plum", labelKey: "plum", swatch: "#7a527c" },
  { value: "graphite", labelKey: "graphite", swatch: "#4f5b5c" },
];

export type TeamThemePolicy = {
  version: 1;
  defaults?: {
    colorProfile?: { type: "preset"; id: ColorProfileId };
  };
  locks?: {
    colorProfile?: boolean;
  };
  brand?: {
    name?: string;
    colorProfileId?: ColorProfileId;
  };
};

export const EMPTY_TEAM_POLICY: TeamThemePolicy = { version: 1 };

export function parseTeamThemePolicy(
  raw: string | null | undefined,
): TeamThemePolicy {
  if (!raw) return EMPTY_TEAM_POLICY;
  try {
    const parsed = JSON.parse(raw) as Partial<TeamThemePolicy>;
    if (parsed?.version !== 1) return EMPTY_TEAM_POLICY;
    const colorProfile = parsed.defaults?.colorProfile;
    const locks = parsed.locks?.colorProfile
      ? { colorProfile: true }
      : undefined;
    if (
      !colorProfile ||
      colorProfile.type !== "preset" ||
      !ACCENT_OPTIONS.some((item) => item.value === colorProfile.id)
    ) {
      return locks ? { version: 1, locks } : EMPTY_TEAM_POLICY;
    }
    return {
      version: 1,
      defaults: { colorProfile },
      ...(locks ? { locks } : {}),
      ...(parsed.brand?.name
        ? {
            brand: {
              name: parsed.brand.name,
              colorProfileId: colorProfile.id,
            },
          }
        : {}),
    };
  } catch {
    return EMPTY_TEAM_POLICY;
  }
}

export function serializeTeamThemePolicy(policy: TeamThemePolicy): string {
  const colorProfile = policy.defaults?.colorProfile;
  const normalized: TeamThemePolicy = {
    version: 1,
    ...(colorProfile ? { defaults: { colorProfile } } : {}),
    ...(colorProfile && policy.locks?.colorProfile
      ? { locks: { colorProfile: true } }
      : {}),
    ...(colorProfile && policy.brand?.name
      ? {
          brand: {
            name: policy.brand.name,
            colorProfileId: colorProfile.id,
          },
        }
      : {}),
  };
  return JSON.stringify(normalized);
}

export const SettingsAppearance = PersonalAppearanceSettings;

/**
 * Legacy team-policy compatibility panel. Team branding V3 owns its eventual
 * replacement; keep this narrow export until that editor moves in T4.
 */
export function TeamThemePanel({
  title,
  description,
  lockLabel,
  clearLabel,
  busyLabel,
  scopeAllLabel,
  scopeAdminLabel,
  dividerLabel,
  appliedLabel,
  brandLabel,
  policy,
  busy,
  error,
  labelFor,
  onSave,
}: {
  title: string;
  description: string;
  lockLabel: string;
  clearLabel: string;
  busyLabel: string;
  scopeAllLabel: string;
  scopeAdminLabel: string;
  dividerLabel: string;
  appliedLabel: string;
  brandLabel: string;
  policy: TeamThemePolicy;
  busy: boolean;
  error: string | null;
  labelFor: (key: string) => string;
  onSave: (policy: TeamThemePolicy) => void;
}) {
  const selected = policy.defaults?.colorProfile?.id;
  const locked = Boolean(policy.locks?.colorProfile && selected);

  return (
    <>
      <div className="ap-team-divider">
        <span className="ln" />
        <span className="tx">{dividerLabel}</span>
        <span className="ln" />
      </div>
      <div className="ap-team-card" data-testid="appearance-team-theme-panel">
        <div className="ap-team-head">
          <span className="ap-team-ico">
            <Building size={19} />
          </span>
          <div className="ap-team-htt">
            <div className="ap-team-scope">
              <span className="ap-team-pill all">
                <span className="pip" />
                {scopeAllLabel}
              </span>
              <span className="ap-team-pill adm">
                <Lock />
                {scopeAdminLabel}
              </span>
            </div>
            <div className="ap-team-title">{title}</div>
            <div className="ap-team-body">{description}</div>
          </div>
        </div>

        <div
          role="group"
          aria-label={brandLabel}
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fit, minmax(128px, 1fr))",
            gap: 8,
          }}
        >
          {ACCENT_OPTIONS.map((item) => {
            const isSelected = selected === item.value;
            return (
              <button
                key={item.value}
                type="button"
                disabled={busy}
                aria-pressed={isSelected}
                onClick={() =>
                  onSave({
                    version: 1,
                    defaults: {
                      colorProfile: { type: "preset", id: item.value },
                    },
                    ...(locked ? { locks: { colorProfile: true } } : {}),
                  })
                }
                style={{
                  appearance: "none",
                  border: `1px solid ${isSelected ? "var(--c-accent)" : "var(--c-line)"}`,
                  borderRadius: "var(--r-1)",
                  background: isSelected
                    ? "var(--c-accent-tint)"
                    : "var(--c-surface)",
                  color: "var(--c-ink)",
                  cursor: busy ? "wait" : "pointer",
                  minHeight: 42,
                  padding: "8px 10px",
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "space-between",
                  gap: 10,
                  fontFamily: "var(--font-v2-sans)",
                  fontSize: "var(--fs-sm)",
                  fontWeight: 500,
                }}
              >
                <span
                  style={{
                    display: "inline-flex",
                    alignItems: "center",
                    gap: 8,
                  }}
                >
                  <span
                    style={{
                      width: 18,
                      height: 18,
                      borderRadius: "var(--r-pill)",
                      background: item.swatch,
                      boxShadow: "inset 0 0 0 1px rgba(0,0,0,0.10)",
                    }}
                  />
                  {labelFor(item.labelKey)}
                </span>
                {isSelected ? <Check size={12} aria-hidden /> : null}
              </button>
            );
          })}
        </div>

        <label
          style={{
            display: "flex",
            alignItems: "center",
            gap: 10,
            padding: "11px 13px",
            border: "1px solid var(--c-accent-soft)",
            borderRadius: "var(--r-2)",
            background: "var(--c-surface)",
            color: selected ? "var(--c-ink-2)" : "var(--c-ink-3)",
            fontSize: "var(--fs-sm)",
            cursor: selected && !busy ? "pointer" : "default",
          }}
        >
          <span style={{ flex: 1, minWidth: 0 }}>{lockLabel}</span>
          <input
            type="checkbox"
            checked={locked}
            disabled={busy || !selected}
            onChange={(event) => {
              if (!selected) return;
              onSave({
                version: 1,
                defaults: {
                  colorProfile: { type: "preset", id: selected },
                },
                ...(event.currentTarget.checked
                  ? { locks: { colorProfile: true } }
                  : {}),
              });
            }}
            style={{
              accentColor: "var(--c-accent)",
              width: 16,
              height: 16,
              flex: "none",
            }}
          />
        </label>

        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 10,
            flexWrap: "wrap",
          }}
        >
          {selected ? (
            <span
              style={{
                display: "inline-flex",
                alignItems: "center",
                gap: 6,
                color: "var(--c-info)",
                fontSize: "var(--fs-xs)",
                fontFamily: "var(--font-v2-mono)",
              }}
            >
              <Check size={12} aria-hidden /> {appliedLabel}
            </span>
          ) : null}
          <span style={{ flex: 1 }} />
          <Button
            size="sm"
            onClick={() => onSave(EMPTY_TEAM_POLICY)}
            disabled={busy || !selected}
          >
            {clearLabel}
          </Button>
          {busy ? (
            <span
              style={{ color: "var(--c-ink-3)", fontSize: "var(--fs-xs)" }}
            >
              {busyLabel}
            </span>
          ) : null}
          {error ? (
            <span
              role="alert"
              style={{ color: "var(--c-err)", fontSize: "var(--fs-xs)" }}
            >
              {error}
            </span>
          ) : null}
        </div>
      </div>
    </>
  );
}
