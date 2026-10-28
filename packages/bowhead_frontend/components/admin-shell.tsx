"use client";

/**
 * AdminShell — shared shell for non-canvas pages such as admin / memory / traces.
 *
 * Use the unified work sidebar on the left and the page's own content on the right.
 * Clicking the Sidebar brand logo returns to `/` (Canvas home), keeping users oriented.
 */

import { useTranslations } from "next-intl";

import type { SettingsTabId } from "@/lib/settings-destinations";

import { AppShell } from "./shell/AppShell";
import { Topbar } from "./shell/Topbar";

type ShellTitleKey =
  | "chat"
  | "reports"
  | "knowledge"
  | "approvals"
  | "traces"
  | "credits"
  | "access"
  | "settings";

interface Props {
  current: ShellTitleKey;
  children: React.ReactNode;
  /** Optional override for the mobile topbar title. Defaults to i18n sidebar.<current> */
  mobileTitle?: string;
  /** Keep global navigation compact when the page already has local navigation. */
  preferCompactSidebar?: boolean;
  /** Settings tab shown by the page (SettingsShell); highlights it in the sidebar's settings content. */
  settingsActiveTab?: SettingsTabId;
}

export function AdminShell({
  current,
  children,
  mobileTitle,
  preferCompactSidebar,
  settingsActiveTab,
}: Props) {
  const tNav = useTranslations("sidebar");

  // V5 mobile fix Day 3: try to render i18n nav label, fall back to current id on missing key.
  let resolvedTitle = mobileTitle ?? "";
  if (!resolvedTitle) {
    try {
      resolvedTitle = tNav(current as string);
    } catch {
      resolvedTitle = String(current);
    }
  }

  // layout-redesign P2: delegate the shell to AppShell + shared Topbar. `current` is
  // now only used to resolve the mobile title (active highlight is route-derived).
  // children get an overflow:auto wrapper to preserve aux-page whole-content scroll
  // (.v2-main itself doesn't scroll — it's head + content flex column).
  return (
    <AppShell
      mobileTitle={resolvedTitle}
      preferCompactSidebar={preferCompactSidebar}
      settingsActiveTab={settingsActiveTab}
      topbar={<Topbar titleSlot={<span className="title">{resolvedTitle}</span>} />}
    >
      <div
        style={{
          flex: 1,
          minWidth: 0,
          minHeight: 0,
          overflow: "auto",
          display: "flex",
          flexDirection: "column",
        }}
      >
        {children}
      </div>
    </AppShell>
  );
}
