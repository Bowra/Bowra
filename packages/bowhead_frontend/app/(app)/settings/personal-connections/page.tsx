"use client";

// My Connections moved to /connections/; this stub keeps bookmarks and already-shared links working.
import { useEffect } from "react";

import { useAppRouter } from "@/lib/use-app-router";

export default function LegacyRedirect() {
  const router = useAppRouter();
  useEffect(() => router.replace(`/connections/${window.location.search}${window.location.hash}`), [router]);
  return null;
}
