"use client";

/**
 * /team/settings/tools/ — Manage tool sources (own route, wrapped in SettingsShell): the External tool sources and
 * Built-in services tabs. See components/settings/tools/tools-manager.tsx and
 * docs/sprints/tools-page-2026-11-27/product-logic.md.
 */

import { useTranslations } from "next-intl";

import { SettingsShell } from "@/components/settings/settings-shell";
import { ToolsManager } from "@/components/settings/tools/tools-manager";

export default function ToolsSettingsPage() {
  // The top bar repeats the page H1 for the large-title pattern.
  const t = useTranslations("settings.tools");
  return (
    <SettingsShell active="tools" title={t("title")}>
      <ToolsManager />
    </SettingsShell>
  );
}
