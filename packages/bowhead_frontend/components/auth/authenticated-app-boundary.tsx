"use client";

import type { ReactNode } from "react";

import { InertAppFallback } from "@/components/auth/inert-app-fallback";
import type { AppChromePolicy } from "@/lib/app-route-policy";
import { useAuthSession } from "@/lib/auth/auth-session-context";

export function AuthenticatedAppBoundary({
  children,
  chrome,
}: {
  children: ReactNode;
  chrome: AppChromePolicy;
}) {
  const session = useAuthSession();

  if (session.status === "authenticated") return children;

  return (
    <InertAppFallback
      chrome={chrome}
      error={session.status === "error" ? session.error : null}
      onRetry={session.status === "error" ? session.retry : undefined}
    />
  );
}
