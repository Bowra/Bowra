"use client";

// Concept terminology audit P1-6: team content now lives at /team/settings/workflows — this stub only preserves old links/bookmarks.
import { useEffect } from "react";

import { useAppRouter } from "@/lib/use-app-router";

export default function LegacyRedirect() {
  const router = useAppRouter();
  useEffect(() => router.replace(`/team/settings/workflows/${window.location.search}${window.location.hash}`), [router]);
  return null;
}
