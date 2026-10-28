import { unprefixAppPath } from "@/lib/base-path";

export type AppChromePolicy = "product-shell" | "standalone";
export type AppAuthPolicy = "required" | "development-bypass";

export interface AppRoutePolicy {
  auth: AppAuthPolicy;
  chrome: AppChromePolicy;
}

/** Route prefixes that bypass authentication; each segment re-exports `components/development-route-layout`, which 404s in production. */
export const DEVELOPMENT_ROUTE_PREFIXES = ["/spike"] as const;
const STANDALONE_ROUTE_PREFIXES = [
  "/empty-workspace",
  "/onboarding",
] as const;

/**
 * Classifies routes owned by the authenticated `(app)` route group.
 *
 * Public startup routes are intentionally absent: their separate root layout
 * is the security boundary that keeps authenticated APIs out of first
 * activation and login.
 */
export function appRoutePolicy(pathname: string): AppRoutePolicy {
  const route = unprefixAppPath(pathname);

  if (DEVELOPMENT_ROUTE_PREFIXES.some((prefix) => matchesPrefix(route, prefix))) {
    return { auth: "development-bypass", chrome: "standalone" };
  }
  if (STANDALONE_ROUTE_PREFIXES.some((prefix) => matchesPrefix(route, prefix))) {
    return { auth: "required", chrome: "standalone" };
  }
  return { auth: "required", chrome: "product-shell" };
}

function matchesPrefix(pathname: string, prefix: string): boolean {
  return pathname === prefix || pathname.startsWith(`${prefix}/`);
}
