"use client";

import { ToolConfirmationSurfaces } from "@/components/chat/tool-confirmation/ToolConfirmationSurfaces";

import { usePathname } from "next/navigation";

import { AuthenticatedAppChrome } from "@/components/authenticated-app-chrome";
import { AuthenticatedAppBoundary } from "@/components/auth/authenticated-app-boundary";
import { BrandMetadataController } from "@/components/brand-metadata-controller";
import { BrandProvider } from "@/components/brand-provider";
import { ToastContainer } from "@/components/chat/toast-container";
import { ConfirmDialogHost } from "@/components/confirm-dialog-host";
import { appRoutePolicy } from "@/lib/app-route-policy";
import { AuthSessionProvider } from "@/lib/auth/auth-session-context";
import { currentClientPathname } from "@/lib/public-startup-route";

export function AuthenticatedAppRoot({
  children,
}: {
  children: React.ReactNode;
}) {
  const pathname = currentClientPathname(usePathname());
  const policy = appRoutePolicy(pathname);

  if (policy.auth === "development-bypass") {
    return (
      <BrandProvider>
        <BrandMetadataController />
        {children}
        <ToastContainer />
        <ConfirmDialogHost />
      </BrandProvider>
    );
  }

  return (
    <BrandProvider>
      <AuthSessionProvider>
        <AuthenticatedAppBoundary chrome={policy.chrome}>
          <AuthenticatedAppChrome
            chrome={policy.chrome}
            pathname={pathname}
          >
            {children}
            <ToolConfirmationSurfaces />
          </AuthenticatedAppChrome>
        </AuthenticatedAppBoundary>
      </AuthSessionProvider>
    </BrandProvider>
  );
}
