/** Register a raw page title for the single brand metadata controller. */

"use client";

import { useEffect, useId, useLayoutEffect, useRef } from "react";

import {
  registerPageTitle,
  type PageTitleRegistrationHandle,
} from "@/lib/page-title-store";

// Layout effects do nothing during the static prerender; use a passive effect there to avoid React's warning.
const useIsomorphicLayoutEffect = typeof window === "undefined" ? useEffect : useLayoutEffect;

export function useDocumentTitle(
  title: string | null | undefined,
  ownerId?: string,
): void {
  useTitleRegistration(title, ownerId, useEffect);
}

/**
 * The page's own title as the tab-title default ("My reports · Bowhead"), registered by AppShell for every
 * authenticated page. It registers in a layout effect, before the passive effects of the page's content, so a more
 * specific title registered by that content (for example the open report's canvas title) still wins.
 */
export function usePageDefaultDocumentTitle(title: string | null | undefined, ownerId: string): void {
  useTitleRegistration(title, ownerId, useIsomorphicLayoutEffect);
}

function useTitleRegistration(
  title: string | null | undefined,
  ownerId: string | undefined,
  useRegistrationEffect: typeof useEffect,
): void {
  const generatedOwnerId = useId();
  const resolvedOwnerId = ownerId ?? `use-document-title:${generatedOwnerId}`;
  const titleRef = useRef(title);
  const registrationRef = useRef<PageTitleRegistrationHandle | null>(null);
  titleRef.current = title;

  useRegistrationEffect(() => {
    const registration = registerPageTitle(
      resolvedOwnerId,
      titleRef.current,
    );
    registrationRef.current = registration;
    return () => {
      registration.unregister();
      if (registrationRef.current === registration) {
        registrationRef.current = null;
      }
    };
  }, [resolvedOwnerId]);

  useEffect(() => {
    registrationRef.current?.update(title);
  }, [title]);
}
