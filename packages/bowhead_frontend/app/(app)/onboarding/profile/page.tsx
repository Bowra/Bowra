"use client";

import {
 Suspense, useEffect, useMemo, useState } from "react";
import { useSearchParams } from "next/navigation";

import { AuthField } from "@/components/auth-shell";
import { Check } from "@/components/icons";
import {
  BusyLabel,
  PublicBanner,
  PublicShell,
  PublicSteps,
  PublicSummary,
  useBrandedTranslations,
} from "@/components/public-shell";
import { fetchWithRefresh } from "@/lib/auth-api";
import { readInviteResult, type InviteType } from "@/lib/invite-api";
import { safeInternalPath } from "@/lib/safe-next-path";
import { useAppRouter } from "@/lib/use-app-router";

type Profile = {
  display_name?: string | null;
  preferences?: Record<string, unknown>;
};

const GOALS = [
  "analyze_metrics",
  "write_reports",
  "exec_updates",
  "explore_data",
  "manage_knowledge",
  "team_workflows",
] as const;

type Goal = (typeof GOALS)[number];

const LEGACY_GOALS: Record<string, Goal> = {
  "Analyze business metrics": "analyze_metrics",
  "Write verified reports": "write_reports",
  "Prepare exec updates": "exec_updates",
  "Explore data faster": "explore_data",
  "Manage knowledge": "manage_knowledge",
  "Coordinate team workflows": "team_workflows",
};

export default function InviteProfileOnboardingPage() {
  return (
    <Suspense fallback={null}>
      <InviteProfileOnboardingInner />
    </Suspense>
  );
}

function InviteProfileOnboardingInner() {
  const t = useBrandedTranslations("invite");
  const router = useAppRouter();
  const searchParams = useSearchParams();
  const [displayName, setDisplayName] = useState("");
  const [selected, setSelected] = useState<Goal[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const inviteResult = useMemo(() => readInviteResult(), []);
  const nextRoute = safeInternalPath(searchParams.get("next"))
    || safeInternalPath(inviteResult?.ready_route || "")
    || "/invite/ready";
  const inviteType = inviteResult?.invite_type || inviteTypeFromReadyRoute(nextRoute);
  const isWorkspace = inviteType === "workspace";

  useEffect(() => {
    let cancelled = false;
    fetchProfile()
      .then((p) => {
        if (cancelled) return;
        setDisplayName(p.display_name || "");
        const saved = (p.preferences?.invite_onboarding as { goals?: unknown } | undefined)?.goals;
        if (Array.isArray(saved)) {
          setSelected(normalizeGoals(saved));
        }
      })
      .catch((e: unknown) => {
        if (cancelled) return;
        const msg = e instanceof Error ? e.message : String(e);
        if (msg.includes("401") && typeof window !== "undefined") {
          router.replace(`/login?next=${encodeURIComponent(window.location.pathname + window.location.search)}`);
          return;
        }
        setError(t(profileErrorKey(msg)));
      });
    return () => {
      cancelled = true;
    };
  }, [router, t]);

  const toggle = (goal: Goal) => {
    setSelected((prev) => (
      prev.includes(goal) ? prev.filter((item) => item !== goal) : [...prev, goal]
    ));
  };

  const save = async (skipped: boolean) => {
    setBusy(true);
    setError(null);
    try {
      const latest = await fetchProfile();
      const preferences = {
        ...(latest.preferences || {}),
        invite_onboarding: {
          goals: skipped ? [] : selected,
          skipped,
          completed_at: new Date().toISOString(),
          invite_type: inviteType || null,
        },
      };
      await patchProfile({
        display_name: displayName.trim() || undefined,
        preferences,
      });
      router.replace(nextRoute);
    } catch (e) {
      setError(
        t(
          profileErrorKey(
            e instanceof Error ? e.message : String(e),
          ),
        ),
      );
    } finally {
      setBusy(false);
    }
  };

  return (
    <PublicShell
      width="invite"
      steps={<PublicSteps current={2} label={t("progress.step_2.title")} />}
      title={t("profile.title")}
      subtitle={t("profile.lead")}
    >
      <PublicSummary
        label={t("profile.summary_aria")}
        rows={[
          {
            term: isWorkspace ? t("profile.space_invite") : t("profile.team_invite"),
            value: isWorkspace
              ? inviteResult?.workspace?.name || t("common.invited_space")
              : t("profile.team_profile"),
          },
          {
            term: t("summary.access"),
            value: isWorkspace ? t("profile.access_after_invite") : t("profile.team_joined"),
          },
          { term: t("summary.next"), value: isWorkspace ? t("profile.enter_space") : t("profile.open_team_profile") },
        ]}
      />
      <div className="pub-fields">
        <AuthField label={t("profile.display_name")}>
          {(control) => (
            <input
              {...control}
              type="text"
              value={displayName}
              autoComplete="name"
              onChange={(e) => setDisplayName(e.target.value)}
              placeholder={t("profile.name_placeholder")}
              className="pub-input"
            />
          )}
        </AuthField>
        <fieldset className="pub-choices">
          <legend className="pub-field__label">{t("profile.goals_title")}</legend>
          <div className="pub-choices__list">
            {GOALS.map((goal) => {
              const on = selected.includes(goal);
              return (
                <button
                  key={goal}
                  type="button"
                  className="pub-choice"
                  aria-pressed={on}
                  onClick={() => toggle(goal)}
                >
                  {on ? <Check size={12} aria-hidden="true" /> : null}
                  {t(`profile.goals.${goal}`)}
                </button>
              );
            })}
          </div>
        </fieldset>
      </div>
      {error ? <PublicBanner tone="error" focusKey={error}>{error}</PublicBanner> : null}
      <div className="pub-actions">
        <button
          className="pub-button pub-button--primary"
          type="button"
          disabled={busy}
          aria-busy={busy || undefined}
          onClick={() => void save(false)}
        >
          <BusyLabel busy={busy} idle={t("action.continue")} pending={t("action.saving")} />
        </button>
        <button className="pub-button pub-button--ghost" type="button" disabled={busy} onClick={() => void save(true)}>
          {t("action.skip")}
        </button>
      </div>
      <p className="pub-note">{t("profile.permission_note")}</p>
    </PublicShell>
  );
}

async function fetchProfile(): Promise<Profile> {
  const response = await fetchWithRefresh("/api/v1/profile/me");
  if (!response.ok) throw new Error(`profile: ${response.status}`);
  return response.json() as Promise<Profile>;
}

async function patchProfile(body: {
  display_name?: string;
  preferences: Record<string, unknown>;
}): Promise<void> {
  const response = await fetchWithRefresh("/api/v1/profile/me", {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!response.ok) throw new Error(`profile update: ${response.status}`);
}

function profileErrorKey(
  message: string,
): "profile.error_sign_in" | "profile.error_save" | "profile.error_load" {
  if (message.includes("401")) {
    return "profile.error_sign_in";
  }
  if (message.includes("profile update")) {
    return "profile.error_save";
  }
  return "profile.error_load";
}

function normalizeGoals(values: unknown[]): Goal[] {
  const normalized = values
    .filter((value): value is string => typeof value === "string")
    .map((value) => {
      if ((GOALS as readonly string[]).includes(value)) {
        return value as Goal;
      }
      return LEGACY_GOALS[value] ?? null;
    })
    .filter((value): value is Goal => value !== null);
  return [...new Set(normalized)];
}

const selectedChipStyle = {
  borderColor: "var(--c-accent)",
  background: "var(--c-accent-tint)",
  color: "var(--c-accent-hover)",
} as const;

function inviteTypeFromReadyRoute(route: string): InviteType | null {
  try {
    const url = new URL(route, "http://bowhead.local");
    if (url.pathname !== "/invite/ready") return null;
    return url.searchParams.get("state") === "workspace_ready" ? "workspace" : "team";
  } catch {
    return route.includes("state=workspace_ready") ? "workspace" : null;
  }
}
