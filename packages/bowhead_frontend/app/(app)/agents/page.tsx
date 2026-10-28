"use client";

import { useEffect } from "react";
import { useAppRouter } from "@/lib/use-app-router";

export default function AgentsPage() {
  const router = useAppRouter();
  useEffect(() => { router.replace("/agents/manage/hired/"); }, [router]);
  return null;
}
