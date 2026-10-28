import "./_process-shim";
import { StrictMode, useState } from "react";
import { createRoot } from "react-dom/client";
import { NextIntlClientProvider } from "next-intl";
import { AppRouterContext } from "next/dist/shared/lib/app-router-context.shared-runtime";
import { ToastContainer } from "../components/chat/toast-container";
import { MemoryV2Content } from "../components/memory/memory-v2-content";
import { ConfirmDialog } from "../components/ui/confirm-dialog";
import { Dialog, DialogShell } from "../components/ui/dialog";
import { PromptDialog } from "../components/ui/prompt-dialog";
import { toast } from "../lib/use-toast";
import zh from "../messages/zh-CN.json";
import "../styles/ui-refresh.css";

/**
 * Real-browser fixture for behavior batch B (docs/design/组件统一-技术设计.md, “行为批 B：窄屏弹窗与撤销”): mount real
 * `Dialog` / `ConfirmDialog` / `PromptDialog` / custom `DialogShell`, real global Toast and Memory page, with the interface
 * stylesheets the app layout imports. All APIs are supplied/intercepted by the spec's page.route; no database writes.
 */
type Scene = "none" | "form" | "busy" | "guarded" | "confirm" | "prompt" | "drawer" | "custom" | "memory" | "toast";

declare global {
  interface Window {
    dialogSheetScene: { show: (scene: Scene) => void };
    dialogSheetEvents: string[];
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

function ConnectionFields() {
  return (
    <>
      <label className="settings-field">
        <span>连接名称</span>
        <input name="name" placeholder="例如：北极九章 生产" />
      </label>
      <label className="settings-field">
        <span>服务地址</span>
        <input name="url" placeholder="https://" />
      </label>
      <label className="settings-field">
        <span>访问密钥</span>
        <input name="key" placeholder="粘贴密钥" />
      </label>
      <label className="settings-field">
        <span>备注</span>
        <textarea name="note" rows={4} />
      </label>
    </>
  );
}

function FormScene({ busy = false, guarded = false }: { busy?: boolean; guarded?: boolean }) {
  const [open, setOpen] = useState(false);
  const [asking, setAsking] = useState(false);
  const close = () => {
    window.dialogSheetEvents.push("close-request");
    if (guarded) setAsking(true);
    else setOpen(false);
  };
  return (
    <>
      <button type="button" onClick={() => setOpen(true)}>添加连接</button>
      {open ? (
        <Dialog
          title="添加连接"
          onClose={close}
          busy={busy}
          footer={(
            <>
              <button type="button" className="ui-btn" onClick={close}>取消</button>
              <button type="button" className="ui-btn ui-btn--primary" onClick={() => window.dialogSheetEvents.push("save")}>保存</button>
            </>
          )}
        >
          <ConnectionFields />
        </Dialog>
      ) : null}
      {asking ? (
        <ConfirmDialog
          title="放弃未保存的内容？"
          description="关闭后已填写的内容不会保留。"
          confirmLabel="放弃"
          onCancel={() => setAsking(false)}
          onConfirm={() => {
            setAsking(false);
            setOpen(false);
          }}
        />
      ) : null}
    </>
  );
}

function ConfirmScene() {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button type="button" onClick={() => setOpen(true)}>删除报告</button>
      {open ? (
        <ConfirmDialog
          title="删除这份报告？"
          description="删除后无法恢复，报告中的 10 个区块会一起删除。"
          confirmLabel="删除报告"
          onCancel={() => setOpen(false)}
          onConfirm={() => setOpen(false)}
        />
      ) : null}
    </>
  );
}

function PromptScene() {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button type="button" onClick={() => setOpen(true)}>重命名</button>
      {open ? (
        <PromptDialog title="重命名" label="名称" initialValue="华东 Q3" confirmLabel="保存" onCancel={() => setOpen(false)} onConfirm={() => setOpen(false)} />
      ) : null}
    </>
  );
}

function DrawerScene() {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button type="button" onClick={() => setOpen(true)}>邀请成员</button>
      {open ? (
        <Dialog
          title="邀请成员"
          variant="drawer"
          width={560}
          onClose={() => setOpen(false)}
          footer={(
            <>
              <button type="button" className="ui-btn" onClick={() => setOpen(false)}>取消</button>
              <button type="button" className="ui-btn ui-btn--primary">发送邀请</button>
            </>
          )}
        >
          <label className="settings-field">
            <span>邮箱</span>
            <input name="email" placeholder="name@company.com" />
          </label>
          <label className="settings-field">
            <span>团队角色</span>
            <select name="role">
              <option>成员</option>
              <option>管理员</option>
            </select>
          </label>
        </Dialog>
      ) : null}
    </>
  );
}

/** Custom dialog (knowledge base family structure): entire panel scrolls, footer sticks to the bottom. */
function CustomScene() {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button type="button" onClick={() => setOpen(true)}>绑定文档类型</button>
      {open ? (
        <DialogShell
          onClose={() => setOpen(false)}
          label="绑定文档类型"
          backdropClassName="kb-overlay"
          panelClassName="kb-modal"
          panelStyle={{ width: 520, background: "var(--c-surface)", padding: 16 }}
          backdropStyle={{ position: "fixed", inset: 0, display: "grid", placeItems: "center", background: "var(--c-scrim)" }}
        >
          <h3>绑定文档类型</h3>
          {Array.from({ length: 8 }, (_, index) => (
            <label key={index} style={{ display: "grid", gap: 4, marginBottom: 12 }}>
              <span>字段 {index + 1}</span>
              <input name={`field-${index}`} />
            </label>
          ))}
          <div className="kb-modal-foot" style={{ display: "flex", gap: 8, justifyContent: "flex-end", paddingTop: 10 }}>
            <button type="button" onClick={() => setOpen(false)}>取消</button>
            <button type="button">绑定</button>
          </div>
        </DialogShell>
      ) : null}
    </>
  );
}

function ToastScene() {
  return (
    <button
      type="button"
      onClick={() =>
        toast({
          kind: "info",
          undo: true,
          text: "已删除“华东 Q3 下滑归因”",
          action: { label: "撤销", onAction: () => void window.dialogSheetEvents.push("undo") },
          onDismiss: () => void window.dialogSheetEvents.push("dismiss"),
        })
      }
    >
      显示撤销提示
    </button>
  );
}

function Harness() {
  const [scene, setScene] = useState<Scene>("none");
  window.dialogSheetScene = { show: setScene };
  window.dialogSheetEvents ??= [];
  return (
    <AppRouterContext.Provider value={router as never}>
      <NextIntlClientProvider locale="zh-CN" messages={zh}>
        <main style={{ padding: 16 }}>
          <h1>dialog sheet</h1>
          <p id="before">
            <button type="button">before</button>
          </p>
          {scene === "form" ? <FormScene /> : null}
          {scene === "busy" ? <FormScene busy /> : null}
          {scene === "guarded" ? <FormScene guarded /> : null}
          {scene === "confirm" ? <ConfirmScene /> : null}
          {scene === "prompt" ? <PromptScene /> : null}
          {scene === "drawer" ? <DrawerScene /> : null}
          {scene === "custom" ? <CustomScene /> : null}
          {scene === "toast" ? <ToastScene /> : null}
          {scene === "memory" ? <MemoryV2Content /> : null}
        </main>
        <ToastContainer />
      </NextIntlClientProvider>
    </AppRouterContext.Provider>
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <Harness />
  </StrictMode>,
);
