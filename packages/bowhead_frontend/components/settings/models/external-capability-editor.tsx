"use client";

import { useTranslations } from "next-intl";

export interface ExternalCapabilityConfig {
  models: Record<
    string,
    {
      provider?: string;
      real_id?: string;
      capability?: string;
      protocol?: string;
      [key: string]: unknown;
    }
  >;
  active: Record<string, string>;
  [key: string]: unknown;
}

export function ExternalCapabilityEditor({
  kind,
  providers,
  value,
  busy,
  onChange,
}: {
  kind: "image" | "search";
  providers: Record<string, { type?: string; display_name?: string }>;
  value?: ExternalCapabilityConfig;
  busy: boolean;
  onChange: (value: ExternalCapabilityConfig) => void;
}) {
  const t = useTranslations("settings.models");
  const config = value || { models: {}, active: {} };
  const capability = kind === "image" ? "image_gen" : "web_search";
  const protocols =
    kind === "image"
      ? ["dashscope_async", "ark_sync"]
      : ["dashscope_compat", "dashscope_native"];
  const compatibleProviders = Object.entries(providers).filter(
    ([, provider]) => provider.type === "openai_compatible",
  );
  const update = (name: string, patch: Record<string, string>) =>
    onChange({
      ...config,
      models: {
        ...config.models,
        [name]: { ...config.models[name], ...patch },
      },
    });
  return (
    <section className="block">
      <h2 className="block-title">{t("sections.external_models")}</h2>
      <p className="block-desc">{t("sections.external_models_body")}</p>
      {Object.entries(config.models).map(([name, model]) => (
        <div className="external-capability-model" key={name}>
          <div className="fld">
            <label htmlFor={`external-provider-${name}`}>
              {t("sections.external_connection")}
            </label>
            <select
              className="inp"
              id={`external-provider-${name}`}
              value={model.provider || ""}
              disabled={busy}
              onChange={(event) =>
                update(name, { provider: event.target.value })
              }
            >
              <option value="">{t("sections.select_connection")}</option>
              {compatibleProviders.map(([key, provider]) => (
                <option key={key} value={key}>
                  {provider.display_name || key}
                </option>
              ))}
            </select>
          </div>
          <div className="fld">
            <label htmlFor={`external-model-${name}`}>
              {t("sections.external_model_id")}
            </label>
            <input
              className="inp mono"
              id={`external-model-${name}`}
              value={model.real_id || ""}
              disabled={busy}
              onChange={(event) =>
                update(name, { real_id: event.target.value })
              }
            />
          </div>
          <div className="fld">
            <label htmlFor={`external-protocol-${name}`}>
              {t("sections.external_protocol")}
            </label>
            <select
              className="inp"
              id={`external-protocol-${name}`}
              value={model.protocol || protocols[0]}
              disabled={busy}
              onChange={(event) =>
                update(name, { protocol: event.target.value })
              }
            >
              {protocols.map((protocol) => (
                <option key={protocol} value={protocol}>
                  {t(`sections.protocol_${protocol}`)}
                </option>
              ))}
            </select>
          </div>
          <button
            type="button"
            className="btn ghost sm"
            disabled={busy}
            onClick={() => {
              const models = { ...config.models };
              delete models[name];
              const active = { ...config.active };
              if (active[capability] === name) delete active[capability];
              onChange({ ...config, models, active });
            }}
          >
            {t("sections.remove_external_model")}
          </button>
        </div>
      ))}
      <button
        type="button"
        className="btn sec sm"
        disabled={busy || compatibleProviders.length === 0}
        onClick={() => {
          let index = 1;
          while (config.models[`${kind}_${index}`]) index += 1;
          onChange({
            ...config,
            models: {
              ...config.models,
              [`${kind}_${index}`]: {
                provider: compatibleProviders[0][0],
                real_id: "",
                capability,
                protocol: protocols[0],
              },
            },
          });
        }}
      >
        {t("sections.add_external_model")}
      </button>
      {compatibleProviders.length === 0 ? (
        <p className="ui-group-footnote">{t("sections.external_needs_connection")}</p>
      ) : null}
    </section>
  );
}
