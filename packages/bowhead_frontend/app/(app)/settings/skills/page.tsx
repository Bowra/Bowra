"use client";

// Skills moved to /skills/; this stub keeps already-sent notifications, Agent repair links, and bookmarks working.
import { useEffect } from "react";

import { useAppRouter } from "@/lib/use-app-router";

export default function LegacyRedirect() {
  const router = useAppRouter();
  useEffect(() => router.replace(`/skills/${window.location.search}${window.location.hash}`), [router]);
  return null;
}
