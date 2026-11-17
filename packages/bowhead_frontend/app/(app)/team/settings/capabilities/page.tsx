"use client";

// Team Plugins moved to /team/settings/plugins/; this stub keeps bookmarks and earlier links working.
import { useEffect } from "react";

import { useAppRouter } from "@/lib/use-app-router";

export default function LegacyRedirect() {
  const router = useAppRouter();
  useEffect(() => router.replace(`/team/settings/plugins/${window.location.search}${window.location.hash}`), [router]);
  return null;
}
