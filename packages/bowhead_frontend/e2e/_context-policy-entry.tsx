import "./_process-shim";
import { useState } from "react";
import { createRoot } from "react-dom/client";
import { NextIntlClientProvider } from "next-intl";
import { ContextPolicyFields } from "../components/settings/models/context-policy-fields";
import type { ContextPolicy } from "../lib/context-policy";
import en from "../messages/en-US.json";
import zh from "../messages/zh-CN.json";
import "../styles/models-settings.css";
import "../styles/ui-refresh-pages-settings.css";

const search = new URLSearchParams(location.search);
const locale = search.get("locale") === "zh-CN" ? "zh-CN" : "en-US";
document.documentElement.dataset.theme = search.get("theme") ?? "light";
function Editor() {
  const [value, setValue] = useState<ContextPolicy>();
  return (
    <NextIntlClientProvider locale={locale} messages={locale === "zh-CN" ? zh : en}>
      <main className="mset" style={{ maxWidth: 640, margin: "0 auto" }}>
        <div className="model-editor-connection"><div className="medit"><div className="medit-row">
          <ContextPolicyFields index={0} value={value} onChange={setValue} />
        </div></div></div>
        <output data-testid="saved-preferences" hidden>{JSON.stringify(value ?? null)}</output>
      </main>
    </NextIntlClientProvider>
  );
}
createRoot(document.getElementById("root")!).render(<Editor />);
