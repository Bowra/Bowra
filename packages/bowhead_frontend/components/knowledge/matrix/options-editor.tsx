"use client";

/**
 * kb-matrix M3 wave-2 — select/multi_select option editor (design-wave2/
 * matrix-wave2b.jsx OptionsEditor verbatim). Tag input: Enter creates chip, Backspace
 * deletes the last item, × deletes, duplicates shake + Option Already Exists; inline errors for >64 characters/100-option cap, soft warning above 20.
 * Controlled component (parent owns value/onChange, writes create/update requests); client validation
 * mirrors server (lib/kb-matrix/options.ts), which remains authoritative (422 fallback Toast).
 *
 * Drag ordering: design included a ⠿ drag handle + Drag to Reorder, but DnD was not wired and option order has no backend
 * meaning (wave-1 made it order-independent; reorder neither bumps nor stales, only affects future extraction)—a handle that cannot drag
 * misleads, so wave-2 design review #5 removed it and changed copy to Add with Enter (honesty over decoration).
 */

import { useState } from "react";
import { useTranslations } from "next-intl";

import { OPTIONS_MAX, type OptionError, checkNewOption } from "@/lib/kb-matrix/options";

import { IcWarn, IcX } from "./matrix-icons";

export function OptionsEditor({
  value,
  onChange,
  aiValues,
}: {
  value: string[];
  onChange: (opts: string[]) => void;
  /** AI-suggested prefilled options (subset); matching items show an AI Suggested suffix. */
  aiValues?: string[];
}) {
  const t = useTranslations("kbMatrix");
  const [text, setText] = useState("");
  const [err, setErr] = useState<OptionError | "">("");
  const [dupIdx, setDupIdx] = useState(-1);
  const atLimit = value.length >= OPTIONS_MAX;
  const soft = value.length > 20 && value.length < OPTIONS_MAX;

  const add = () => {
    const check = checkNewOption(text, value);
    if (!check.ok) {
      if (check.error === "dup") {
        setErr("dup");
        setDupIdx(check.dupIndex ?? -1);
        setTimeout(() => setDupIdx(-1), 400);
      } else if (check.error) {
        setErr(check.error);
      }
      return;
    }
    onChange([...value, text.trim()]);
    setText("");
    setErr("");
  };

  const onKey = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "Enter") {
      e.preventDefault();
      add();
    } else if (e.key === "Backspace" && !text && value.length) {
      onChange(value.slice(0, -1));
    } else {
      setErr("");
    }
  };

  const errMsg =
    err === "too_long" ? t("opt_too_long") : err === "dup" ? t("opt_dup") : err === "at_limit" ? t("opt_at_limit") : "";

  return (
    <div data-testid="kb-matrix-options">
      <div className="mx-seclabel" style={{ marginTop: 12 }}>{t("opt_section")}</div>
      <div className="mx-tagbox">
        {value.map((o, i) => (
          // key=option text (wave-2 review F5): options are unique; avoid index keys assigning
          // the .dup shake to the wrong tag when deleting from the middle.
          <span key={o} className={"mx-tag" + (aiValues?.includes(o) ? " ai" : "") + (dupIdx === i ? " dup" : "")}>
            {o}
            {aiValues?.includes(o) && <span className="aitag">{t("ai_suggest_tag")}</span>}
            <button type="button" className="del" aria-label={t("close")} onClick={() => onChange(value.filter((_, j) => j !== i))}>
              <IcX s={11} />
            </button>
          </span>
        ))}
        <input
          className="mx-taginput"
          value={text}
          disabled={atLimit}
          placeholder={value.length ? "" : t("opt_placeholder")}
          onChange={(e) => {
            setText(e.target.value);
            setErr("");
          }}
          onKeyDown={onKey}
          onBlur={add}
        />
      </div>
      <div className="mx-optmeta">
        <span>{atLimit ? t("opt_at_limit") : t("opt_meta_hint")}</span>
        <span>{t("opt_count", { n: value.length })}</span>
      </div>
      {errMsg && <div className="mx-fielderr">{errMsg}</div>}
      {soft && (
        <div className="mx-softwarn">
          <span className="ic"><IcWarn s={13} /></span>
          {t("opt_soft_warn", { n: value.length })}
        </div>
      )}
    </div>
  );
}
