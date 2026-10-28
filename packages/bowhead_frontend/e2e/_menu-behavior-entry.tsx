import "./_process-shim";
import { StrictMode, useState } from "react";
import { createRoot } from "react-dom/client";
import { NextIntlClientProvider } from "next-intl";
import { AppRouterContext } from "next/dist/shared/lib/app-router-context.shared-runtime";
import { WorkRecovery } from "../components/sidebar/work-recovery";
import { SpacePicker } from "../components/space-picker";
import { ConfirmDialog } from "../components/ui/confirm-dialog";
import { RowActionsMenu } from "../components/ui/row-actions-menu";
import { MenuFloating, useMenu, useMenuItem } from "../components/ui/menu";
import { UserMenu } from "../components/user-menu";
import type { MeUser } from "../lib/auth-api";
import type { SessionGroup } from "../lib/types";
import zh from "../messages/zh-CN.json";
import "../styles/ui-refresh.css";

/**
 * Real-browser fixture for behavior batch A (docs/design/组件统一-技术设计.md “行为批 A：菜单”): mount real components with the
 * interface stylesheets the app layout imports. Narrow-screen bottom-sheet position/grab handle, submenu positioning/safe triangle,
 * Esc and focus return require a real browser. APIs are supplied by the spec's page.route (all intercepted, no database writes).
 */
type Scene = "none" | "account" | "long-menu" | "short-menu" | "sessions" | "row-menu" | "space-picker";

declare global {
  interface Window {
    menuBehaviorScene: { show: (scene: Scene) => void };
    deletedSessions: string[];
  }
}

const router = {
  push: () => undefined,
  replace: () => undefined,
  refresh: () => undefined,
  back: () => undefined,
  forward: () => undefined,
  prefetch: () => undefined,
};

const me: MeUser = {
  id: "u_e2e",
  email: "owner@example.com",
  display_name: "Owner",
  avatar_url: null,
  team_id: "team_1",
  tier: "ultra",
  role: "user",
  team_role: "owner",
  status: "active",
  email_verified: true,
  credit_balance: 100,
  credit_quota: 100,
  locale: "zh-CN",
  timezone: "Asia/Shanghai",
  is_team_admin: true,
  workspace_count: 1,
  needs_onboarding: false,
};

const LONG_ITEMS = ["复制链接", "重命名", "移到文件夹", "置顶", "导出 PDF", "导出 Word", "分享", "查看历史"];
const SHORT_ITEMS = ["编辑", "复制", "重命名"];

function PlainMenu({ label, items }: { label: string; items: string[] }) {
  const [open, setOpen] = useState(false);
  const menu = useMenu({ open, onOpenChange: setOpen, placement: "bottom-start" });
  return (
    <>
      <button type="button" ref={menu.refs.setReference} {...menu.getReferenceProps()}>
        {label}
      </button>
      <MenuFloating menu={menu}>
        <div ref={menu.refs.setFloating} {...menu.getFloatingProps({ className: "app-floating-menu", "aria-label": label })}>
          {items.map((item) => (
            <PlainItem key={item} label={item} />
          ))}
        </div>
      </MenuFloating>
    </>
  );
}

function PlainItem({ label }: { label: string }) {
  const props = useMenuItem({ label });
  return (
    <button type="button" className="app-floating-menu-item" {...props}>
      {label}
    </button>
  );
}

const GROUPS: SessionGroup[] = [
  {
    id: "today",
    label: "TODAY",
    items: [
      { id: "s_1", title: "华东 Q3 下滑归因", intents: [], time: "2m", state: "done" },
      { id: "s_2", title: "渠道分析", intents: [], time: "just now", state: "active" },
    ],
  },
];

function Sessions() {
  return (
    <div style={{ width: 300 }}>
      {/* This fixture lacks sidebar height; its list is a very short scroll container, which Chromium scrolls to focus “⋯” when clicked,
          closing the open session menu via closeOnScroll: make the list non-scrolling. */}
      <style>{".unified-work-list{overflow:visible!important;max-height:none!important}"}</style>
      <WorkRecovery
        groups={GROUPS}
        currentSessionId={null}
        query=""
        onQueryChange={() => undefined}
        onPick={() => undefined}
        onTogglePin={() => undefined}
        archiveEnabled
        onArchive={() => "success" as const}
        onDelete={(id) => {
          window.deletedSessions.push(id);
          return "success" as const;
        }}
        onUndelete={() => undefined}
        showSearch={false}
      />
    </div>
  );
}

/** Row-end “⋯”: selecting dangerous Delete… opens a real confirmation dialog (no request; only record whether it executes). */
function RowMenuScene() {
  const [confirming, setConfirming] = useState(false);
  return (
    <>
      <RowActionsMenu
        label="季度复盘 的更多操作"
        actions={[
          { id: "edit", label: "编辑", onSelect: () => undefined },
          { id: "delete", label: "删除", danger: true, onSelect: () => setConfirming(true) },
        ]}
      />
      {confirming ? (
        <ConfirmDialog
          title="删除这份文件？"
          description="删除后无法恢复。"
          confirmLabel="删除"
          onCancel={() => setConfirming(false)}
          onConfirm={() => {
            window.deletedSessions.push("row");
            setConfirming(false);
          }}
        />
      ) : null}
    </>
  );
}

function Harness() {
  const [scene, setScene] = useState<Scene>("none");
  window.menuBehaviorScene = { show: setScene };
  window.deletedSessions ??= [];
  return (
    <AppRouterContext.Provider value={router as never}>
      <NextIntlClientProvider locale="zh-CN" messages={zh}>
        <main style={{ padding: 16 }}>
          <h1>menu behavior</h1>
          <p id="before">
            <button type="button">before</button>
          </p>
          {scene === "account" ? (
            <div style={{ width: 240 }}>
              <UserMenu user={me} showIdentity />
            </div>
          ) : null}
          {scene === "long-menu" ? <PlainMenu label="长菜单" items={LONG_ITEMS} /> : null}
          {scene === "short-menu" ? <PlainMenu label="短菜单" items={SHORT_ITEMS} /> : null}
          {scene === "sessions" ? <Sessions /> : null}
          {scene === "row-menu" ? <RowMenuScene /> : null}
          {scene === "space-picker" ? <SpacePicker compact={false} /> : null}
        </main>
      </NextIntlClientProvider>
    </AppRouterContext.Provider>
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <Harness />
  </StrictMode>,
);
