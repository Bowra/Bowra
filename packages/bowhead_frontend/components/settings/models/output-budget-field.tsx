"use client";

import { ConfigurationFieldHelp } from "@/components/settings/configuration-field-help";
import { useLocale, useTranslations } from "next-intl";
import type { OutputBudgetDefaults } from "@/lib/llm-config-v2-api";
import type { OutboundParams } from "@/lib/outbound-params";
import { outputCapacity, outputPolicy } from "@/lib/output-budget";
import type { RuntimeParameters } from "@/lib/runtime-parameters";

interface Props {
  index: number;
  modelId: string;
  value?: number;
  inherited?: number;
  defaults?: OutputBudgetDefaults;
  capabilities: string[];
  params: OutboundParams;
  runtime?: RuntimeParameters;
  raw?: Record<string, Record<string, unknown>> | null;
  disabled?: boolean;
  onChange: (value: number | undefined) => void;
}

export function OutputBudgetField(props: Props) {
  const t = useTranslations("settings.models.output_budget");
  const locale = useLocale();
  const id = `mset-model-output-capacity-${props.index}`;
  const capacity = props.defaults
    ? outputCapacity(
        props.defaults,
        props.modelId,
        props.value,
        props.inherited,
      )
    : null;
  const valid =
    props.value === undefined ||
    (Number.isSafeInteger(props.value) && props.value > 0);
  const rows =
    props.defaults && capacity && valid
      ? props.capabilities
          .filter((capability) =>
            ["chat", "tool", "vision"].includes(capability),
          )
          .map((capability) => ({
            capability,
            ...outputPolicy(
              props.defaults!,
              capacity.value,
              capability,
              props.params,
              props.runtime,
              props.raw,
            ),
          }))
      : [];
  const same = rows.every(
    (row) => row.mode === rows[0]?.mode && row.value === rows[0]?.value,
  );
  return (
    <div className="medit-proto medit-setting-row mset-output-budget">
      <span className="configuration-field-label"><label className="mp-lab" htmlFor={id}>
        {t("capacity_label")}
      </label><ConfigurationFieldHelp field="team_model_config.providers.*.models.*.max_output_tokens" label={t("capacity_label")} /></span>
      <input
        id={id}
        className="inp mono"
        type="number"
        min={1}
        step={1}
        inputMode="numeric"
        name={`provider_model_output_capacity_${props.index}`}
        value={props.value ?? capacity?.value ?? ""}
        disabled={props.disabled}
        aria-invalid={!valid}
        aria-describedby={`${id}-help`}
        onChange={(event) =>
          props.onChange(
            event.target.value === "" ? undefined : Number(event.target.value),
          )
        }
      />
      <div className="mp-hint mset-output-budget__help" id={`${id}-help`}>
        {!valid ? (
          <span role="alert" className="field-error">
            {t("invalid")}
          </span>
        ) : capacity ? (
          <span>
            {t(`source_${capacity.source}`)} ·{" "}
            {t("capacity_value", {
              value: capacity.value.toLocaleString(locale),
            })}
          </span>
        ) : (
          <span>{t("unavailable")}</span>
        )}
        {props.value !== undefined ? (
          <button
            type="button"
            className="mset-runtime__text-action"
            disabled={props.disabled}
            onClick={() => props.onChange(undefined)}
          >
            {t("reset")}
          </button>
        ) : null}
        {rows
          .filter((_, index) => !same || index === 0)
          .map((row) => (
            <span key={row.capability} className="mset-output-budget__policy">
              <strong>
                {same ? t("all_capabilities") : t(`cap_${row.capability}`)}
              </strong>
              {" · "}
              <span>
                {t(`mode_${row.mode}`, {
                  value: row.value?.toLocaleString(locale) ?? "",
                })}
              </span>
            </span>
          ))}
        <span>{t("hint")}</span>
      </div>
    </div>
  );
}
