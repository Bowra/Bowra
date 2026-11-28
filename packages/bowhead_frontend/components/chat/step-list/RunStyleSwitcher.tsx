"use client";

/**
 * The four-way style switcher on the running card and the expanded trace (chat running state R3.2, R3.4).
 *
 * A keyboard radio group: Tab reaches the chosen style, arrow keys move and pick. Picking switches the view at once
 * and saves the account setting, so it stays the reader's default in every conversation, browser and device. When the
 * save fails the view switches back and a notice says why, with a way to Appearance settings.
 */
import { useRef, type KeyboardEvent } from "react";
import { useTranslations } from "next-intl";

import { appPath } from "@/lib/base-path";
import { personalSettingsPath } from "@/lib/personal-settings-paths";
import { RUNNING_VIEW_STYLES, useRunningViewStyle, type RunningViewStyle } from "@/lib/running-view-style";
import { toast } from "@/lib/use-toast";

function StyleIcon({ style }: { style: RunningViewStyle }) {
  const common = { width: 15, height: 15, viewBox: "0 0 24 24", "aria-hidden": true } as const;
  if (style === "chips") {
    return (
      <svg {...common} fill="none" stroke="currentColor" strokeWidth={2}>
        <rect x="2" y="5" width="8" height="5" rx="2.5" />
        <rect x="12" y="5" width="10" height="5" rx="2.5" />
        <rect x="2" y="14" width="12" height="5" rx="2.5" />
      </svg>
    );
  }
  if (style === "lanes") {
    return (
      <svg {...common} fill="currentColor">
        <circle cx="4" cy="7" r="2" />
        <circle cx="10" cy="7" r="2" />
        <circle cx="16" cy="7" r="2" />
        <circle cx="4" cy="17" r="2" />
        <circle cx="10" cy="17" r="2" />
      </svg>
    );
  }
  if (style === "timeline") {
    return (
      <svg {...common} fill="currentColor">
        <rect x="2" y="5" width="12" height="4" rx="1" />
        <rect x="15" y="5" width="7" height="4" rx="1" />
        <rect x="6" y="15" width="9" height="4" rx="1" />
      </svg>
    );
  }
  return (
    <svg {...common} fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round">
      <path d="M8 6h13" />
      <path d="M8 12h13" />
      <path d="M8 18h13" />
      <path d="M3 6h.01" />
      <path d="M3 12h.01" />
      <path d="M3 18h.01" />
    </svg>
  );
}

export function RunStyleSwitcher({ className }: { className?: string }) {
  const t = useTranslations("step_list");
  const { style, setStyle } = useRunningViewStyle();
  const buttons = useRef<(HTMLButtonElement | null)[]>([]);

  const pick = async (next: RunningViewStyle) => {
    if (next === style) return;
    const saved = await setStyle(next);
    if (saved) return;
    toast({
      kind: "error",
      text: t("style_save_failed", { style: t(`style_name.${next}`) }),
      // The card lives in chat and trace views that may render outside the app router; a page load is enough here.
      action: { label: t("style_open_settings"), onAction: () => window.location.assign(appPath(personalSettingsPath("appearance"))) },
    });
  };

  const onKeyDown = (event: KeyboardEvent<HTMLButtonElement>, index: number) => {
    const step = event.key === "ArrowRight" || event.key === "ArrowDown" ? 1 : event.key === "ArrowLeft" || event.key === "ArrowUp" ? -1 : 0;
    if (step === 0) return;
    event.preventDefault();
    const next = (index + step + RUNNING_VIEW_STYLES.length) % RUNNING_VIEW_STYLES.length;
    buttons.current[next]?.focus();
    void pick(RUNNING_VIEW_STYLES[next]);
  };

  return (
    <div role="radiogroup" aria-label={t("style_switcher")} className={className ? `bh-style-switch ${className}` : "bh-style-switch"} data-testid="run-style-switcher">
      {RUNNING_VIEW_STYLES.map((option, index) => {
        const name = t(`style_name.${option}`);
        const checked = option === style;
        return (
          <button
            key={option}
            ref={(node) => { buttons.current[index] = node; }}
            type="button"
            role="radio"
            aria-checked={checked}
            aria-label={name}
            title={name}
            tabIndex={checked ? 0 : -1}
            className={checked ? "bh-style-switch__opt is-on" : "bh-style-switch__opt"}
            onClick={() => void pick(option)}
            onKeyDown={(event) => onKeyDown(event, index)}
            data-style={option}
          >
            <StyleIcon style={option} />
          </button>
        );
      })}
    </div>
  );
}
