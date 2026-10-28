"use client";

import { Suspense } from "react";
import { useSearchParams } from "next/navigation";

import { ChartStyleEditor } from "@/components/settings/chart-style-editor/chart-style-editor";
import { TeamSettingsRoute } from "@/components/settings/team-settings-route";

/**
 * Static-export friendly: replace dynamic route /team/settings/brand-appearance/chart-styles/[styleId]
 * with /team/settings/brand-appearance/chart-styles?styleId=xxx (query string).
 * styleId is runtime data that generateStaticParams cannot enumerate at build time;
 * `output: export` would fail compilation outright (as already handled for /admin/credits/users).
 *
 * Next.js 15 + output:export requires useSearchParams() inside <Suspense> for
 * prerendering (avoiding CSR bailout). The top-level page wraps Suspense; the inner component uses the hook.
 */
export default function TeamChartStyleEditorPage() {
  return (
    <Suspense fallback={null}>
      <TeamChartStyleEditorInner />
    </Suspense>
  );
}

function TeamChartStyleEditorInner() {
  const searchParams = useSearchParams();
  const styleId = searchParams.get("styleId") ?? "";
  return (
    <TeamSettingsRoute active="team-appearance">
      <ChartStyleEditor styleId={styleId} />
    </TeamSettingsRoute>
  );
}
