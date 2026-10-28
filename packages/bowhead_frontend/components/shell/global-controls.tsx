"use client";

import { useEffect, useState } from "react";

import { NotificationCenter } from "@/components/notification-center";
import { SpaceAboutButton } from "@/components/space-about";
import { SpacePicker } from "@/components/space-picker";
import { UserBadge } from "@/components/user-badge";
import { useMediaQuery } from "@/lib/use-media-query";

/**
 * Placement of application-wide right-side controls (space switcher, notification center, credit).
 *
 * At ≥1024, desktop `Topbar` mounts all three. Below 1024, AppShell uses the mobile top bar and CSS hides
 * desktop Topbar; space switching and notifications move to the mobile bar's right, credit to the session-drawer bottom (above the account card).
 * Each control owns its visibility conditions (hide credit when unauthenticated, hide bell when notification API is disabled, etc.);
 * this only decides placement: exactly one instance of each control mounts at a time, avoiding duplicate polling.
 *
 * P2 batch 4: the desktop top bar starts at 1024 (styles: `max-width: 1023.98px`). Between 768 and 1023 the sidebar is an
 * overlay, not the drawer, so the mobile top bar also carries the credits (icon only); below 768 credits stay in the drawer.
 */
export const SHELL_NARROW_QUERY = "(max-width: 1023.98px)";
export const SHELL_PHONE_QUERY = "(max-width: 767.98px)";

export type ShellControl = "space" | "notifications" | "credits";
export type ShellControlsSurface = "topbar" | "mobile-topbar" | "mobile-drawer";

export const SHELL_CONTROL_PLACEMENT: Record<
  ShellControlsSurface,
  { narrow: boolean; controls: readonly ShellControl[] }
> = {
  topbar: { narrow: false, controls: ["space", "notifications", "credits"] },
  "mobile-topbar": { narrow: true, controls: ["space", "notifications"] },
  "mobile-drawer": { narrow: true, controls: ["credits"] },
};

/**
 * Current shell layout. Null before mounting (including the static-export first frame): render none of the three positions,
 * avoiding mounting then unmounting desktop controls on narrow screens and sending an extra round of requests.
 */
export function useShellLayout(): "desktop" | "narrow" | null {
  const narrow = useMediaQuery(SHELL_NARROW_QUERY);
  const [mounted, setMounted] = useState(false);
  useEffect(() => setMounted(true), []);
  if (!mounted) return null;
  return narrow ? "narrow" : "desktop";
}

/** 768–1023, where the mobile top bar also shows the credits and the drawer is not used. */
function useTabletBand(): boolean {
  const narrow = useMediaQuery(SHELL_NARROW_QUERY);
  const phone = useMediaQuery(SHELL_PHONE_QUERY);
  return narrow && !phone;
}

function renderControl(control: ShellControl) {
  switch (control) {
    case "space":
      // workspace-datarc-decouple R6: workspace is app-global context, picked in the shell.
      return (
        <span key="space" className="shell-space-cluster">
          <SpacePicker compact />
          <SpaceAboutButton />
        </span>
      );
    case "notifications":
      return <NotificationCenter key="notifications" />;
    case "credits":
      return <UserBadge key="credits" />;
  }
}

export function ShellGlobalControls({ surface }: { surface: ShellControlsSurface }) {
  const layout = useShellLayout();
  const tablet = useTabletBand();
  const placement = SHELL_CONTROL_PLACEMENT[surface];
  if (layout === null || (layout === "narrow") !== placement.narrow) return null;
  if (tablet && surface === "mobile-topbar") {
    return <>{(["space", "notifications", "credits"] as const).map(renderControl)}</>;
  }
  if (tablet && surface === "mobile-drawer") return null;
  return <>{placement.controls.map(renderControl)}</>;
}
