"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { deleteUserSetting, getEffectiveTheme, setUserSetting } from "./api";
import { applyBrandBundleToDocument } from "./brand-runtime";
import {
  acceptEffectiveThemeResponse,
  beginEffectiveThemeRequest,
  isEffectiveThemeRequestCurrent,
} from "./effective-theme-request-guard";
import {
  APPEARANCE_THEME_CONFIG_KEY,
  DEFAULT_THEME_CONFIG,
  DEFAULT_USER_THEME_CONFIG,
  THEME_CONFIG_CHANGED_EVT,
  THEME_CONFIG_STORAGE_KEY,
  THEME_STORAGE_KEY,
  applyThemeConfigToDoc,
  loadThemeConfig,
  resetThemeConfig,
  resolveThemeMode,
  saveThemeConfig,
  serializeUserThemeConfig,
  type EffectiveThemeResponse,
  type Theme,
  type ThemeConfig,
  type UserThemeConfig,
} from "./theme-config";

type ThemeLocks = Partial<Record<"colorProfile", boolean>>;
export type ThemeConfigPatch = {
  [Key in Exclude<keyof ThemeConfig, "version">]?: ThemeConfig[Key] | undefined;
};

function applyEffectiveTheme(response: EffectiveThemeResponse) {
  const resolved = resolveThemeMode(response.effective.mode);
  applyBrandBundleToDocument(response);
  return resolved;
}

export async function getGuardedEffectiveTheme(
  expectedTeamId?: string,
): Promise<EffectiveThemeResponse | null> {
  const request = beginEffectiveThemeRequest(expectedTeamId);
  try {
    const response = await getEffectiveTheme(request.signal);
    return acceptEffectiveThemeResponse(request.token, response)
      ? response
      : null;
  } catch (error) {
    if (
      !isEffectiveThemeRequestCurrent(request.token) ||
      (error instanceof Error && error.name === "AbortError")
    ) {
      return null;
    }
    throw error;
  } finally {
    request.complete();
  }
}

export async function refreshEffectiveTheme(
  expectedTeamId?: string,
): Promise<boolean> {
  const response = await getGuardedEffectiveTheme(expectedTeamId);
  if (!response) return false;
  applyEffectiveTheme(response);
  return true;
}

/**
 * followup-context-deck #2 — fetch and apply team effective theme once at global app load.
 *
 * Bug: after admin changed team colors, ordinary users had to open Settings → Colors (mount useThemeConfig to
 * refresh) to see them; app startup only read localStorage through pre-hydration <script>, never fetched
 * /theme/effective, so team colors did not propagate. Global mount (<ThemeBootstrap/>) calls this
 * once: fetch + applyEffectiveTheme (write document + localStorage) + dispatch THEME_CONFIG_CHANGED_EVT
 * to synchronize React state in mounted useTheme / useThemeConfig listeners.
 *
 * Fetch failure (unauthenticated / offline / unreachable backend, e.g. login-page 401) → catch no-op, retaining
 * localStorage theme applied by pre-hydration <script>; never throw and break first paint.
 */
export async function bootstrapEffectiveTheme(
  expectedTeamId?: string,
): Promise<void> {
  try {
    await refreshEffectiveTheme(expectedTeamId);
  } catch {
    // Silent: retain localStorage theme (already applied by pre-hydration script).
  }
}

function applyPatchToUserConfig(
  userConfig: UserThemeConfig,
  patch: Partial<ThemeConfig>,
  locks: ThemeLocks,
): UserThemeConfig {
  const next: UserThemeConfig = { ...userConfig, version: 1 };
  for (const [key, value] of Object.entries(patch) as Array<
    [keyof ThemeConfig, ThemeConfig[keyof ThemeConfig]]
  >) {
    if (key === "version") continue;
    if (key === "colorProfile" && locks.colorProfile) continue;
    if (value === undefined) {
      delete (next as unknown as Record<string, unknown>)[key];
    } else {
      (next as unknown as Record<string, unknown>)[key] = value;
    }
  }
  return next;
}

export function useThemeConfig(): {
  config: ThemeConfig;
  userConfig: UserThemeConfig;
  resolvedTheme: Theme;
  locks: ThemeLocks;
  source: EffectiveThemeResponse["source"];
  teamBrandName?: string | null;
  isLoading: boolean;
  error: string | null;
  refresh: () => Promise<void>;
  setConfig: (patch: ThemeConfigPatch) => Promise<void>;
  reset: () => Promise<void>;
} {
  const [config, setConfigState] = useState<ThemeConfig>(() =>
    loadThemeConfig(),
  );
  const [userConfig, setUserConfig] = useState<UserThemeConfig>(
    DEFAULT_USER_THEME_CONFIG,
  );
  const [resolvedTheme, setResolvedTheme] = useState<Theme>(() =>
    resolveThemeMode(loadThemeConfig().mode),
  );
  const [locks, setLocks] = useState<ThemeLocks>({});
  const [source, setSource] = useState<EffectiveThemeResponse["source"]>({});
  const [teamBrandName, setTeamBrandName] = useState<string | null | undefined>(
    undefined,
  );
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const configRef = useRef(config);
  const userConfigRef = useRef(userConfig);
  const locksRef = useRef(locks);
  const mutationRevisionRef = useRef(0);
  const saveChainRef = useRef<Promise<void>>(Promise.resolve());
  const mountedRef = useRef(false);
  const internalThemeWriteRef = useRef(false);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  const applyResponse = useCallback((response: EffectiveThemeResponse) => {
    if (!mountedRef.current) return;
    configRef.current = response.effective;
    userConfigRef.current = response.userConfig ?? DEFAULT_USER_THEME_CONFIG;
    locksRef.current = response.locks ?? {};
    setConfigState(response.effective);
    setUserConfig(response.userConfig ?? DEFAULT_USER_THEME_CONFIG);
    setLocks(response.locks ?? {});
    setSource(response.source ?? {});
    setTeamBrandName(
      response.brand?.applicationName ?? response.team?.brandName,
    );
    internalThemeWriteRef.current = true;
    try {
      setResolvedTheme(applyEffectiveTheme(response));
    } finally {
      internalThemeWriteRef.current = false;
    }
  }, []);

  const refresh = useCallback(
    async (force = false) => {
      const requestRevision = mutationRevisionRef.current;
      if (mountedRef.current) setIsLoading(true);
      try {
        const response = await getGuardedEffectiveTheme();
        if (!response || !mountedRef.current) return;
        if (force || requestRevision === mutationRevisionRef.current) {
          applyResponse(response);
        }
        setError(null);
      } catch (err) {
        if (!mountedRef.current) return;
        setError(err instanceof Error ? err.message : "Failed to load theme");
        const local = loadThemeConfig();
        const resolved = resolveThemeMode(local.mode);
        configRef.current = local;
        setConfigState(local);
        setResolvedTheme(resolved);
        applyThemeConfigToDoc(local, resolved);
      } finally {
        if (mountedRef.current) setIsLoading(false);
      }
    },
    [applyResponse],
  );

  useEffect(() => {
    void refresh();

    const syncLocal = () => {
      const next = loadThemeConfig();
      const resolved = resolveThemeMode(next.mode);
      configRef.current = next;
      setConfigState(next);
      setResolvedTheme(resolved);
      applyThemeConfigToDoc(next, resolved);
    };
    const onStorage = (event: StorageEvent) => {
      if (
        event.key &&
        event.key !== THEME_CONFIG_STORAGE_KEY &&
        event.key !== THEME_STORAGE_KEY
      )
        return;
      syncLocal();
    };
    // Configuration copilot batch 0.5 (review P2-3): CHANGED_EVT may come from external writers (chat lite confirmation
    // apply → refreshEffectiveTheme broadcast). Only syncLocal without updating userConfigRef leaves this mounted component's
    // next setConfig writing a stale sparse userConfig wholesale, silently erasing chat changes;
    // refresh on event too. Chain refresh after saveChainRef: this hook's own optimistic save also
    // broadcasts the event, so immediate GET precedes PUT persistence and reads old values; chaining after save always reads persisted state
    // (revision guard separately prevents overwriting newer in-flight optimistic state).
    const syncLocalAndRefresh = () => {
      const internalWrite = internalThemeWriteRef.current;
      syncLocal();
      if (internalWrite) return;
      // Catch first: a rejected save chain (after setConfig failure) never executes derived then
      // and leaks an unhandledrejection per event (implementation review P2-1).
      void saveChainRef.current.catch(() => undefined).then(() => refresh());
    };
    const media = window.matchMedia?.("(prefers-color-scheme: dark)");
    window.addEventListener("storage", onStorage);
    window.addEventListener(THEME_CONFIG_CHANGED_EVT, syncLocalAndRefresh);
    media?.addEventListener?.("change", syncLocal);
    return () => {
      window.removeEventListener("storage", onStorage);
      window.removeEventListener(THEME_CONFIG_CHANGED_EVT, syncLocalAndRefresh);
      media?.removeEventListener?.("change", syncLocal);
    };
  }, [refresh]);

  const setConfig = async (patch: ThemeConfigPatch) => {
    const writeRevision = mutationRevisionRef.current + 1;
    mutationRevisionRef.current = writeRevision;
    const cleanPatch = { ...patch };
    const currentLocks = locksRef.current;
    if (currentLocks.colorProfile) delete cleanPatch.colorProfile;
    const optimistic = { ...configRef.current };
    for (const [key, value] of Object.entries(cleanPatch)) {
      if (value !== undefined) {
        (optimistic as unknown as Record<string, unknown>)[key] = value;
      }
    }
    const nextUserConfig = applyPatchToUserConfig(
      userConfigRef.current,
      cleanPatch,
      currentLocks,
    );

    configRef.current = optimistic;
    userConfigRef.current = nextUserConfig;
    setConfigState(optimistic);
    setUserConfig(nextUserConfig);
    setResolvedTheme(resolveThemeMode(optimistic.mode));
    internalThemeWriteRef.current = true;
    try {
      saveThemeConfig(optimistic);
    } finally {
      internalThemeWriteRef.current = false;
    }
    const previousSave = saveChainRef.current.catch(() => undefined);
    const savePromise = previousSave.then(async () => {
      await setUserSetting(
        APPEARANCE_THEME_CONFIG_KEY,
        serializeUserThemeConfig(userConfigRef.current),
      );
      const response = await getGuardedEffectiveTheme();
      if (!response) return;
      if (writeRevision === mutationRevisionRef.current) {
        applyResponse(response);
      }
      if (mountedRef.current) setError(null);
    });
    saveChainRef.current = savePromise;
    try {
      await savePromise;
    } catch (err) {
      if (
        mountedRef.current &&
        writeRevision === mutationRevisionRef.current
      ) {
        setError(err instanceof Error ? err.message : "Failed to save theme");
        await refresh(true);
      }
    }
  };

  const reset = async () => {
    const writeRevision = mutationRevisionRef.current + 1;
    mutationRevisionRef.current = writeRevision;
    userConfigRef.current = DEFAULT_USER_THEME_CONFIG;
    setUserConfig(DEFAULT_USER_THEME_CONFIG);
    internalThemeWriteRef.current = true;
    try {
      resetThemeConfig();
    } finally {
      internalThemeWriteRef.current = false;
    }
    const previousSave = saveChainRef.current.catch(() => undefined);
    const savePromise = previousSave.then(async () => {
      await deleteUserSetting(APPEARANCE_THEME_CONFIG_KEY);
      const response = await getGuardedEffectiveTheme();
      if (!response) return;
      if (writeRevision === mutationRevisionRef.current) {
        applyResponse(response);
      }
      if (mountedRef.current) setError(null);
    });
    saveChainRef.current = savePromise;
    try {
      await savePromise;
    } catch (err) {
      if (
        mountedRef.current &&
        writeRevision === mutationRevisionRef.current
      ) {
        setError(err instanceof Error ? err.message : "Failed to reset theme");
        await refresh(true);
      }
    }
  };

  return {
    config,
    userConfig,
    resolvedTheme,
    locks,
    source,
    teamBrandName,
    isLoading,
    error,
    refresh,
    setConfig,
    reset,
  };
}
