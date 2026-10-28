"use client";

import { Suspense } from "react";

import { SettingsAccessBoundary } from "@/components/settings/access-boundary";
import { FeishuIntegrationDetail } from "@/components/settings/feishu-integration-detail";
import { SettingsShell } from "@/components/settings/settings-shell";

export default function FeishuIntegrationPage() {
  return (
    <SettingsShell active="integrations" scope="team">
      <SettingsAccessBoundary tab="integrations">
        <Suspense fallback={<div aria-busy="true" />}>
          <FeishuIntegrationDetail />
        </Suspense>
      </SettingsAccessBoundary>
    </SettingsShell>
  );
}
