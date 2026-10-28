/**
 * Build-time gate for the development-only `(app)` segments listed in
 * `DEVELOPMENT_ROUTE_PREFIXES` (currently `/spike/*`).
 *
 * Those routes skip the authentication boundary (see `appRoutePolicy`) and
 * render deterministic fixtures, so they must never answer in a production
 * image. They stay available in development servers and in the Playwright e2e
 * build, which sets `NEXT_PUBLIC_BOWHEAD_E2E=1` (see `playwright.config.ts`).
 * Production images (`deploy/Dockerfile.app`) never set that flag and also
 * delete the segment sources before `next build`, so none of their chunks ship.
 */
export interface DevelopmentRouteGateEnv {
  NODE_ENV?: string;
  NEXT_PUBLIC_BOWHEAD_E2E?: string;
}

export function developmentRoutesEnabled(
  env: DevelopmentRouteGateEnv = {
    // Literal reads so Next.js inlines both values at build time.
    NODE_ENV: process.env.NODE_ENV,
    NEXT_PUBLIC_BOWHEAD_E2E: process.env.NEXT_PUBLIC_BOWHEAD_E2E,
  },
): boolean {
  return env.NODE_ENV !== "production" || env.NEXT_PUBLIC_BOWHEAD_E2E === "1";
}
