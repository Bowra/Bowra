import "./_process-shim";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { NextIntlClientProvider } from "next-intl";
import { AppRouterContext } from "next/dist/shared/lib/app-router-context.shared-runtime";
import { UsersTable } from "../components/admin/credits/users-table";
import zh from "../messages/zh-CN.json";
import "../styles/ui-refresh.css";
import "../styles/ui-refresh-pages-admin.css";

/**
 * Real-browser fixture for page-by-page acceptance B (docs/design/组件统一-技术设计.md, “逐页验收 B”): mount the real credit users table;
 * the spec sets the UI-refresh flag on `<html>` and supplies the user list through page.route.
 */
declare global {
  interface Window {
    creditsListRowsEvents: string[];
  }
}
window.creditsListRowsEvents = [];

const router = {
  push: (href: string) => window.creditsListRowsEvents.push(`push:${href}`),
  replace: () => undefined,
  refresh: () => undefined,
  back: () => undefined,
  forward: () => undefined,
  prefetch: () => undefined,
};

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <AppRouterContext.Provider value={router as never}>
      <NextIntlClientProvider locale="zh-CN" messages={zh}>
        <main>
          <h1>credits list rows</h1>
          <UsersTable
            refreshTick={0}
            onAfterMutate={() => undefined}
            onAdjustTeamQuota={() => undefined}
            capabilities={{ credit_user_quota_write: true, credit_user_tier_write: true, credit_grant: true } as never}
          />
        </main>
      </NextIntlClientProvider>
    </AppRouterContext.Provider>
  </StrictMode>,
);
