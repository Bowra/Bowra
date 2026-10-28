"use client";

import { useEffect } from "react";

import { SessionsDrawerProvider } from "@/components/sessions-drawer";
import { BrandMetadataController } from "@/components/brand-metadata-controller";
import { PersonalAppearanceProvider } from "@/components/settings/personal-appearance-provider";
import { ProductShell } from "@/components/shell/ProductShell";
import { ThemeBootstrap } from "@/components/theme-bootstrap";
import { ToastContainer } from "@/components/chat/toast-container";
import { ConfirmDialogHost } from "@/components/confirm-dialog-host";
import { TurnCompletionNotifier } from "@/components/turn-completion-notifier";
import type { AppChromePolicy } from "@/lib/app-route-policy";
import { appPath } from "@/lib/base-path";

/**
 * Authenticated application chrome. Its caller owns route classification and
 * mounts this component only after the shared session gate succeeds.
 */
export function AuthenticatedAppChrome({
  children,
  chrome,
  pathname,
}: {
  children: React.ReactNode;
  chrome: AppChromePolicy;
  pathname: string;
}) {
  useEffect(() => {
    if (
      typeof performance !== "undefined" &&
      typeof performance.mark === "function"
    ) {
      try {
        performance.mark("bowhead_shell_mounted");
      } catch {
        // Shell availability must not depend on best-effort telemetry.
      }
    }
  }, []);

  return (
    <PersonalAppearanceProvider>
      <ThemeBootstrap />
      <BrandMetadataController />
      <SessionsDrawerProvider>
        {chrome === "product-shell" ? (
          <ProductShell pathname={pathname}>{children}</ProductShell>
        ) : (
          children
        )}
        <ToastContainer />
        <TurnCompletionNotifier />
        <ConfirmDialogHost />
      </SessionsDrawerProvider>
    </PersonalAppearanceProvider>
  );
}
