"use client";

import { useId, useState } from "react";
import { RotateCcw } from "lucide-react";
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

function quantity([price, cost, fixed]: (bigint | null)[]): bigint | null {
  if (price === null || cost === null || fixed === null) return null;
  if (fixed === 0n) return 0n;
  if (price <= cost) return null;
  const margin = price - cost;
  return (fixed + margin - 1n) / margin;
}

const scale = 1_000_000n;

function decimalText(amount: bigint, decimal: string): string {
  const fraction = (amount % scale).toString().padStart(6, "0").replace(/0+$/, "");
  return (amount / scale).toString() + (fraction ? decimal + fraction : "");
}

// Use integer slider positions. Each step is a power of ten in millionths and
// every reachable amount fits the same precision contract as direct entry.
function sliderRange(anchor: bigint) {
  const step = 10n ** BigInt(Math.max(0, anchor.toString().length - 3));
  const limit = 999_999_999_999_999n / (step > scale ? step / scale : 1n);
  const upper = (2n * anchor + step - 1n) / step;
  return { step, ticks: Number(upper < limit ? upper : limit) };
}

function AmountControl({
  label,
  sliderLabel,
  currency,
  decimal,
  text,
  anchor,
  amount,
  invalid,
  statusId,
  onChange,
  onType,
}: {
  label: string;
  sliderLabel: string;
  currency?: string;
  decimal: string;
  text: string;
  anchor: bigint | null;
  amount: bigint | null;
  invalid: boolean;
  statusId: string;
  onChange: (text: string) => void;
  onType: (text: string) => void;
}) {
  const inputId = useId();
  const range = anchor && anchor > 0n ? sliderRange(anchor) : null;
  const position = range && amount !== null ? Number((amount + range.step / 2n) / range.step) : 0;
  return (
    <div className={styles.control}>
      <label htmlFor={inputId}>{label}</label>
      <div className={styles.amount}>
        <input
          id={inputId}
          type="text"
          inputMode="decimal"
          autoComplete="off"
          maxLength={64}
          aria-label={currency ? `${label} (${currency})` : label}
          aria-invalid={invalid}
          aria-describedby={invalid ? statusId : undefined}
          value={text}
          onChange={(event) => onType(event.target.value)}
        />
        {currency && <span aria-hidden="true">{currency}</span>}
      </div>
      <input
        type="range"
        className={styles.slider}
        aria-label={sliderLabel}
        min={0}
        max={range?.ticks ?? 1}
        step={1}
        value={position}
        disabled={!range || amount === null}
        aria-valuetext={text + (currency ? ` ${currency}` : "")}
        onChange={(event) => {
          if (range) onChange(decimalText(BigInt(event.target.value) * range.step, decimal));
        }}
      />
    </div>
  );
}

function BreakEvenCard({ initial }: { initial: Initial }) {
  const t = useTranslations("break_even");
  const locale = useLocale();
  const titleId = useId();
  const statusId = useId();
  const format = new Intl.NumberFormat(locale);
  const decimal = format.formatToParts(1.1).find((part) => part.type === "decimal")?.value ?? ".";
  const original = fields.map((key) => millionths(initial[key] ?? ""));
  const originalDraft = {
    unit_price: (initial.unit_price ?? "").replace(".", decimal),
    unit_variable_cost: (initial.unit_variable_cost ?? "").replace(".", decimal),
    fixed_costs: (initial.fixed_costs ?? "").replace(".", decimal),
  };
  const [draft, setDraft] = useState(originalDraft);
  const [anchors, setAnchors] = useState<Partial<Record<Field, bigint>>>({});
  const values = fields.map((key) => millionths(draft[key].trim().replace(decimal, ".")));
  const invalid = fields.filter((key, i) => draft[key].trim() && values[i] === null);
  const missing = fields.filter((_, i) => values[i] === null);
  const changed = fields.some((key) => draft[key] !== originalDraft[key]);
  const units = quantity(values);
  const baseline = quantity(original);
  let message = "";
  if (invalid.length) message = t("invalid", { fields: invalid.map((key) => t(key)).join(", ") });
  else if (missing.length) message = t("missing", { fields: missing.map((key) => t(key)).join(", ") });
  else if (units === null) message = t("unreachable");

  return (
    <section className={styles.card} aria-labelledby={titleId}>
      <div className={styles.layout}>
        <div className={styles.summary}>
          <h3 id={titleId}>{t("title")}</h3>
          <output id={statusId} aria-live="polite" aria-atomic="true">
            {units === null ? (
              <span className={styles.message}>{message}</span>
            ) : (
              <>
                <span className={styles.quantity}>
                  {t.rich("quantity", {
                    units: format.format(units),
                    count: Number(units),
                    value: (children) => <strong>{children}</strong>,
                  })}
                </span>
                {changed && baseline !== null && (
                  <span className={styles.comparison}>
                    {t("comparison", {
                      baseline: format.format(baseline),
                      delta: new Intl.NumberFormat(locale, { signDisplay: "exceptZero" }).format(units - baseline),
                    })}
                  </span>
                )}
              </>
            )}
          </output>
          <button
            type="button"
            className={styles.reset}
            disabled={!changed}
            onClick={() => {
              setDraft(originalDraft);
              setAnchors({});
            }}
          >
            <RotateCcw size={14} aria-hidden="true" />
            {t("reset")}
          </button>
        </div>
        <div className={styles.inputs}>
          {fields.map((key, index) => (
            <AmountControl
              key={key}
              label={t(key)}
              sliderLabel={t("adjust", { field: t(key) })}
              currency={initial.currency}
              decimal={decimal}
              text={draft[key]}
              anchor={anchors[key] ?? original[index]}
              amount={values[index]}
              invalid={invalid.includes(key)}
              statusId={statusId}
              onType={(text) => {
                const next = millionths(text.trim().replace(decimal, "."));
                const base = original[index];
                if (next !== null && next > 0n) {
                  const anchor = base && next <= 2n * base && next * 10n >= base ? base : next;
                  setAnchors((previous) => ({ ...previous, [key]: anchor }));
                }
                setDraft((previous) => ({ ...previous, [key]: text }));
              }}
              onChange={(text) => setDraft((previous) => ({ ...previous, [key]: text }))}
            />
          ))}
        </div>
      </div>
    </section>
  );
}
