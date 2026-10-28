/**
 * useAuthRedirect — unified auth gate for protected pages.
 *
 * Calls /api/v1/auth/me, mirrors AuthError handling across pages:
 *   - 401 (anonymous / expired) → window.location.replace(appPath('/login/?next=<currentPath>'))
 *   - 200 → returns { status: "authed", me, isTeamAdmin }
 *   - 200 but requireTeamAdmin=true and !is_team_admin → status: "not_admin"
 *   - other errors → status: "error" (caller can decide; defaults bubble up)
 *
 * Replaces ad-hoc "Please log in." / "需要登录" / inline alerts across:
 *   /knowledge,
 *   /admin/credits, /admin/credits/users, /admin/turns.
 *
 * Why window.location (not Next.js router) — matches existing /knowledge pattern
 * and bypasses client-side router cache so /login renders fresh.
 */

"use client";

import { useEffect, useState } from "react";

import {
  AuthError,
  clearInvalidSessionAndRedirect,
  fetchMe,
  getBootstrapStatus,
  localActivationRequired,
  type MeUser,
} from "@/lib/auth-api";
import { appPath } from "@/lib/base-path";
import { useOptionalAuthSession } from "@/lib/auth/auth-session-context";

export type AuthRedirectStatus =
  | "loading"
  | "redirecting"
  | "authed"
  | "not_admin"
  | "error";

export interface AuthRedirectResult {
  status: AuthRedirectStatus;
  me: MeUser | null;
  isTeamAdmin: boolean;
  /** Non-401 error message (network / 5xx). 401 always triggers redirect, not error. */
  error: string | null;
  /** Raw failure object, for diagnostics only (Copy Diagnostics or the collapsible area), never direct display. */
  errorCause?: unknown;
}

export interface UseAuthRedirectOptions {
  /** If true, also gate on me.is_team_admin (returns status: "not_admin" when false). */
  requireTeamAdmin?: boolean;
  /** Gate on team role when backend returns team_role. */
  allowedTeamRoles?: Array<NonNullable<MeUser["team_role"]>>;
  /** Gate on credit capability keys, e.g. "credits_read" or "credit:credits_read". */
  requiredCapabilities?: string[];
  /** Override path used in ?next=. Defaults to window.location.pathname + search. */
  returnPath?: string;
}

function currentReturnPath(): string {
  if (typeof window === "undefined") return "/";
  return window.location.pathname + window.location.search;
}

export function useAuthRedirect(
  options: UseAuthRedirectOptions = {},
): AuthRedirectResult {
  const {
    requireTeamAdmin = false,
    returnPath,
  } = options;
  const allowedTeamRolesKey = (options.allowedTeamRoles ?? []).join("|");
  const requiredCapabilitiesKey = (options.requiredCapabilities ?? []).join("|");
  const sharedSession = useOptionalAuthSession();
  const [state, setState] = useState<AuthRedirectResult>({
    status: "loading",
    me: null,
    isTeamAdmin: false,
    error: null,
  });

  useEffect(() => {
    if (sharedSession) return;
    let cancelled = false;
    const allowedTeamRoles = allowedTeamRolesKey
      ? allowedTeamRolesKey.split("|") as Array<NonNullable<MeUser["team_role"]>>
      : [];
    const requiredCapabilities = requiredCapabilitiesKey
      ? requiredCapabilitiesKey.split("|")
      : [];
    fetchMe()
      .then((u) => {
        if (cancelled) return;
        if (u.membership_required) {
          setState({ status: "redirecting", me: u, isTeamAdmin: false, error: null });
          if (typeof window !== "undefined") {
            window.location.replace(appPath("/access-required/"));
          }
          return;
        }
        const isAdmin = !!u.is_team_admin || u.team_role === "owner" || u.team_role === "team_admin";
        if (requireTeamAdmin && !isAdmin) {
          setState({ status: "not_admin", me: u, isTeamAdmin: false, error: null });
          return;
        }
        if (allowedTeamRoles.length > 0 && !allowedTeamRoles.includes(u.team_role ?? "member")) {
          setState({ status: "not_admin", me: u, isTeamAdmin: isAdmin, error: null });
          return;
        }
        if (requiredCapabilities.length > 0 &&
            !requiredCapabilities.every((cap) => hasCapability(u, cap))) {
          setState({ status: "not_admin", me: u, isTeamAdmin: isAdmin, error: null });
          return;
        }
        setState({ status: "authed", me: u, isTeamAdmin: isAdmin, error: null });
      })
      .catch(async (e: unknown) => {
        if (cancelled) return;
        if (e instanceof AuthError && e.status === 401) {
          // Keep the trailing slash: static hosting otherwise redirects /login to
          // /login/ and a prefix-stripping reverse proxy loses the public-entry prefix.
          setState({ status: "redirecting", me: null, isTeamAdmin: false, error: null });
          if (typeof window !== "undefined") {
            void clearInvalidSessionAndRedirect(returnPath ?? currentReturnPath());
          }
          return;
        }
        if (e instanceof AuthError && e.status === 421) {
          const bootstrapStatus = await getBootstrapStatus().catch(() => null);
          if (cancelled) return;
          if (localActivationRequired(bootstrapStatus)) {
            setState({ status: "redirecting", me: null, isTeamAdmin: false, error: null });
            if (typeof window !== "undefined") {
              window.location.replace(appPath("/activate/"));
            }
            return;
          }
        }
        setState({
          status: "error",
          me: null,
          isTeamAdmin: false,
          error: e instanceof Error ? e.message : String(e),
          errorCause: e,
        });
      });
    return () => {
      cancelled = true;
    };
  }, [
    requireTeamAdmin,
    allowedTeamRolesKey,
    requiredCapabilitiesKey,
    returnPath,
    sharedSession,
  ]);

  if (!sharedSession) return state;
  if (sharedSession.status === "loading") {
    return {
      status: "loading",
      me: null,
      isTeamAdmin: false,
      error: null,
    };
  }
  if (sharedSession.status === "redirecting") {
    return {
      status: "redirecting",
      me: null,
      isTeamAdmin: false,
      error: null,
    };
  }
  if (sharedSession.status === "error" || !sharedSession.me) {
    return {
      status: "error",
      me: null,
      isTeamAdmin: false,
      error: sharedSession.error,
      errorCause: sharedSession.errorCause,
    };
  }
  return accessResultForUser(sharedSession.me, {
    requireTeamAdmin,
    allowedTeamRoles: allowedTeamRolesKey
      ? allowedTeamRolesKey.split("|") as Array<
          NonNullable<MeUser["team_role"]>
        >
      : [],
    requiredCapabilities: requiredCapabilitiesKey
      ? requiredCapabilitiesKey.split("|")
      : [],
  });
}

function accessResultForUser(
  user: MeUser,
  options: {
    requireTeamAdmin: boolean;
    allowedTeamRoles: Array<NonNullable<MeUser["team_role"]>>;
    requiredCapabilities: string[];
  },
): AuthRedirectResult {
  const isAdmin =
    Boolean(user.is_team_admin) ||
    user.team_role === "owner" ||
    user.team_role === "team_admin";
  const roleAllowed =
    options.allowedTeamRoles.length === 0 ||
    options.allowedTeamRoles.includes(user.team_role ?? "member");
  const capabilitiesAllowed =
    options.requiredCapabilities.length === 0 ||
    options.requiredCapabilities.every((capability) =>
      hasCapability(user, capability),
    );

  if (
    (options.requireTeamAdmin && !isAdmin) ||
    !roleAllowed ||
    !capabilitiesAllowed
  ) {
    return {
      status: "not_admin",
      me: user,
      isTeamAdmin: isAdmin,
      error: null,
    };
  }
  return {
    status: "authed",
    me: user,
    isTeamAdmin: isAdmin,
    error: null,
  };
}

function hasCapability(user: MeUser, capability: string): boolean {
  const key = capability.startsWith("credit:")
    ? capability.slice("credit:".length)
    : capability;
  const creditCaps = user.credit_capabilities ?? user.capabilities?.credit;
  if (!creditCaps || !(key in creditCaps)) return false;
  return Boolean(creditCaps[key as keyof typeof creditCaps]);
}
