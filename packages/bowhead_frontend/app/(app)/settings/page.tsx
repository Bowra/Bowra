"use client";

import { useEffect } from "react";
import { personalSettingsPath } from "@/lib/personal-settings-paths";
import { useAppRouter } from "@/lib/use-app-router";

export default function PersonalSettingsIndexRedirect() {
  const router = useAppRouter();

  useEffect(() => {
    router.replace(personalSettingsPath());
  }, [router]);

  return <div aria-busy="true" />;
}
