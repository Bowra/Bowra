"use client";

// The standalone Memory page moved into Settings. Bookmarks and links stored
// before the move (notifications, Home items, emails) still open /memory/, so
// forward them to Settings > Memory with the original query and hash.
import { useEffect } from "react";

import { personalSettingsPath } from "@/lib/personal-settings-paths";
import { useAppRouter } from "@/lib/use-app-router";

export default function LegacyMemoryRedirect() {
  const router = useAppRouter();
  useEffect(() => {
    router.replace(`${personalSettingsPath("memory")}${window.location.search}${window.location.hash}`);
  }, [router]);
  return <div aria-busy="true" />;
}
