import "./_process-shim";
import { StrictMode, useState } from "react";
import { createRoot } from "react-dom/client";
import { NextIntlClientProvider } from "next-intl";
import { SaveBar, SaveBarSlotContext } from "../components/ui/save-bar";
import zh from "../messages/zh-CN.json";
import "../app/globals.css";
import "../styles/ui-refresh.css";
import "../styles/ui-refresh-pages-settings.css";

/**
 * Real-browser save-bar fixture for page-by-page acceptance A (docs/design/组件统一-技术设计.md, “逐页验收 A”): reproduce the settings-shell structure,
 * `.settings-shell.is-settings-surface > .settings-content` (same inline styles as SettingsShell), with a save-bar slot at the end of content,
 * mounting the real SaveBar. Below 1200, the shell is single-column and the whole document scrolls, as on real pages. The spec sets the UI-refresh flag.
 */
function Page() {
  const [slot, setSlot] = useState<HTMLElement | null>(null);
  const [value, setValue] = useState("");
  return (
    <div className="settings-shell is-settings-surface">
      <section className="settings-content" style={{ padding: "28px 32px", overflow: "auto", minWidth: 0 }}>
        <div className="settings-content-inner" data-ui-width="form">
          <h1>settings savebar</h1>
          {Array.from({ length: 14 }, (_, index) => (
            <p key={index} style={{ height: 60, margin: 0 }}>
              第 {index + 1} 项
            </p>
          ))}
          <label htmlFor="last-field">最后一项</label>
          <input id="last-field" data-testid="last-field" value={value} onChange={(event) => setValue(event.target.value)} />
        </div>
        <div ref={setSlot} className="ui-savebar-slot" />
      </section>
      <SaveBarSlotContext.Provider value={slot}>
        <SaveBar dirty={value !== ""} onSave={() => undefined} onDiscard={() => setValue("")} />
      </SaveBarSlotContext.Provider>
    </div>
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <NextIntlClientProvider locale="zh-CN" messages={zh}>
      <Page />
    </NextIntlClientProvider>
  </StrictMode>,
);
