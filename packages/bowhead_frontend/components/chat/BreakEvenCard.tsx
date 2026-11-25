"use client";

import { useId, useState } from "react";
import { useLocale, useTranslations } from "next-intl";

import styles from "./BreakEvenCard.module.css";

const fields = ["unit_price", "unit_variable_cost", "fixed_costs"] as const;
type Field = (typeof fields)[number];
type Initial = Partial<Record<Field | "currency", string>>;

function record(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

/** Parse decimal text into integer millionths, keeping the editable text untouched. */
function millionths(text: string): bigint | null {
  if (text.length > 64 || !/^[0-9]+(?:\.[0-9]+)?$/.test(text)) return null;
  const [integer, fraction = ""] = text.split(".");
  const whole = integer.replace(/^0+/, "") || "0";
  const digits = fraction.replace(/0+$/, "");
  if (whole.length + digits.length > 15 || digits.length > 6) return null;
  return BigInt(whole + digits.padEnd(6, "0"));
}

function readInitial(payload: unknown, generation: number): Initial | null {
  if (!record(payload) || (payload.execution_generation ?? 1) !== generation) return null;
  const card = payload.answer_card;
  if (!record(card) || card.kind !== "break_even" || card.version !== 1 || !record(card.initial)) return null;
  const initial: Initial = {};
  for (const key of fields) {
    const value = card.initial[key];
    if (value === undefined) continue;
    if (typeof value !== "string" || millionths(value) === null) return null;
    initial[key] = value;
  }
  const currency = card.initial.currency;
  if (typeof currency === "string" && /^[A-Z]{3}$/.test(currency)) initial.currency = currency;
  return initial;
}

export function ChatAnswerCard({ payload, generation }: { payload: unknown; generation?: number | null }) {
  const initial = readInitial(payload, generation ?? 1);
  return initial ? <BreakEvenCard initial={initial} /> : null;
}

function BreakEvenCard({ initial }: { initial: Initial }) {
  const t = useTranslations("break_even");
  const locale = useLocale();
  const titleId = useId();
  const decimal = new Intl.NumberFormat(locale).formatToParts(1.1).find(p => p.type === "decimal")?.value ?? ".";
  const [draft, setDraft] = useState(() => ({
    unit_price: (initial.unit_price ?? "").replace(".", decimal),
    unit_variable_cost: (initial.unit_variable_cost ?? "").replace(".", decimal),
    fixed_costs: (initial.fixed_costs ?? "").replace(".", decimal),
  }));
  const values = fields.map(key => millionths(draft[key].trim().replace(decimal, ".")));
  const [price, cost, fixed] = values;
  const invalid = fields.filter((key, i) => draft[key].trim() && values[i] === null);
  let result: string;
  if (invalid.length) {
    result = t("invalid", { fields: invalid.map(key => t(key)).join(", ") });
  } else if (price === null || cost === null || fixed === null) {
    result = t("missing", { fields: fields.filter((_, i) => values[i] === null).map(key => t(key)).join(", ") });
  } else if (fixed > 0n && price <= cost) {
    result = t("unreachable");
  } else {
    const margin = price - cost;
    const units = fixed === 0n ? 0n : (fixed + margin - 1n) / margin;
    result = t("result", { units: new Intl.NumberFormat(locale).format(units) });
  }

  return (
    <section className={styles.card} aria-labelledby={titleId}>
      <h3 id={titleId}>{t("title")}</h3>
      <div className={styles.inputs}>
        {fields.map(key => (
          <label key={key}>
            <span>{t(key)}{initial.currency && ` (${initial.currency})`}</span>
            <input
              type="text"
              inputMode="decimal"
              autoComplete="off"
              maxLength={64}
              value={draft[key]}
              aria-invalid={invalid.includes(key)}
              onChange={event => setDraft({ ...draft, [key]: event.target.value })}
            />
          </label>
        ))}
      </div>
      <output aria-live="polite" aria-atomic="true">{result}</output>
    </section>
  );
}
