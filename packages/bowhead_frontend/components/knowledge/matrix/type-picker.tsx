"use client";

/**
 * kb-matrix M3 wave-2 — eight-type column picker (design-wave2/matrix-wave2b.jsx
 * TypePicker8 verbatim). 2×4 card grid: text/numeric/money/date/question column/single-select/multi-select/
 * yes-no. Glyphs come from the design; labels from i18n `type_*` (already created in wave-1, reused here).
 * Disabled when editing a column (type cannot change after creation, §B-a).
 */

import { useTranslations } from "next-intl";

/** Order of the eight types (create allowlist; AI draft may return any of them). */
export const MATRIX_TYPES = [
  "string",
  "number",
  "currency",
  "date",
  "question",
  "select",
  "multi_select",
  "boolean",
] as const;

/** Column-header/picker glyphs. date uses line glyph ☷ to match grid-header TYPE_GLYPH (wave-2 design review
 * #3: design emoji 📅 breaks the mono line-glyph set and differs from the column header). */
const TYPE_GLYPH: Record<(typeof MATRIX_TYPES)[number], string> = {
  string: "Aa",
  number: "#",
  currency: "¥",
  date: "☷",
  question: "？",
  select: "◉",
  multi_select: "⊞",
  boolean: "⊙",
};

export function TypePicker8({
  value,
  onChange,
  disabled,
}: {
  value: string;
  onChange: (type: string) => void;
  disabled?: boolean;
}) {
  const t = useTranslations("kbMatrix");
  return (
    <div className="mx-typegrid" data-testid="kb-matrix-typepicker">
      {MATRIX_TYPES.map((id) => (
        <button
          key={id}
          type="button"
          disabled={disabled}
          className={"mx-typecard" + (value === id ? " on" : "")}
          onClick={() => !disabled && onChange(id)}
        >
          <span className="g">{TYPE_GLYPH[id]}</span>
          {t(`type_${id}` as never)}
        </button>
      ))}
    </div>
  );
}
