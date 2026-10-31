"use client";

import { ConfigurationFieldHelp } from "@/components/settings/configuration-field-help";
import { useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { ApiError } from "@/lib/api-client";
import type { OutboundParams } from "@/lib/outbound-params";
import {
  parseAdvancedJson,
  stageLegacyConversion,
  updateParameterScope,
  type ParameterPreview,
  type ParameterProfile,
  type RuntimeParameterPolicy,
  type RuntimeParameters,
} from "@/lib/runtime-parameters";
import { OutboundParamsSection } from "./outbound-params-section";

interface Props {
  index: number;
  modelId: string;
  protocol: string;
  apiSurface?: string;
  capabilities: string[];
  runtime?: RuntimeParameters;
  legacyParams: OutboundParams;
  legacyRaw: string;
  profiles: ParameterProfile[];
  disabled?: boolean;
  previewKey: string;
  onChange: (patch: {
    runtimeParameters?: RuntimeParameters;
    requestBodyOverrides?: string;
    parameterError?: string;
  }) => void;
  onPreview: (capability: string) => Promise<ParameterPreview>;
}

interface AdvancedRow {
  key: string;
  value: string;
}

export function RuntimeParametersSection(props: Props) {
  const t = useTranslations("settings.models.runtime");
  const [expanded, setExpanded] = useState(false);
  const [scope, setScope] = useState("defaults");
  const guidancePath = "team_model_config.providers.*.models.*.runtime_parameters";
  const policyPath = `${guidancePath}.${scope === "defaults" ? "defaults" : `capability_overrides.${scope}`}`;
  const [previewCapability, setPreviewCapability] = useState("");
  const [preview, setPreview] = useState<{
    key: string;
    value: ParameterPreview;
  } | null>(null);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [advancedView, setAdvancedView] = useState("fields");
  const [rowDrafts, setRowDrafts] = useState<Record<string, AdvancedRow[]>>({});
  const [jsonDrafts, setJsonDrafts] = useState<Record<string, string>>({});
  const [invalidScopes, setInvalidScopes] = useState<Record<string, boolean>>(
    {},
  );
  const [conversion, setConversion] = useState<ReturnType<
    typeof stageLegacyConversion
  > | null>(null);
  const [removeReview, setRemoveReview] = useState(false);
  const requestGeneration = useRef(0);
  const latestKey = useRef(props.previewKey);
  latestKey.current = props.previewKey;

  const capabilities = props.capabilities.filter((capability) =>
    ["chat", "tool", "vision"].includes(capability),
  );
  const selectedCapability = capabilities.includes(previewCapability)
    ? previewCapability
    : capabilities[0] || "chat";
  const runtime = props.runtime;
  const defaults = runtime?.defaults || {};
  const policy =
    scope === "defaults"
      ? defaults
      : runtime?.capability_overrides?.[scope] || {};
  const profile = props.profiles.find((item) => item.id === runtime?.profile);
  const family =
    props.protocol === "anthropic_compatible"
      ? "anthropic"
      : props.apiSurface === "responses"
        ? "openai_responses"
        : "openai";
  const availableProfiles = props.profiles.filter(
    (item) =>
      item.family === family && item.provider_types.includes(props.protocol),
  );
  const exceptions = Object.keys(runtime?.capability_overrides || {});
  const independent = capabilities.filter(
    (capability) =>
      runtime?.capability_overrides?.[capability]?.thinking_mode !== undefined,
  );
  const legacy = parseAdvancedJson(props.legacyRaw) as Record<
    string,
    Record<string, unknown>
  > | null;
  const hasLegacy =
    Boolean(legacy && Object.keys(legacy).length) ||
    Object.keys(props.legacyParams).length > 0;
  const advancedAllowed = props.protocol !== "anthropic_compatible";
  const rows =
    rowDrafts[scope] ||
    Object.entries(policy.advanced || {}).map(([key, value]) => ({
      key,
      value: JSON.stringify(value),
    }));
  const scopeLabel =
    scope === "defaults"
      ? t("defaults")
      : t("capability", { capability: t(`cap_${scope}`) });
  const id = `model-parameters-${props.index}`;
  const summary =
    defaults.thinking_mode === "off"
      ? t("summary_off", { count: exceptions.length })
      : exceptions.length
        ? t("summary_overrides", { count: exceptions.length })
        : runtime
          ? t("configured")
          : t("inherited");

  function setPolicy(next: RuntimeParameterPolicy) {
    props.onChange({
      runtimeParameters: updateParameterScope(runtime, scope, next),
    });
  }

  function setField(
    key: "thinking_mode" | "reasoning_effort" | "thinking_budget",
    value: unknown,
  ) {
    const next = { ...policy };
    if (value === "follow") delete next[key];
    else Object.assign(next, { [key]: value });
    setPolicy(next);
  }

  function changeScope(next: string) {
    requestGeneration.current += 1;
    setLoading(false);
    setScope(next);
    setRemoveReview(false);
    if (next !== "defaults") setPreviewCapability(next);
  }

  function commitAdvanced(
    raw: Record<string, unknown> | null,
    nextRows?: AdvancedRow[],
    text?: string,
  ) {
    const errors = { ...invalidScopes, [scope]: !raw };
    setInvalidScopes(errors);
    if (nextRows) setRowDrafts({ ...rowDrafts, [scope]: nextRows });
    if (text !== undefined) setJsonDrafts({ ...jsonDrafts, [scope]: text });
    props.onChange({
      ...(raw
        ? {
            runtimeParameters: updateParameterScope(runtime, scope, {
              ...policy,
              advanced: raw,
            }),
          }
        : {}),
      parameterError: Object.values(errors).some(Boolean)
        ? t("advanced_invalid")
        : undefined,
    });
  }

  function changeRows(next: AdvancedRow[]) {
    const object: Record<string, unknown> = {};
    let valid = true;
    for (const row of next) {
      if (
        !row.key.trim() ||
        row.key.includes(".") ||
        Object.hasOwn(object, row.key)
      ) {
        valid = false;
        break;
      }
      try {
        object[row.key] = JSON.parse(row.value);
      } catch {
        valid = false;
        break;
      }
    }
    commitAdvanced(valid ? object : null, next);
    const nextJson = { ...jsonDrafts };
    delete nextJson[scope];
    setJsonDrafts(nextJson);
  }

  async function refreshPreview() {
    const key = props.previewKey;
    const capability = selectedCapability;
    const generation = ++requestGeneration.current;
    setLoading(true);
    setPreviewError(null);
    try {
      const result = await props.onPreview(capability);
      if (latestKey.current !== key || requestGeneration.current !== generation)
        return;
      setPreview({ key: `${key}:${capability}`, value: result });
    } catch (error) {
      if (latestKey.current !== key || requestGeneration.current !== generation)
        return;
      setPreview(null);
      setPreviewError(
        error instanceof ApiError && error.code === "invalid_runtime_parameters"
          ? error.message
          : t("preview_failed"),
      );
    } finally {
      if (requestGeneration.current === generation) setLoading(false);
    }
  }

  const incompatible =
    (policy.thinking_mode === "off" || policy.thinking_mode === "omit") &&
    (policy.thinking_budget !== undefined ||
      (policy.reasoning_effort && policy.reasoning_effort !== "omit"));
  const stale =
    preview && preview.key !== `${props.previewKey}:${selectedCapability}`;

  return (
    <details
      className="mset-outbound mset-runtime"
      data-testid={`runtime-parameters-${props.index}`}
      onToggle={(event) => setExpanded(event.currentTarget.open)}
    >
      <summary>
        <span className="mset-outbound__title">{t("title")}</span>
        <span className="mset-runtime__summary">{summary}</span>
      </summary>
      {expanded ? (
        <div className="mset-runtime__content">
          <div className="mset-runtime__profile">
            <span className="configuration-field-label"><label htmlFor={`${id}-profile`}>{t("profile")}</label><ConfigurationFieldHelp field={`${guidancePath}.profile`} label={t("profile")} facts={{ enum: availableProfiles.map((item) => item.id) }} /></span>
            <select
              className="inp"
              id={`${id}-profile`}
              value={runtime?.profile || ""}
              disabled={props.disabled}
              onChange={(event) =>
                props.onChange({
                  runtimeParameters: updateParameterScope(
                    { ...runtime, profile: event.target.value || undefined },
                    "defaults",
                    defaults,
                  ),
                })
              }
            >
              <option value="">{t("unknown")}</option>
              {runtime?.profile &&
              !availableProfiles.some((item) => item.id === runtime.profile) ? (
                <option value={runtime.profile}>
                  {t("incompatible_profile", { profile: runtime.profile })}
                </option>
              ) : null}
              {availableProfiles.map((item) => (
                <option key={item.id} value={item.id}>
                  {t.has(`profile_${item.id}`)
                    ? t(`profile_${item.id}`)
                    : item.label}
                </option>
              ))}
            </select>
            <p className="mset-runtime__help">
              {profile ? t("profile_contract") : t("unknown_help")}
            </p>
            {runtime?.profile &&
            !availableProfiles.some((item) => item.id === runtime.profile) ? (
              <p className="mset-runtime__error" role="alert">
                {t("profile_mismatch")}
              </p>
            ) : null}
            {profile ? (
              <a
                className="mset-runtime__source"
                href={profile.source}
                target="_blank"
                rel="noopener noreferrer"
              >
                {t("profile_source")} · {profile.id}
              </a>
            ) : null}
          </div>
          <div
            className="mset-runtime__scopes"
            role="group"
            aria-label={t("edit_scope")}
          >
            <button
              type="button"
              className="btn btn-sm"
              aria-pressed={scope === "defaults"}
              onClick={() => changeScope("defaults")}
            >
              {t("defaults")}
            </button>
            <button
              type="button"
              className="btn btn-sm"
              aria-pressed={scope !== "defaults"}
              onClick={() =>
                changeScope(
                  capabilities.includes(scope)
                    ? scope
                    : capabilities.includes("tool")
                      ? "tool"
                      : capabilities[0],
                )
              }
            >
              {t("overrides", { count: exceptions.length })}
            </button>
            {scope !== "defaults" ? (
              <select
                className="inp"
                aria-label={t("override_capability")}
                value={scope}
                onChange={(event) => changeScope(event.target.value)}
              >
                {[...new Set([...capabilities, ...exceptions])].map(
                  (capability) => (
                    <option key={capability} value={capability}>
                      {capabilities.includes(capability)
                        ? t(`cap_${capability}`)
                        : `${capability} · ${t("undeclared")}`}
                    </option>
                  ),
                )}
              </select>
            ) : null}
          </div>
          <p className="mset-runtime__coverage">
            {scope === "defaults"
              ? t("coverage", {
                  capabilities: capabilities
                    .map((capability) => t(`cap_${capability}`))
                    .join(" / "),
                })
              : t("scope_inherit", {
                  capability: t.has(`cap_${scope}`) ? t(`cap_${scope}`) : scope,
                })}
          </p>
          {independent.length && scope === "defaults" ? (
            <p className="mset-runtime__help">
              {t("independent", {
                capabilities: independent
                  .map((capability) => t(`cap_${capability}`))
                  .join(" / "),
              })}{" "}
              <button
                type="button"
                className="mset-runtime__text-action"
                onClick={() => changeScope(independent[0])}
              >
                {t("view_override")}
              </button>
            </p>
          ) : null}
          <div className="mset-runtime__layout">
            <div className="mset-runtime__controls">
              {!profile &&
              (policy.thinking_mode !== undefined ||
                policy.reasoning_effort !== undefined ||
                policy.thinking_budget !== undefined) ? (
                <div className="mset-runtime__review">
                  <p className="mset-runtime__error" role="alert">
                    {t("retained_reasoning")}
                  </p>
                  <button
                    type="button"
                    className="btn btn-sm"
                    disabled={props.disabled}
                    onClick={() => {
                      const next = { ...policy };
                      delete next.thinking_mode;
                      delete next.thinking_budget;
                      delete next.reasoning_effort;
                      setPolicy(next);
                    }}
                  >
                    {t("remove_retained")}
                  </button>
                </div>
              ) : null}
              {profile ? (
                <fieldset
                  className="mset-runtime__group"
                  disabled={props.disabled}
                >
                  <legend>{t("reasoning")}</legend>
                  <span className="configuration-field-label"><label htmlFor={`${id}-thinking`}>{t("thinking")}</label><ConfigurationFieldHelp field={`${policyPath}.thinking_mode`} label={t("thinking")} facts={{ enum: profile.thinking_modes }} /></span>
                  <select
                    className="inp"
                    id={`${id}-thinking`}
                    aria-describedby={`${id}-thinking-help`}
                    value={policy.thinking_mode || "follow"}
                    onChange={(event) =>
                      setField("thinking_mode", event.target.value)
                    }
                  >
                    <option value="follow">
                      {scope === "defaults" ? t("inherit") : t("use_default")}
                    </option>
                    {policy.thinking_mode &&
                    !profile.thinking_modes.includes(policy.thinking_mode) ? (
                      <option value={policy.thinking_mode}>
                        {t(`thinking_${policy.thinking_mode}`)} ·{" "}
                        {t("unsupported")}
                      </option>
                    ) : null}
                    {profile.thinking_modes.map((mode) => (
                      <option key={mode} value={mode}>
                        {mode === "omit" ? t("omit") : t(`thinking_${mode}`)}
                      </option>
                    ))}
                  </select>
                  <p id={`${id}-thinking-help`} className="mset-runtime__help">
                    {t("thinking_help")}
                    {scope !== "defaults"
                      ? ` ${t("default_value", { value: defaults.thinking_mode ? t(`thinking_${defaults.thinking_mode}`) : t("inherit") })}`
                      : ""}
                  </p>
                  {profile.efforts.length || policy.reasoning_effort ? (
                    <>
                      <span className="configuration-field-label"><label htmlFor={`${id}-effort`}>{t("effort")}</label><ConfigurationFieldHelp field={`${policyPath}.reasoning_effort`} label={t("effort")} facts={{ enum: [...profile.efforts, "omit"] }} /></span>
                      <select
                        className="inp"
                        id={`${id}-effort`}
                        value={policy.reasoning_effort || "follow"}
                        onChange={(event) =>
                          setField("reasoning_effort", event.target.value)
                        }
                      >
                        <option value="follow">
                          {scope === "defaults"
                            ? t("inherit")
                            : t("use_default")}
                        </option>
                        <option value="omit">{t("omit")}</option>
                        {policy.reasoning_effort &&
                        policy.reasoning_effort !== "omit" &&
                        !profile.efforts.includes(policy.reasoning_effort) ? (
                          <option value={policy.reasoning_effort}>
                            {policy.reasoning_effort} · {t("unsupported")}
                          </option>
                        ) : null}
                        {profile.efforts.map((effort) => (
                          <option key={effort} value={effort}>
                            {effort}
                          </option>
                        ))}
                      </select>
                    </>
                  ) : null}
                  {profile.budget_min !== undefined ||
                  policy.thinking_budget !== undefined ? (
                    <>
                      <span className="configuration-field-label"><label htmlFor={`${id}-budget-mode`}>{t("budget")}</label><ConfigurationFieldHelp field={`${policyPath}.thinking_budget`} label={t("budget")} facts={{ minimum: profile.budget_min, maximum: profile.budget_max }} /></span>
                      <div className="mset-runtime__value-row">
                        <select
                          className="inp"
                          id={`${id}-budget-mode`}
                          value={
                            policy.thinking_budget === undefined
                              ? "follow"
                              : "fixed"
                          }
                          onChange={(event) =>
                            setField(
                              "thinking_budget",
                              event.target.value === "follow"
                                ? "follow"
                                : Number.NaN,
                            )
                          }
                        >
                          <option value="follow">
                            {scope === "defaults"
                              ? t("inherit")
                              : t("use_default")}
                          </option>
                          <option value="fixed">{t("fixed_tokens")}</option>
                        </select>
                        {policy.thinking_budget !== undefined ? (
                          <input
                            className="inp mono"
                            type="number"
                            min={profile.budget_min}
                            max={profile.budget_max}
                            step={1}
                            aria-label={t("budget_tokens")}
                            aria-describedby={`${id}-budget-help`}
                            value={
                              Number.isFinite(policy.thinking_budget)
                                ? policy.thinking_budget
                                : ""
                            }
                            onChange={(event) =>
                              setField(
                                "thinking_budget",
                                event.target.value.trim()
                                  ? Number(event.target.value)
                                  : Number.NaN,
                              )
                            }
                          />
                        ) : null}
                      </div>
                      <p
                        id={`${id}-budget-help`}
                        className="mset-runtime__help"
                      >
                        {t("budget_help", {
                          min: profile.budget_min || 0,
                          max: profile.budget_max || 0,
                        })}
                      </p>
                    </>
                  ) : null}
                  {incompatible ? (
                    <div className="mset-runtime__error" role="alert">
                      {t("incompatible_rules")}{" "}
                      <button
                        type="button"
                        className="mset-runtime__text-action"
                        onClick={() => setRemoveReview(true)}
                      >
                        {t("review_remove")}
                      </button>
                    </div>
                  ) : null}
                  {removeReview ? (
                    <div className="mset-runtime__review">
                      <p>{t("remove_review")}</p>
                      <button
                        type="button"
                        className="btn btn-sm"
                        onClick={() => {
                          const next = { ...policy };
                          delete next.thinking_budget;
                          delete next.reasoning_effort;
                          setPolicy(next);
                          setRemoveReview(false);
                        }}
                      >
                        {t("confirm_remove")}
                      </button>
                      <button
                        type="button"
                        className="btn btn-sm"
                        onClick={() => setRemoveReview(false)}
                      >
                        {t("cancel")}
                      </button>
                    </div>
                  ) : null}
                </fieldset>
              ) : null}
              <fieldset
                className="mset-runtime__group"
                disabled={props.disabled}
              >
                <legend>{t("generation")}</legend>
                <OutboundParamsSection temperatureMaximum={profile?.temperature_max} guidancePath={`${policyPath}.outbound_params`}
                  embedded
                  index={props.index}
                  providerKey=""
                  modelId={props.modelId}
                  protocol={props.protocol}
                  params={policy.outbound_params || {}}
                  onChange={(next) =>
                    setPolicy({ ...policy, outbound_params: next })
                  }
                  rawOverrides={null}
                  rawSlot={null}
                  toolApplicable={
                    scope === "tool" ||
                    (scope === "defaults" && capabilities.includes("tool"))
                  }
                  disabled={props.disabled}
                />
                <p className="mset-runtime__help">{t("baseline_help")}</p>
              </fieldset>
              {advancedAllowed ? (
                <details className="mset-runtime__advanced">
                  <summary>
                    {t("advanced")} · {scopeLabel}
                  </summary>
                  <p className="mset-runtime__help">{t("advanced_help")}<ConfigurationFieldHelp field={`${policyPath}.advanced`} label={t("advanced")} /></p>
                  <div
                    className="mset-runtime__scopes"
                    role="group"
                    aria-label={t("advanced_view")}
                  >
                    <button
                      type="button"
                      className="btn btn-sm"
                      aria-pressed={advancedView === "fields"}
                      onClick={() => {
                        if (!invalidScopes[scope]) {
                          setAdvancedView("fields");
                          const next = { ...rowDrafts };
                          delete next[scope];
                          setRowDrafts(next);
                        }
                      }}
                    >
                      {t("fields")}
                    </button>
                    <button
                      type="button"
                      className="btn btn-sm"
                      aria-pressed={advancedView === "json"}
                      onClick={() => {
                        if (!invalidScopes[scope]) setAdvancedView("json");
                      }}
                    >
                      {t("json")}
                    </button>
                  </div>
                  {advancedView === "json" ? (
                    <textarea
                      className="inp mono mset-runtime__json"
                      aria-label={t("advanced_json")}
                      rows={5}
                      disabled={props.disabled}
                      spellCheck={false}
                      value={
                        jsonDrafts[scope] ??
                        JSON.stringify(policy.advanced || {}, null, 2)
                      }
                      onChange={(event) =>
                        commitAdvanced(
                          parseAdvancedJson(event.target.value),
                          undefined,
                          event.target.value,
                        )
                      }
                    />
                  ) : (
                    <div className="mset-runtime__fields">
                      {rows.map((row, index) => (
                        <div className="mset-runtime__advanced-row" key={index}>
                          <input
                            className="inp mono"
                            aria-label={t("field_name", { index: index + 1 })}
                            placeholder={t("field_name_placeholder")}
                            value={row.key}
                            disabled={props.disabled}
                            onChange={(event) =>
                              changeRows(
                                rows.map((item, rowIndex) =>
                                  rowIndex === index
                                    ? { ...item, key: event.target.value }
                                    : item,
                                ),
                              )
                            }
                          />
                          <input
                            className="inp mono"
                            aria-label={t("field_value", { index: index + 1 })}
                            placeholder={t("field_value_placeholder")}
                            value={row.value}
                            disabled={props.disabled}
                            onChange={(event) =>
                              changeRows(
                                rows.map((item, rowIndex) =>
                                  rowIndex === index
                                    ? { ...item, value: event.target.value }
                                    : item,
                                ),
                              )
                            }
                          />
                          <button
                            type="button"
                            className="btn btn-sm"
                            aria-label={t("remove_field", { index: index + 1 })}
                            disabled={props.disabled}
                            onClick={() =>
                              changeRows(
                                rows.filter(
                                  (_, rowIndex) => rowIndex !== index,
                                ),
                              )
                            }
                          >
                            {t("remove")}
                          </button>
                        </div>
                      ))}
                      <button
                        type="button"
                        className="btn btn-sm"
                        disabled={props.disabled}
                        onClick={() =>
                          changeRows([...rows, { key: "", value: "" }])
                        }
                      >
                        {t("add_field")}
                      </button>
                    </div>
                  )}
                  {invalidScopes[scope] ? (
                    <p className="mset-runtime__error" role="alert">
                      {t("advanced_invalid")}
                    </p>
                  ) : null}
                  <p className="mset-runtime__help">{t("unverified")}</p>
                </details>
              ) : (
                <p className="mset-runtime__help">
                  {t("advanced_unavailable")}
                </p>
              )}
              {hasLegacy ? (
                <details className="mset-runtime__advanced">
                  <summary>{t("legacy")}</summary>
                  <p className="mset-runtime__help">{t("legacy_help")}</p>
                  <textarea
                    className="inp mono mset-runtime__json"
                    aria-label={t("legacy_json")}
                    rows={4}
                    disabled={props.disabled || !advancedAllowed}
                    value={props.legacyRaw}
                    onChange={(event) =>
                      props.onChange({
                        requestBodyOverrides: event.target.value,
                      })
                    }
                  />
                  <button
                    type="button"
                    className="btn btn-sm"
                    disabled={props.disabled || !legacy}
                    onClick={() =>
                      legacy &&
                      setConversion(
                        stageLegacyConversion(
                          runtime,
                          legacy,
                          props.legacyParams,
                          profile,
                        ),
                      )
                    }
                  >
                    {t("review_conversion")}
                  </button>
                  {Object.keys(props.legacyParams).length ? (
                    <pre className="mset-runtime__wire">
                      {JSON.stringify(props.legacyParams, null, 2)}
                    </pre>
                  ) : null}
                  {conversion ? (
                    <div className="mset-runtime__review">
                      <p>{t("conversion_scope")}</p>
                      <ul>
                        {conversion.converted.map((field) => (
                          <li key={field}>
                            <code>{field}</code>
                          </li>
                        ))}
                      </ul>
                      <pre className="mset-runtime__wire">
                        {JSON.stringify(
                          { before: { capability_overrides: runtime?.capability_overrides || {}, legacy: legacy }, after: { capability_overrides: conversion.runtime?.capability_overrides || {}, legacy: conversion.raw } },
                          null,
                          2,
                        )}
                      </pre>
                      <button
                        type="button"
                        className="btn btn-sm"
                        disabled={
                          props.disabled || !conversion.converted.length
                        }
                        onClick={() => {
                          props.onChange({
                            runtimeParameters: conversion.runtime,
                            requestBodyOverrides: JSON.stringify(
                              conversion.raw,
                              null,
                              2,
                            ),
                          });
                          setConversion(null);
                        }}
                      >
                        {t("confirm_conversion")}
                      </button>
                      <button
                        type="button"
                        className="btn btn-sm"
                        onClick={() => setConversion(null)}
                      >
                        {t("cancel")}
                      </button>
                    </div>
                  ) : null}
                </details>
              ) : null}
            </div>
            <aside className="mset-runtime__preview" aria-label={t("preview")}>
              <h4>{t("preview")}</h4>
              <p className="mset-runtime__help">{t("draft")}</p>
              <label htmlFor={`${id}-preview-cap`}>
                {t("preview_capability")}
              </label>
              <select
                className="inp"
                id={`${id}-preview-cap`}
                value={selectedCapability}
                onChange={(event) => {
                  requestGeneration.current += 1;
                  setLoading(false);
                  setPreviewCapability(event.target.value);
                }}
              >
                {capabilities.map((capability) => (
                  <option key={capability} value={capability}>
                    {t(`cap_${capability}`)}
                  </option>
                ))}
              </select>
              <p className="mset-runtime__help">{t("preview_help")}</p>
              <button
                type="button"
                className="btn btn-sm"
                disabled={
                  props.disabled ||
                  loading ||
                  Object.values(invalidScopes).some(Boolean)
                }
                onClick={() => void refreshPreview()}
              >
                {loading ? t("compiling") : t("refresh")}
              </button>
              {stale ? (
                <p className="mset-runtime__help" role="status">
                  {t("outdated")}
                </p>
              ) : null}
              {previewError ? (
                <p className="mset-runtime__error" role="alert">
                  {previewError}
                </p>
              ) : null}
              {preview && !stale ? (
                <>
                  <dl className="mset-runtime__projection">
                    {preview.value.parameters.map((row, index) => (
                      <div key={`${row.field}-${index}`}>
                        <dt>
                          <code>{row.field}</code>
                        </dt>
                        <dd>
                          {row.redacted
                            ? t("redacted")
                            : row.omitted
                              ? t("not_sent")
                              : JSON.stringify(row.value)}
                          <span>
                            {t.has(`source_${row.source}`)
                              ? t(`source_${row.source}`)
                              : row.source}
                            {row.variable ? ` · ${t("varies")}` : ""}
                          </span>
                        </dd>
                      </div>
                    ))}
                  </dl>
                  <details>
                    <summary>{t("wire_details")}</summary>
                    <pre className="mset-runtime__wire">
                      {JSON.stringify(preview.value, null, 2)}
                    </pre>
                  </details>
                </>
              ) : null}
            </aside>
          </div>
        </div>
      ) : null}
    </details>
  );
}
