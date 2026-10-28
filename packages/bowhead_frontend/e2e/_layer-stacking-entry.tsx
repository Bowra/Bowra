import "./_process-shim";
import { StrictMode, useState } from "react";
import { createRoot } from "react-dom/client";
import { NextIntlClientProvider } from "next-intl";
import { EmailSendConfirmations } from "../components/chat/email-send-confirmations";
import { ToastContainer } from "../components/chat/toast-container";
import { ConfirmDialog } from "../components/ui/confirm-dialog";
import { Dialog } from "../components/ui/dialog";
import { MenuFloating, useMenu, useMenuItem } from "../components/ui/menu";
import { toast } from "../lib/use-toast";
import zh from "../messages/zh-CN.json";
import "../styles/personal-mcp.css";

/**
 * Real-browser layer-contract fixture (docs/design/组件统一-技术设计.md “图层”): use real
 * DialogShell / Dialog / confirmation / useMenu / ToastContainer and real styles to build nested combinations.
 */
type Scene = "none" | "toast-over-confirm" | "menu-in-top-dialog" | "dialog-from-high-host" | "email-confirm-over-dialog";

declare global {
  interface Window {
    layerScene: { show: (scene: Scene) => void };
  }
}

function DemoMenu({ label }: { label: string }) {
  const [open, setOpen] = useState(false);
  const menu = useMenu({ open, onOpenChange: setOpen, placement: "bottom-start" });
  return (
    <>
      <button type="button" ref={menu.refs.setReference} {...menu.getReferenceProps()} data-testid="menu-trigger">
        {label}
      </button>
      <MenuFloating menu={menu}>
        <div
          ref={menu.refs.setFloating}
          data-testid="demo-menu"
          {...menu.getFloatingProps({ className: "app-floating-menu ws-menu", style: { width: 220 } })}
        >
          <Item>第一项</Item>
          <Item>第二项</Item>
          <Item>第三项</Item>
        </div>
      </MenuFloating>
    </>
  );
}

function Item({ children }: { children: string }) {
  const props = useMenuItem({ closeOnSelect: false });
  return <button type="button" {...props} style={{ display: "block", width: "100%", padding: 10 }}>{children}</button>;
}

function Nested() {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button type="button" data-testid="open-nested" onClick={() => setOpen(true)}>打开普通弹窗</button>
      {open ? (
        <Dialog title="普通弹窗" onClose={() => setOpen(false)}>
          <p data-testid="nested-body">从高层浮层里打开的普通弹窗</p>
        </Dialog>
      ) : null}
    </>
  );
}

function Harness() {
  const [scene, setScene] = useState<Scene>("none");
  window.layerScene = { show: setScene };
  return (
    <NextIntlClientProvider locale="zh-CN" messages={zh}>
      <main style={{ padding: 16 }}>
        <h1>layer stacking</h1>
        {scene === "toast-over-confirm" ? (
          <ConfirmDialog title="删除报告" description="删除后不可恢复" confirmLabel="删除" onCancel={() => undefined} onConfirm={() => undefined} />
        ) : null}
        {scene === "menu-in-top-dialog" ? (
          <Dialog title="顶层弹窗" layer="top" onClose={() => undefined}>
            <DemoMenu label="更多" />
          </Dialog>
        ) : null}
        {scene === "dialog-from-high-host" ? (
          // Custom high-layer overlay outside FloatingTree: elevation can only be determined from the opening point (the focused element).
          <div style={{ position: "fixed", inset: 0, zIndex: 9999, background: "rgba(0,0,0,.2)" }} data-testid="high-host">
            <Nested />
          </div>
        ) : null}
        {scene === "email-confirm-over-dialog" ? (
          <>
            {/* An ordinary dialog is already open (mounted on body), then polling receives an email confirmation. The confirmation renders in the app shell:
                on desktop, .v2-app-root is fixed and creates its own stacking context. */}
            <Dialog title="成员设置" onClose={() => undefined}>
              <p>已经打开的弹窗</p>
            </Dialog>
            <div className="v2-app-root">
              <EmailSendConfirmations />
            </div>
          </>
        ) : null}
      </main>
      <ToastContainer />
    </NextIntlClientProvider>
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <Harness />
  </StrictMode>,
);

export { toast };
(window as unknown as { layerToast: typeof toast }).layerToast = toast;
