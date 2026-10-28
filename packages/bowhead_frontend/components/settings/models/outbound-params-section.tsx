"use client";

/**
 * Model-level Outbound Parameters—design `docs/models-settings-outbound-params-design-brief-2026-08-18.md`.
 *
 * Outbound overrides are uncommon, but a provider that accepts only its default temperature rejects
 * incompatible requests. Keep this section visually quiet until the runtime applies an adaptation:
 * no colored badges while collapsed; use warm colors only when we adapted the outgoing request for the user.
 *
 * Three collapsed-heading states (quiet / N configured / N automatically adapted) and four parameter rows follow the design.
 */

import { useState } from "react";
import { useTranslations } from "next-intl";

import {
  OUTBOUND_PARAM_KEYS,
  adaptationsForModel,
  declaredOutboundParamCount,
  outboundParamModes,
  outboundParamNeedsValue,
  outboundParamsConflicts,
  type OutboundAdaptation,
  type OutboundParamKey,
  type OutboundParamMode,
  type OutboundParams,
} from "@/lib/outbound-params";

export interface OutboundParamsSectionProps {
  providerKey: string;
  modelId: string;
  protocol: string;
  params: OutboundParams;
  onChange: (next: OutboundParams) => void;
  /** Raw-JSON escape hatch (retain existing shape, adding only a conflict notice). */
  rawOverrides: Record<string, Record<string, unknown>> | null;
  /** Raw-JSON escape hatch; pass null when the protocol adapter does not consume overrides (omit the entire layer). */
  rawSlot: React.ReactNode;
  adaptations?: readonly OutboundAdaptation[];
  disabled?: boolean;
  index: number;
  /** Reuse the controls inside the single runtime-parameter disclosure. */
  embedded?: boolean;
  toolApplicable?: boolean;
}

export function OutboundParamsSection({
  providerKey,
  modelId,
  protocol,
  params,
  onChange,
  rawOverrides,
  rawSlot,
  adaptations,
  disabled,
  index,
  embedded = false,
  toolApplicable = true,
}: OutboundParamsSectionProps) {
  const t = useTranslations("settings.models.outbound");
  const [disputeOpen, setDisputeOpen] = useState<OutboundParamKey | null>(null);

  const declared = declaredOutboundParamCount(params);
  const adapted = adaptationsForModel(
    adaptations,
    providerKey,
    modelId,
    params,
  );
  const conflicts = outboundParamsConflicts(params, rawOverrides);

  function setRule(key: OutboundParamKey, mode: OutboundParamMode) {
    const next: OutboundParams = { ...params };
    if (mode === "follow") delete next[key];
    else
      next[key] = {
        mode,
        value: outboundParamNeedsValue(mode) ? params[key]?.value : undefined,
      };
    onChange(next);
  }

  function setValue(key: OutboundParamKey, raw: string) {
    const parsed = raw.trim() === "" ? undefined : Number(raw);
    const mode = params[key]?.mode ?? "follow";
    onChange({
      ...params,
      [key]: { mode, value: Number.isFinite(parsed) ? parsed : undefined },
    });
  }

  const Container = embedded ? "div" : "details";
  return (
    <Container
      className={embedded ? "mset-outbound-controls" : "mset-outbound"}
      data-testid={`outbound-params-${index}`}
    >
      {embedded ? null : (
        <summary>
          <span className="mset-outbound__title">{t("title")}</span>
          <span className="mset-outbound__sub">{t("subtitle")}</span>
          {adapted.length > 0 ? (
            <span className="mset-outbound__chip is-adapted">
              {t("adapted_count", { count: adapted.length })}
            </span>
          ) : declared > 0 ? (
            <span className="mset-outbound__chip">
              {t("declared_count", { count: declared })}
            </span>
          ) : (
            <span className="mset-outbound__quiet">{t("quiet")}</span>
          )}
        </summary>
      )}

      <div
        className="mset-outbound__table"
        role="group"
        aria-label={t("title")}
      >
        {OUTBOUND_PARAM_KEYS.map((key) => {
          if (key === "parallel_tool_calls" && !toolApplicable) return null;
          const rule = params[key];
          const mode: OutboundParamMode = rule?.mode ?? "follow";
          const adaptation = adapted.find((item) => item.param === key);
          const needsValue = outboundParamNeedsValue(mode);
          const missingValue =
            needsValue &&
            (key === "parallel_tool_calls" && !embedded
              ? typeof rule?.value !== "boolean" &&
                typeof rule?.value !== "number"
              : typeof rule?.value !==
                (key === "parallel_tool_calls" ? "boolean" : "number"));
          return (
            <div
              key={key}
              className={`mset-outbound__row${mode !== "follow" ? " is-set" : ""}${adaptation ? " is-adapted" : ""}`}
            >
              <div className="mset-outbound__name">
                {t(`param_${key}`)}
                {adaptation ? (
                  <span className="mset-outbound__flag">
                    {t("adapted_chip")}
                  </span>
                ) : null}
                <span className="mset-outbound__raw">{key}</span>
              </div>
              <div className="mset-outbound__hint">
                {t(`param_${key}_hint`)}
              </div>
              <select
                className="inp"
                aria-label={`${t(`param_${key}`)} — ${t("title")}`}
                value={mode}
                disabled={disabled}
                onChange={(event) =>
                  setRule(key, event.target.value as OutboundParamMode)
                }
              >
                {outboundParamModes(key, protocol).map((option) => (
                  <option key={option} value={option}>
                    {t(`mode_${option}`)}
                  </option>
                ))}
              </select>
              {needsValue ? (
                key === "parallel_tool_calls" ? (
                  <select
                    className="inp"
                    aria-label={`${t(`param_${key}`)} — ${t("boolean_value")}`}
                    value={
                      typeof rule?.value === "boolean" ||
                      (!embedded && typeof rule?.value === "number")
                        ? String(rule.value)
                        : ""
                    }
                    disabled={disabled}
                    onChange={(event) =>
                      onChange({
                        ...params,
                        [key]: { mode, value: event.target.value === "true" },
                      })
                    }
                  >
                    <option value="" disabled>
                      {t("boolean_choose")}
                    </option>
                    {!embedded && typeof rule?.value === "number" ? (
                      <option value={String(rule.value)}>
                        {String(rule.value)}
                      </option>
                    ) : null}
                    <option value="true">{t("boolean_on")}</option>
                    <option value="false">{t("boolean_off")}</option>
                  </select>
                ) : (
                  <input
                    className={`inp mset-outbound__value${missingValue ? " is-invalid" : ""}`}
                    type="number"
                    inputMode="decimal"
                    aria-label={`${t(`param_${key}`)} — ${t(`mode_${mode}`)}`}
                    value={typeof rule?.value === "number" ? rule.value : ""}
                    min={key === "max_tokens" ? 1 : 0}
                    step={key === "max_tokens" ? 1 : "any"}
                    disabled={disabled}
                    onChange={(event) => setValue(key, event.target.value)}
                  />
                )
              ) : (
                <span className="mset-outbound__note">
                  {mode === "omit" ? t("omit_note") : "—"}
                </span>
              )}
              {missingValue ? (
                <span className="mset-outbound__error">
                  {t("value_required")}
                </span>
              ) : null}
              {protocol === "anthropic_compatible" && key === "max_tokens" ? (
                <span className="mset-outbound__note-wide">
                  {t("anthropic_max_tokens_required")}
                </span>
              ) : null}

              {adaptation ? (
                <div className="mset-outbound__adapted" role="status">
                  <span>{t("adapted_text", { param: t(`param_${key}`) })}</span>
                  <span className="mset-outbound__since">
                    {t("adapted_since", {
                      date: adaptation.since.slice(0, 10),
                    })}
                  </span>
                  <span className="mset-outbound__acts">
                    <button
                      type="button"
                      className="btn btn-primary btn-sm"
                      disabled={disabled}
                      onClick={() => setRule(key, "omit")}
                    >
                      {t("adapted_pin")}
                    </button>
                    <button
                      type="button"
                      className="mset-outbound__dispute"
                      onClick={() =>
                        setDisputeOpen(disputeOpen === key ? null : key)
                      }
                    >
                      {t("adapted_dispute")}
                    </button>
                  </span>
                  {disputeOpen === key ? (
                    <p className="mset-outbound__dispute-body">
                      {t("adapted_dispute_body")}
                    </p>
                  ) : null}
                </div>
              ) : null}
            </div>
          );
        })}
      </div>

      {/* An empty escape hatch is worse than none: an expanded layer where nothing can change is exactly the false affordance this section addresses. */}
      {rawSlot === null ? null : (
        <details className="mset-outbound__raw-slot">
          <summary>{t("raw_title")}</summary>
          <p className="mset-outbound__hint">{t("raw_hint")}</p>
          {rawSlot}
          {conflicts.map((key) => (
            <p key={key} className="mset-outbound__conflict">
              {t("raw_conflict", { key })}
            </p>
          ))}
        </details>
      )}
    </Container>
  );
}
