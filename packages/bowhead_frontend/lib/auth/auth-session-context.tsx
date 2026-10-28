"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";

import {
  AuthError,
  clearInvalidSessionAndRedirect,
  fetchMe,
  getBootstrapStatus,
  invalidateMeCache,
  localActivationRequired,
  revalidateMe,
  type MeUser,
} from "@/lib/auth-api";
import { appPath } from "@/lib/base-path";
import { TEAM_CHANGED_EVENT } from "@/lib/team-storage";

export type AuthSessionStatus =
  | "loading"
  | "authenticated"
  | "redirecting"
  | "error";

export interface AuthSessionValue {
  status: AuthSessionStatus;
  me: MeUser | null;
  error: string | null;
  /** Raw failure object, for diagnostics only (Copy Diagnostics or the collapsible area), never direct display. */
  errorCause?: unknown;
  retry: () => void;
  revalidateCapabilities: () => Promise<void>;
}

const AuthSessionContext = createContext<AuthSessionValue | null>(null);

export function AuthSessionProvider({ children }: { children: ReactNode }) {
  const requestSequenceRef = useRef(0);
  const sessionRef = useRef<Omit<AuthSessionValue, "retry" | "revalidateCapabilities">>({
    status: "loading",
    me: null,
    error: null,
  });
  const capabilityRefreshRef = useRef<Promise<void> | null>(null);
  const [session, setSession] = useState<
    Omit<AuthSessionValue, "retry" | "revalidateCapabilities">
  >({
    status: "loading",
    me: null,
    error: null,
  });

  const loadSession = useCallback(async () => {
    const sequence = requestSequenceRef.current + 1;
    requestSequenceRef.current = sequence;
    setSession({ status: "loading", me: null, error: null });
    markAuthStart();

    try {
      const me = await fetchMe();
      if (requestSequenceRef.current !== sequence) return;
      if (me.membership_required) {
        markAuthResolved();
        setSession({
          status: "redirecting",
          me: null,
          error: null,
        });
        window.location.replace(appPath("/access-required/"));
        return;
      }
      markAuthResolved();
      setSession({ status: "authenticated", me, error: null });
    } catch (error: unknown) {
      if (requestSequenceRef.current !== sequence) return;
      if (error instanceof AuthError && error.status === 401) {
        markAuthResolved();
        setSession({
          status: "redirecting",
          me: null,
          error: null,
        });
        void clearInvalidSessionAndRedirect(currentReturnPath());
        return;
      }
      if (error instanceof AuthError && error.status === 421) {
        const bootstrapStatus = await getBootstrapStatus().catch(() => null);
        if (requestSequenceRef.current !== sequence) return;
        if (localActivationRequired(bootstrapStatus)) {
          markAuthResolved();
          setSession({
            status: "redirecting",
            me: null,
            error: null,
          });
          window.location.replace(appPath("/activate/"));
          return;
        }
      }
      markAuthResolved();
      setSession({
        status: "error",
        me: null,
        error: error instanceof Error ? error.message : String(error),
        errorCause: error,
      });
    }
  }, []);

  const retry = useCallback(() => {
    invalidateMeCache();
    void loadSession();
  }, [loadSession]);

  // Keep async capability refreshes fenced to the latest committed render.
  // Assigning the ref during render avoids the one-effect delay in which an
  // immediate focus/click refresh could still observe the previous state.
  sessionRef.current = session;

  const revalidateCapabilities = useCallback((): Promise<void> => {
    const current = sessionRef.current;
    if (current.status !== "authenticated" || !current.me) {
      return Promise.resolve();
    }
    if (capabilityRefreshRef.current) return capabilityRefreshRef.current;
    const expectedUserId = current.me.id;
    const expectedTeamId = current.me.team_id;
    const expectedGeneration = current.me.auth_session_generation;
    const refresh = revalidateMe()
      .then((me) => {
        const latest = sessionRef.current;
        if (
          latest.status !== "authenticated"
          || latest.me?.id !== expectedUserId
          || latest.me.team_id !== expectedTeamId
          || latest.me.auth_session_generation !== expectedGeneration
        ) {
          return;
        }
        if (me.id !== expectedUserId || me.team_id !== expectedTeamId) {
          // A background request crossed an identity/team boundary. Tear down
          // the authenticated subtree before reloading so no old-user draft or
          // recovery payload can remain mounted under the new credentials.
          requestSequenceRef.current += 1;
          invalidateMeCache();
          setSession({ status: "loading", me: null, error: null });
          window.location.reload();
          return;
        }
        // A new auth generation for the same identity is authoritative (for
        // example after token rotation). Publishing it makes recovery scopes
        // reconcile immediately instead of silently retaining stale authority.
        setSession({ status: "authenticated", me, error: null });
      })
      .catch((error: unknown) => {
        if (error instanceof AuthError && error.status === 401) {
          requestSequenceRef.current += 1;
          setSession({ status: "redirecting", me: null, error: null });
          void clearInvalidSessionAndRedirect(currentReturnPath());
        }
        // Other transient refresh failures keep the authenticated tree mounted.
      })
      .finally(() => {
        if (capabilityRefreshRef.current === refresh) {
          capabilityRefreshRef.current = null;
        }
      });
    capabilityRefreshRef.current = refresh;
    return refresh;
  }, []);

  useEffect(() => {
    void loadSession();
    return () => {
      requestSequenceRef.current += 1;
    };
  }, [loadSession]);

  useEffect(() => {
    if (session.status !== "authenticated") return;
    const onFocus = () => void revalidateCapabilities();
    const onVisibility = () => {
      if (document.visibilityState === "visible") void revalidateCapabilities();
    };
    const timer = window.setInterval(() => {
      if (document.visibilityState === "visible") void revalidateCapabilities();
    }, 60_000);
    window.addEventListener("focus", onFocus);
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      window.clearInterval(timer);
      window.removeEventListener("focus", onFocus);
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [revalidateCapabilities, session.status]);

  useEffect(() => {
    const onTeamChanged = () => {
      invalidateMeCache();
      void loadSession();
    };
    window.addEventListener(TEAM_CHANGED_EVENT, onTeamChanged);
    return () => {
      window.removeEventListener(TEAM_CHANGED_EVENT, onTeamChanged);
    };
  }, [loadSession]);

  const value = useMemo<AuthSessionValue>(
    () => ({ ...session, retry, revalidateCapabilities }),
    [retry, revalidateCapabilities, session],
  );

  return (
    <AuthSessionContext.Provider value={value}>
      {children}
    </AuthSessionContext.Provider>
  );
}

export function useAuthSession(): AuthSessionValue {
  const session = useContext(AuthSessionContext);
  if (!session) {
    throw new Error("useAuthSession must be used inside AuthSessionProvider");
  }
  return session;
}

export function useOptionalAuthSession(): AuthSessionValue | null {
  return useContext(AuthSessionContext);
}

function currentReturnPath(): string {
  if (typeof window === "undefined") return "/";
  return window.location.pathname + window.location.search;
}

function markAuthStart(): void {
  if (
    typeof performance === "undefined" ||
    typeof performance.mark !== "function"
  ) {
    return;
  }
  try {
    performance.mark("bowhead_auth_start");
  } catch {
    // Telemetry must never change authentication control flow.
  }
}

function markAuthResolved(): void {
  if (
    typeof performance === "undefined" ||
    typeof performance.mark !== "function" ||
    typeof performance.measure !== "function"
  ) {
    return;
  }
  try {
    performance.mark("bowhead_auth_resolved");
    performance.measure(
      "bowhead_auth_resolution",
      "bowhead_auth_start",
      "bowhead_auth_resolved",
    );
  } catch {
    // Marks may be unavailable or cleared by another observer.
  }
}
