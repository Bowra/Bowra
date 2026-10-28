import { notFound } from "next/navigation";
import type { ReactNode } from "react";

import { developmentRoutesEnabled } from "@/lib/development-route-gate";

/**
 * Shared layout for every development-only `(app)` segment listed in
 * `DEVELOPMENT_ROUTE_PREFIXES` (currently `/spike`). Each segment's `layout.tsx`
 * re-exports this component, so any page added below it inherits the
 * production 404 without opting in.
 */
export default function DevelopmentRouteLayout({
  children,
}: {
  children: ReactNode;
}) {
  if (!developmentRoutesEnabled()) {
    notFound();
  }
  return <>{children}</>;
}
