import "./_process-shim";
import { useState } from "react";
import { createRoot } from "react-dom/client";
import { NextIntlClientProvider } from "next-intl";
import { Field } from "../components/ui/field";
import { FieldRow } from "../components/ui/section-heading";
import { DialogShell } from "../components/ui/dialog";
import { ConfigurationFieldHelp } from "../components/settings/configuration-field-help";
import type { FieldContract } from "../lib/configuration-field-guidance";
import manifest from "../edition/configuration-field-translations.json";
import en from "../messages/en-US.json";
import zh from "../messages/zh-CN.json";
import de from "../messages/de-DE.json";
import fr from "../messages/fr-FR.json";
import "../app/globals.css";
import "../styles/ui-refresh.css";

const field = "team_model_retry.max_retries";
const entry = manifest.fields[field];
const contract: FieldContract = {
  contract_version: 1,
  domain: "team_model_retry",
  contract_revision: "fixture",
  schema: {
    type: "object",
    properties: {
      max_retries: {
        type: "integer",
        minimum: 0,
        maximum: 5,
        description: en.configuration_fields[entry.key as keyof typeof en.configuration_fields],
        "x-bowhead-field": { id: field, text_revision: entry.revision, unit: "count" },
      },
    },
  },
};
const locales = { "en-US": en, "zh-CN": zh, "de-DE": de, "fr-FR": fr };
const locale = (new URLSearchParams(location.search).get("locale") as keyof typeof locales) || "en-US";
function Help({ label }: { label: string }) {
  return <ConfigurationFieldHelp field={field} contract={contract} label={label} />;
}
function Page() {
  const [open, setOpen] = useState(false);
  const [value, setValue] = useState("0");
  return (
    <NextIntlClientProvider locale={locale} messages={locales[locale]}>
      <main>
        <h1>Configuration guidance</h1>
        <button onClick={() => setOpen(true)}>Open editor</button>
        <Field id="retries" label="Retries" help={<Help label="Retries" />} hint="0–5">
          <input id="retries" value={value} onChange={(event) => setValue(event.target.value)} />
        </Field>
        <output data-testid="draft">{value}</output>
        {(["top-left", "top-right", "bottom-left", "bottom-right"] as const).map((corner) => (
          <div
            key={corner}
            style={{
              position: "fixed",
              zIndex: 5,
              ...(corner.startsWith("top") ? { top: 8 } : { bottom: 8 }),
              ...(corner.endsWith("left") ? { left: 8 } : { right: 8 }),
            }}
          >
            <Help label={corner} />
          </div>
        ))}
        {open ? (
          <DialogShell
            onClose={() => setOpen(false)}
            label="Original editor"
            panelClassName="ui-dialog"
            panelStyle={{ width: "min(480px, 100vw - 32px)", padding: 16, background: "var(--c-surface)" }}
            backdropStyle={{ position: "fixed", inset: 0, display: "grid", placeItems: "center", background: "var(--c-scrim)" }}
          >
            <div style={{ transform: "translateZ(0)", isolation: "isolate", overflow: "auto", maxHeight: 240 }}>
              <FieldRow
                title="Retries"
                help={<Help label="Dialog retries" />}
                control={<input aria-label="Dialog draft" value={value} onChange={(event) => setValue(event.target.value)} />}
              />
              <p style={{ minHeight: 400 }}>Scrollable editor</p>
              <Help label="Last row" />
            </div>
            <button onClick={() => setOpen(false)}>Close editor</button>
          </DialogShell>
        ) : null}
      </main>
    </NextIntlClientProvider>
  );
}
createRoot(document.getElementById("root")!).render(<Page />);
