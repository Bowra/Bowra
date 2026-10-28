const PUBLIC_PATHS = new Set([
  "/activate",
  "/access-required",
  "/forgot-password",
  "/invite",
  "/login",
  "/register",
  "/reset-password",
]);

export function isPublicStartupRoute(pathname: string): boolean {
  const normalized = pathname.replace(/\/+$/, "") || "/";
  return PUBLIC_PATHS.has(normalized) || normalized.startsWith("/invite/");
}

/**
 * The static export can hydrate before Next has restored its router state.  In
 * that short window usePathname() may still describe the previous page.  Public
 * startup pages must be decided from the browser URL so authenticated global
 * components are never mounted there.
 */
export function currentClientPathname(fallbackPathname: string | null): string {
  if (typeof window === "undefined") return fallbackPathname ?? "/";
  return window.location.pathname;
}
