"use client";

import type { ComponentProps } from "react";
import { appPath } from "@/lib/base-path";
import { shouldHandleAppLinkClick } from "@/lib/navigation-click";
import { useAppRouter } from "@/lib/use-app-router";

/** Preserve native modified clicks and the application's unsaved-navigation guards. */
export function CapabilityLink({ href, onClick, ...props }: ComponentProps<"a"> & { href: string }) {
  const router = useAppRouter();
  return <a {...props} href={appPath(href)} onClick={(event) => {
    onClick?.(event);
    if (!shouldHandleAppLinkClick(event)) return;
    event.preventDefault();
    router.push(href);
  }} />;
}
