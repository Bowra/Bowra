"use client";

import { useEffect, useRef } from "react";
import { usePathname } from "next/navigation";

import {
  activateCachedBrandForTeam,
  syncBrandThemeColor,
  syncBrandFromStorage,
} from "@/lib/brand-runtime";
import {
  currentClientPathname,
  isPublicStartupRoute,
} from "@/lib/public-startup-route";
import { beginBrandTransition } from "@/lib/effective-theme-request-guard";
import { TEAM_CHANGED_EVENT } from "@/lib/team-storage";
import { bootstrapEffectiveTheme } from "@/lib/use-theme-config";

/**
 * followup-context-deck #2 — mount globally once (inside layout). Fetch and apply the team's effective theme on app load
 * so admin color changes apply globally on the next load without manually visiting Settings → Colors.
 *
 * The pre-hydration <script> (app/layout.tsx) already applied the localStorage theme (no first-frame flash). Here,
 * asynchronously override and persist the backend team palette, making localStorage ≈ backend for flash-free reloads. Render null.
 */
export function ThemeBootstrap(): null {
  const pathname = currentClientPathname(usePathname());
  const bootstrappedRef = useRef(false);
  useEffect(() => {
    // The effective-theme endpoint is authenticated. Public routes must never
    // request it: its 401 is intentionally handled by apiFetch as a logout,
    // which would otherwise race the local first-run activation redirect.
    if (
      isPublicStartupRoute(pathname) ||
      pathname.startsWith("/internal/canvas-export/")
    )
      return;
    if (!bootstrappedRef.current) {
      bootstrappedRef.current = true;
      void bootstrapEffectiveTheme();
    }

    const onTeamChanged = (event: Event) => {
      const teamId =
        (event as CustomEvent<{ teamId?: string }>).detail?.teamId ?? "";
      if (!teamId) return;
      beginBrandTransition(teamId);
      activateCachedBrandForTeam(teamId);
      void bootstrapEffectiveTheme(teamId);
    };
    const onStorage = (event: StorageEvent) => {
      syncBrandFromStorage(event);
    };
    const systemTheme = window.matchMedia?.("(prefers-color-scheme: dark)");
    const onSystemThemeChanged = () => syncBrandThemeColor();
    window.addEventListener(TEAM_CHANGED_EVENT, onTeamChanged);
    window.addEventListener("storage", onStorage);
    systemTheme?.addEventListener("change", onSystemThemeChanged);
    return () => {
      window.removeEventListener(TEAM_CHANGED_EVENT, onTeamChanged);
      window.removeEventListener("storage", onStorage);
      systemTheme?.removeEventListener("change", onSystemThemeChanged);
    };
  }, [pathname]);
  return null;
}
