"use client";

import { useTranslations } from "next-intl";
import { ConfigurationFieldHelp } from "@/components/settings/configuration-field-help";
import { Field, fieldDescribedBy } from "@/components/ui/field";
import { Switch } from "@/components/ui/switch";
import { contextPolicyValid, type ContextPolicy } from "@/lib/context-policy";

interface Props {
  index: number;
  value?: ContextPolicy;
  disabled?: boolean;
  onChange: (value: ContextPolicy | undefined) => void;
}

export function ContextPolicyFields({ index, value, disabled, onChange }: Props) {
  const t = useTranslations("settings.models.context_policy");
  const id = `mset-context-policy-${index}`;
  const valid = contextPolicyValid(value);
  const enabled = value?.enabled ?? true;
  return (
    <div className="medit-proto mset-context-policy">
      <Field id={`${id}-enabled`} className="fld" label={t("enabled")} hint={enabled ? t("on_hint") : t("off_hint")}
        help={<ConfigurationFieldHelp field="team_model_config.providers.*.models.*.context_policy.enabled" label={t("enabled")} />}>
        <Switch id={`${id}-enabled`} checked={enabled} disabled={disabled}
          aria-describedby={fieldDescribedBy(`${id}-enabled`, { hint: true })}
          onCheckedChange={(checked) => onChange({ ...value, enabled: checked })} />
      </Field>
      <details className="mset-outbound">
        <summary><span className="mset-outbound__title">{t("advanced")}</span></summary>
        <div className="mset-runtime__content">
          {(["trigger_input_tokens", "keep_recent_tokens"] as const).map((field) => (
            <Field key={field} id={`${id}-${field}`} className="fld" label={t(field)}
              help={<ConfigurationFieldHelp field={`team_model_config.providers.*.models.*.context_policy.${field}`} label={t(field)} />}>
              <input id={`${id}-${field}`} className="inp mono" type="number" min={1} step={1} inputMode="numeric"
                value={value?.[field] ?? ""} placeholder={t("automatic")} disabled={disabled}
                aria-invalid={!valid} aria-describedby={!valid ? `${id}-error` : `${id}-limits`}
                onChange={(event) => onChange({ ...value, [field]: event.target.value === "" ? null : Number(event.target.value) })} />
            </Field>
          ))}
          <span id={`${id}-limits`} className="mp-hint">{t("limits_hint")}</span>
          {value ? <button type="button" className="mset-runtime__text-action" disabled={disabled} onClick={() => onChange(undefined)}>{t("reset")}</button> : null}
        </div>
      </details>
      {!valid ? <span id={`${id}-error`} className="settings-field__error" role="alert">{t("invalid")}</span> : null}
    </div>
  );
}
