"use client";

import {
  createContext,
  useContext,
  useMemo,
  useSyncExternalStore,
} from "react";

import {
  getBrandSnapshot,
  subscribeBrandSnapshot,
} from "@/lib/brand-runtime";
import {
  canonicalEffectiveThemeFallback,
  parseEffectiveThemeResponse,
  type EffectiveBrand,
  type EffectiveThemeResponse,
} from "@/lib/theme-config";

interface BrandContextValue {
  bundle: EffectiveThemeResponse;
  brand: EffectiveBrand;
}

const SERVER_FALLBACK = canonicalEffectiveThemeFallback();
const FALLBACK_BRAND = SERVER_FALLBACK.brand!;
const BrandContext = createContext<BrandContextValue>({
  bundle: SERVER_FALLBACK,
  brand: FALLBACK_BRAND,
});

export function BrandProvider({ children }: { children: React.ReactNode }) {
  const bundle = useSyncExternalStore(
    subscribeBrandSnapshot,
    getBrandSnapshot,
    () => SERVER_FALLBACK,
  );
  const brand = bundle.brand ?? FALLBACK_BRAND;
  const value = useMemo(() => ({ bundle, brand }), [brand, bundle]);
  return (
    <BrandContext.Provider value={value}>{children}</BrandContext.Provider>
  );
}

let publicBrandRaw: unknown;
let publicBrandSnapshot = SERVER_FALLBACK;

function getPublicBrandSnapshot(): EffectiveThemeResponse {
  if (typeof window === "undefined") return SERVER_FALLBACK;
  const raw = (
    window as Window & { __BOWHEAD_PUBLIC_BRAND__?: unknown }
  ).__BOWHEAD_PUBLIC_BRAND__;
  if (raw === publicBrandRaw) return publicBrandSnapshot;
  publicBrandRaw = raw;
  try {
    publicBrandSnapshot = raw
      ? parseEffectiveThemeResponse(raw)
      : SERVER_FALLBACK;
  } catch {
    publicBrandSnapshot = SERVER_FALLBACK;
  }
  return publicBrandSnapshot;
}

function subscribePublicBrand(): () => void {
  return () => undefined;
}

/**
 * Read-only public-page provider. It consumes only the Gateway-injected
 * bootstrap and never starts an authenticated request.
 */
export function PublicBrandProvider({
  children,
}: {
  children: React.ReactNode;
}) {
  const bundle = useSyncExternalStore(
    subscribePublicBrand,
    getPublicBrandSnapshot,
    () => SERVER_FALLBACK,
  );
  const brand = bundle.brand ?? FALLBACK_BRAND;
  const value = useMemo(() => ({ bundle, brand }), [brand, bundle]);
  return (
    <BrandContext.Provider value={value}>{children}</BrandContext.Provider>
  );
}

export function useBrand(): BrandContextValue {
  return useContext(BrandContext);
}
