"use client";

import { PRODUCT_NAME } from "@/lib/product-identity";

import {
  BRAND_BUNDLE_CHANGED_EVT,
  BRAND_CACHE_POINTER_KEY,
  BRAND_CACHE_PREFIX,
  COLOR_SCHEME_TOKEN_KEYS,
  canonicalEffectiveThemeFallback,
  loadThemeConfig,
  parseEffectiveThemeResponse,
  resolveThemeMode,
  saveThemeConfig,
  type ColorSchemeMode,
  type EffectiveThemeResponse,
} from "./theme-config";
import { CANONICAL_THEME_CSS_TOKENS } from "./theme-surface-registry.generated";

const BRAND_STYLE_ID = "bowhead-team-brand-tokens";
const THEME_COLOR_SELECTOR = 'meta[name="theme-color"]';
// Product default scheme backgrounds (globals.css base palette).
const DEFAULT_THEME_COLORS: Record<"light" | "dark", string> = {
  light: "#F4F7FB",
  dark: "#0F172A",
};

interface CachedBrandBundle {
  version: 2;
  teamId: string;
  response: EffectiveThemeResponse;
}

interface BrandWindow extends Window {
  __BOWHEAD_PREHYDRATED_BRAND__?: unknown;
}

let currentBundle: EffectiveThemeResponse = initialBrandSnapshot();
const listeners = new Set<() => void>();

function initialBrandSnapshot(): EffectiveThemeResponse {
  if (typeof window === "undefined") return canonicalEffectiveThemeFallback();
  try {
    const prehydrated = (window as BrandWindow).__BOWHEAD_PREHYDRATED_BRAND__;
    return prehydrated
      ? parseEffectiveThemeResponse(prehydrated)
      : canonicalEffectiveThemeFallback();
  } catch {
    return canonicalEffectiveThemeFallback();
  }
}

export function getBrandSnapshot(): EffectiveThemeResponse {
  return currentBundle;
}

export function subscribeBrandSnapshot(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function applyBrandBundleToDocument(
  input: unknown,
  options: { persist?: boolean; notify?: boolean } = {},
): EffectiveThemeResponse {
  const response = parseEffectiveThemeResponse(input);
  const { persist = true, notify = true } = options;
  const effective = response.effective;
  const resolvedTheme = resolveThemeMode(effective.mode);

  saveThemeConfig(effective);
  if (typeof document !== "undefined") {
    const root = document.documentElement;
    clearInlineBrandTokens(root);
    const colorScheme = effective.colorScheme;
    if (colorScheme) {
      upsertBrandTokenStyle(colorScheme.light, colorScheme.dark);
      root.dataset.brandChecksum = colorScheme.checksum;
    } else {
      removeBrandTokenStyle();
      delete root.dataset.brandChecksum;
    }
    root.dataset.theme = resolvedTheme;
    root.dataset.brandTeam = response.team?.id ?? "";
    root.dataset.brandRevision = response.team?.revisionId ?? "";
    root.dataset.applicationName =
      response.brand?.applicationName ?? PRODUCT_NAME;
    updateThemeColor(effective, resolvedTheme);
  }

  currentBundle = response;
  if (persist) persistBrandBundle(response);
  if (notify) notifyBrandChanged(response);
  return response;
}

/** Keep authenticated browser chrome aligned when system mode changes. */
export function syncBrandThemeColor(): void {
  if (typeof document === "undefined") return;
  const effective = currentBundle.effective;
  updateThemeColor(effective, resolveThemeMode(effective.mode));
}

export function applyCanonicalBrandFallback(): EffectiveThemeResponse {
  clearBrandCachePointer();
  const fallback = canonicalEffectiveThemeFallback();
  fallback.effective = loadThemeConfig();
  return applyBrandBundleToDocument(fallback, {
    persist: false,
  });
}

export function activateCachedBrandForTeam(teamId: string): boolean {
  if (!teamId) {
    applyCanonicalBrandFallback();
    return false;
  }
  const cached = readCachedBrandBundle(teamId);
  if (!cached) {
    applyCanonicalBrandFallback();
    if (typeof document !== "undefined") {
      document.documentElement.dataset.brandPendingTeam = teamId;
    }
    return false;
  }
  if (typeof document !== "undefined") {
    delete document.documentElement.dataset.brandPendingTeam;
  }
  applyBrandBundleToDocument(cached.response, { persist: false });
  try {
    window.localStorage.setItem(BRAND_CACHE_POINTER_KEY, teamId);
  } catch {
    // Storage is optional; the current document is already correct.
  }
  return true;
}

export function syncBrandFromStorage(event: StorageEvent): boolean {
  if (
    event.key !== BRAND_CACHE_POINTER_KEY &&
    !(event.key?.startsWith(BRAND_CACHE_PREFIX) ?? false)
  ) {
    return false;
  }
  let teamId = "";
  try {
    teamId = window.localStorage.getItem(BRAND_CACHE_POINTER_KEY) ?? "";
  } catch {
    return false;
  }
  return activateCachedBrandForTeam(teamId);
}

function persistBrandBundle(response: EffectiveThemeResponse): void {
  const teamId = response.team?.id;
  if (!teamId || response.schemaVersion !== 2 || !response.brand) return;
  const cache: CachedBrandBundle = {
    version: 2,
    teamId,
    response: {
      schemaVersion: 2,
      brand: response.brand,
      effective: response.effective,
      locks: {},
      source: {},
      userConfig: { version: 1 },
      ...(response.chartStyle ? { chartStyle: response.chartStyle } : {}),
      team: response.team,
    },
  };
  try {
    window.localStorage.setItem(
      `${BRAND_CACHE_PREFIX}${teamId}`,
      JSON.stringify(cache),
    );
    window.localStorage.setItem(BRAND_CACHE_POINTER_KEY, teamId);
  } catch {
    // Private browsing/quota failures must not block applying the live bundle.
  }
}

function readCachedBrandBundle(teamId: string): CachedBrandBundle | null {
  try {
    const raw = window.localStorage.getItem(`${BRAND_CACHE_PREFIX}${teamId}`);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as unknown;
    if (!parsed || typeof parsed !== "object") return null;
    const cache = parsed as Partial<CachedBrandBundle>;
    if (cache.version !== 2 || cache.teamId !== teamId) return null;
    const response = parseEffectiveThemeResponse(cache.response);
    if (response.team?.id !== teamId) return null;
    return { version: 2, teamId, response };
  } catch {
    return null;
  }
}

function clearBrandCachePointer(): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.removeItem(BRAND_CACHE_POINTER_KEY);
  } catch {
    // Ignore unavailable storage.
  }
}

function upsertBrandTokenStyle(
  light: ColorSchemeMode,
  dark: ColorSchemeMode,
): void {
  let style = document.getElementById(
    BRAND_STYLE_ID,
  ) as HTMLStyleElement | null;
  if (style && style.tagName !== "STYLE") {
    style.remove();
    style = null;
  }
  if (!style) {
    style = document.createElement("style");
    style.id = BRAND_STYLE_ID;
    document.head.append(style);
  }
  style.textContent = [
    `:root[data-theme="light"]{${cssDeclarations(light)}}`,
    `:root[data-theme="dark"]{${cssDeclarations(dark)}}`,
  ].join("");
}

function removeBrandTokenStyle(): void {
  document.getElementById(BRAND_STYLE_ID)?.remove();
}

function updateThemeColor(
  effective: EffectiveThemeResponse["effective"],
  resolvedTheme: "light" | "dark",
): void {
  let meta = document.querySelector<HTMLMetaElement>(THEME_COLOR_SELECTOR);
  if (!meta) {
    meta = document.createElement("meta");
    meta.name = "theme-color";
    document.head.append(meta);
  }
  meta.dataset.bowheadRuntime = "true";
  meta.content =
    effective.colorScheme?.[resolvedTheme].background ??
    DEFAULT_THEME_COLORS[resolvedTheme];
}

function clearInlineBrandTokens(root: HTMLElement): void {
  for (const cssNames of Object.values(CANONICAL_THEME_CSS_TOKENS)) {
    for (const cssName of cssNames) {
      root.style.removeProperty(cssName);
    }
  }
}

function cssDeclarations(tokens: ColorSchemeMode): string {
  const declarations: string[] = [];
  for (const token of COLOR_SCHEME_TOKEN_KEYS) {
    for (const cssName of CANONICAL_THEME_CSS_TOKENS[token]) {
      declarations.push(`${cssName}:${tokens[token]}`);
    }
  }
  return declarations.join(";");
}

function notifyBrandChanged(response: EffectiveThemeResponse): void {
  for (const listener of listeners) listener();
  if (typeof window !== "undefined") {
    window.dispatchEvent(
      new CustomEvent(BRAND_BUNDLE_CHANGED_EVT, { detail: response }),
    );
  }
}
