"use client";

import { useMemo } from "react";
import { useRouter } from "next/navigation";
import type { AppRouterInstance } from "next/dist/shared/lib/app-router-context.shared-runtime";

import { appPath, unprefixAppPath } from "@/lib/base-path";

export const BEFORE_APP_NAVIGATION_EVENT = "bowhead:before-app-navigation";

/**
 * Detail of {@link BEFORE_APP_NAVIGATION_EVENT}. A guard that needs to ask the
 * user first cancels the event synchronously, then calls `proceed()` once the
 * in-app confirmation resolves; `proceed` performs the original navigation
 * without dispatching the guard event again.
 */
export interface AppNavigationDetail {
  href: string | null;
  proceed: () => void;
}

/**
 * Router wrapper for static exports deployed below a runtime public-entry prefix.
 *
 * Next's client router is built without the runtime prefix, so it must receive
 * an internal, unprefixed route.  The Gateway bootstrap patches history state
 * writes before hydration and restores the public prefix in the address bar.
 * Keeping that division of responsibility preserves the mounted app shell.
 */
export function useAppRouter(): AppRouterInstance {
  const router = useRouter();

  return useMemo(() => ({
    ...router,
    push(href: string, options?: Parameters<AppRouterInstance["push"]>[1]) {
      const go = () => {
        if (options === undefined) router.push(appNavigationHref(href));
        else router.push(appNavigationHref(href), options);
      };
      if (appNavigationAllowed(href, go)) go();
    },
    replace(href: string, options?: Parameters<AppRouterInstance["replace"]>[1]) {
      const go = () => {
        if (options === undefined) router.replace(appNavigationHref(href));
        else router.replace(appNavigationHref(href), options);
      };
      if (appNavigationAllowed(href, go)) go();
    },
    back() {
      const go = () => router.back();
      if (appNavigationAllowed(null, go)) go();
    },
  }), [router]);
}

/**
 * Ask registered guards whether an in-app navigation may run now. `proceed`
 * is how a guard resumes the same navigation after an asynchronous confirmation.
 */
export function appNavigationAllowed(href: string | null, proceed: () => void): boolean {
  if (typeof window === "undefined") return true;
  return window.dispatchEvent(
    new CustomEvent<AppNavigationDetail>(BEFORE_APP_NAVIGATION_EVENT, {
      cancelable: true,
      detail: { href, proceed },
    }),
  );
}

export function appNavigationHref(href: string): string {
  const canonicalHref = canonicalizeInternalRoute(href);
  return unprefixAppPath(canonicalHref);
}

/**
 * Build a browser-visible URL for a full-document transition.
 *
 * Public routes and authenticated routes use separate root layouts. Next must
 * hard-navigate when crossing that boundary, so callers cannot rely on the
 * history patch that normally restores the runtime public-entry prefix after
 * client navigation. Keep the prefix dynamic and canonicalize the target here.
 */
export function appDocumentHref(href: string): string {
  return appPath(appNavigationHref(href));
}

/** Navigate the current document while preserving the runtime public entry. */
export function navigateAppDocument(href: string): void {
  window.location.assign(appDocumentHref(href));
}

function canonicalizeInternalRoute(href: string): string {
  if (!href.startsWith("/") || href.startsWith("//")) return href;
  const boundary = href.search(/[?#]/);
  const pathname = boundary === -1 ? href : href.slice(0, boundary);
  if (pathname === "/" || pathname.endsWith("/")) return href;
  const suffix = boundary === -1 ? "" : href.slice(boundary);
  return `${pathname}/${suffix}`;
}
