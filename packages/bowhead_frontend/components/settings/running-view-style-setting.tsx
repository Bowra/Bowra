"use client";

/**
 * Settings › Appearance › 运行过程显示 (chat running state R3.1, R3.2): how the chat's running view draws its work.
 *
 * Four radio cards with a still preview each. A choice saves at once to the account setting `running_view_style`,
 * the same setting the switcher on the running card writes, so a style picked there shows here, and the default
 * (列表) is the absence of a choice.
 */
import { useRef, useState, type KeyboardEvent } from "react";
import { useTranslations } from "next-intl";

import { RUNNING_VIEW_STYLES, useRunningViewStyle, type RunningViewStyle } from "@/lib/running-view-style";

import styles from "./personal-appearance-settings.module.css";

function Preview({ style }: { style: RunningViewStyle }) {
  if (style === "chips") {
    return (
      <span className="rvs-preview rvs-preview--chips" aria-hidden>
        <span><i /><i /><i /></span>
        <span><i /><i /></span>
      </span>
    );
  }
  if (style === "lanes") {
    return (
      <span className="rvs-preview rvs-preview--lanes" aria-hidden>
        <span><i /><i /><i /><i /><i /><i /></span>
        <span><i /><i /><i /></span>
      </span>
    );
  }
  if (style === "timeline") {
    return (
      <span className="rvs-preview rvs-preview--timeline" aria-hidden>
        <span><i style={{ left: "0%", width: "22%" }} /><i style={{ left: "30%", width: "38%" }} /></span>
        <span><i style={{ left: "18%", width: "54%" }} /></span>
      </span>
    );
  }
  return (
    <span className="rvs-preview rvs-preview--list" aria-hidden>
      <i style={{ width: "85%" }} />
      <i style={{ width: "70%" }} />
      <i style={{ width: "78%" }} />
    </span>
  );
}

export function RunningViewStyleSetting({ help }: { help?: React.ReactNode }) {
  const t = useTranslations("settings.appearance.runningView");
  const { style, setStyle } = useRunningViewStyle();
  const [status, setStatus] = useState<"saving" | "error" | null>(null);
  const cards = useRef<(HTMLButtonElement | null)[]>([]);

  const pick = async (next: RunningViewStyle) => {
    if (next === style) return;
    setStatus("saving");
    setStatus((await setStyle(next)) ? null : "error");
  };

  const onKeyDown = (event: KeyboardEvent<HTMLButtonElement>, index: number) => {
    const step = event.key === "ArrowRight" || event.key === "ArrowDown" ? 1 : event.key === "ArrowLeft" || event.key === "ArrowUp" ? -1 : 0;
    if (step === 0) return;
    event.preventDefault();
    const next = (index + step + RUNNING_VIEW_STYLES.length) % RUNNING_VIEW_STYLES.length;
    cards.current[next]?.focus();
    void pick(RUNNING_VIEW_STYLES[next]);
  };

  return (
    <div className={`${styles.settingRow} pa-setting-row rvs-row`} data-testid="personal-appearance-running-view">
      <div className={`${styles.settingHead} pa-setting-head`}>
        <div>
          <div className="configuration-field-label"><h3 id="rvs-title">{t("title")}</h3>{help}</div>
          <p>{t("desc")}</p>
        </div>
        {status ? (
          <span className={`${styles.fieldStatus} rvs-status`} data-state={status} aria-live="polite">
            {t(status === "saving" ? "saving" : "failed")}
          </span>
        ) : null}
      </div>
      <div role="radiogroup" aria-labelledby="rvs-title" className="rvs-cards">
        {RUNNING_VIEW_STYLES.map((option, index) => {
          const checked = option === style;
          return (
            <button
              key={option}
              ref={(node) => { cards.current[index] = node; }}
              type="button"
              role="radio"
              aria-checked={checked}
              tabIndex={checked ? 0 : -1}
              className={checked ? "rvs-card is-on" : "rvs-card"}
              onClick={() => void pick(option)}
              onKeyDown={(event) => onKeyDown(event, index)}
              data-style={option}
            >
              <Preview style={option} />
              <span className="rvs-card__name">
                <span className="rvs-card__radio" aria-hidden />
                {t(`options.${option}.name`)}
              </span>
              <span className="rvs-card__desc">{t(`options.${option}.desc`)}</span>
            </button>
          );
        })}
      </div>
      <p className="rvs-note">{t("note")}</p>
    </div>
  );
}
