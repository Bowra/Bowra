import "./_process-shim";
import { StrictMode, useState } from "react";
import { createRoot } from "react-dom/client";
import { NextIntlClientProvider } from "next-intl";
import { ExportMenu } from "../components/canvas/export-ui";
import { SettingsExport } from "../components/settings/export";
import { SpacePicker } from "../components/space-picker";
import { ChartStylePreview } from "../components/settings/chart-style-editor/chart-style-preview";
import { MenuFloating, useMenu, useMenuItem } from "../components/ui/menu";
import { chartThemeBaseForMode } from "../lib/chart-theme";
import zh from "../messages/zh-CN.json";
import "../styles/chart-style-settings.css";

/**
 * Real-browser menu call-site fixture (docs/design/组件统一-技术设计.md, Menu): mount real components; the spec's
 * page.route supplies APIs. jsdom cannot faithfully test focus, arrows or scroll clipping; lock them here instead.
 */
type Scene = "none" | "space-picker" | "export-menu" | "export-settings" | "row-menus" | "chart-preview";

declare global {
  interface Window {
    menuScene: { show: (scene: Scene) => void };
  }
}

function ExportHost() {
  const [open, setOpen] = useState(false);
  const menu = useMenu({ open, onOpenChange: setOpen, placement: "bottom-start" });
  return (
    <>
      <button type="button" ref={menu.refs.setReference} {...menu.getReferenceProps()}>
        导出
      </button>
      <ExportMenu menu={menu} busy={false} onPick={() => undefined} onOpenHistory={() => undefined} />
    </>
  );
}

function RowMenu({ row }: { row: number }) {
  const [open, setOpen] = useState(false);
  const menu = useMenu({ open, onOpenChange: setOpen, placement: "bottom-end", offset: 2, closeWhenAnchorHidden: true });
  return (
    <>
      <button type="button" ref={menu.refs.setReference} aria-label={`第 ${row} 行 更多`} {...menu.getReferenceProps()}>
        ⋯
      </button>
      <MenuFloating menu={menu}>
        <div ref={menu.refs.setFloating} {...menu.getFloatingProps({ className: "app-floating-menu", style: { width: 180 } })}>
          <RowMenuItem>编辑</RowMenuItem>
          <RowMenuItem>删除</RowMenuItem>
        </div>
      </MenuFloating>
    </>
  );
}

function RowMenuItem({ children }: { children: string }) {
  const props = useMenuItem();
  return <button type="button" {...props} style={{ display: "block", width: "100%", padding: 8 }}>{children}</button>;
}

function Harness() {
  const [scene, setScene] = useState<Scene>("none");
  window.menuScene = { show: setScene };
  return (
    <NextIntlClientProvider locale="zh-CN" messages={zh}>
      <main style={{ padding: 16 }}>
        <h1>menu callsites</h1>
        {scene === "space-picker" ? <SpacePicker compact={false} /> : null}
        {scene === "export-menu" ? <ExportHost /> : null}
        {scene === "export-settings" ? <SettingsExport /> : null}
        {scene === "row-menus" ? (
          // Same structure as credit-user and knowledge base file tables: rows inside a scrolling container, menus mounted to body.
          <div data-testid="row-scroller" style={{ height: 240, overflow: "auto", border: "1px solid" }}>
            {Array.from({ length: 30 }, (_, index) => (
              <div key={index} style={{ display: "flex", justifyContent: "space-between", padding: 8 }}>
                <span>第 {index + 1} 行</span>
                <RowMenu row={index + 1} />
              </div>
            ))}
          </div>
        ) : null}
        {scene === "chart-preview" ? <ChartStylePreview themeForMode={chartThemeBaseForMode} /> : null}
      </main>
    </NextIntlClientProvider>
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <Harness />
  </StrictMode>,
);
