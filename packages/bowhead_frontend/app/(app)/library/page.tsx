"use client";

// The Library overview only duplicated its two children; the sidebar group now just expands them.
// This stub keeps old links and bookmarks working by opening Files.
import { useEffect } from "react";

import { useAppRouter } from "@/lib/use-app-router";

export default function LegacyRedirect() {
  const router = useAppRouter();
  useEffect(() => router.replace(`/files/${window.location.search}${window.location.hash}`), [router]);
  return null;
}
