import type { ReactNode } from "react";

import { SettingsShell } from "@/components/settings/settings-shell";

/** Keeps the canonical Memory route inside the consolidated settings IA. */
export function MemoryShell({
  children,
}: {
  children: ReactNode;
}) {
  return <SettingsShell active="memory">{children}</SettingsShell>;
}
