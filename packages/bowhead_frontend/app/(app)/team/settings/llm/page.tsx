"use client";

import { useEffect } from "react";
import { useSearchParams } from "next/navigation";

import { useAppRouter } from "@/lib/use-app-router";

export default function LegacyTeamModelsRedirect() {
  const router = useAppRouter();
  const searchParams = useSearchParams();

  useEffect(() => {
    const query = searchParams.toString();
    router.replace(`/team/settings/models/${query ? `?${query}` : ""}`);
  }, [router, searchParams]);

  return <div aria-busy="true" />;
}
