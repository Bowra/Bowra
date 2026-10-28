/**
 * Unverified-number warning block — the **same implementation** for counting and rendering.
 *
 * SourceLinkageStrip's N Figures to Review count follows `report-trust`
 * (`unverified_numbers` in narrative **and** insight_card), while clicking uses
 * `scrollToFirstUnverified()` to find `.v2-narrative-unverified`. Both sides must share the same source:
 * every section included in that count must mount this component in its rendering path, or users see a
 * count but cannot locate the numbers in the body, and clicking does nothing.
 *
 * Observed (116 v1.1.5, cv_51ac5cfb5a5a): 2/2 unverified numbers were in insight_card ⇒
 * 0 `.canvas-report-article .v2-narrative-unverified` matches in the document ⇒ clicking “2 figures to
 * review” always left scrollTop at 0. Counting expanded to insight_card without matching rendering changes.
 *
 * The class name `v2-narrative-unverified` now serves more than narrative — it is
 * the navigation contract for `scrollToFirstUnverified()`; keep it unchanged. When adding a counted section kind,
 * also mount this component in its rendering path.
 */

"use client";

import { useTranslations } from "next-intl";
import { CharIconText } from "@/components/char-icon";

export function UnverifiedNumbersNote({ numbers }: { numbers?: string[] }) {
  const t = useTranslations("canvas");
  const list = numbers ?? [];
  if (list.length === 0) return null;
  return (
    <div
      className="v2-narrative-unverified"
      role="note"
      style={{
        marginTop: 10,
        padding: "8px 10px",
        borderLeft: "3px solid var(--c-warn)",
        background: "color-mix(in srgb, var(--c-warn) 6%, transparent)",
        fontSize: "0.75rem",
        lineHeight: 1.5,
        color: "var(--c-warn-ink)",
      }}
    >
      <span style={{ fontWeight: 600, marginRight: 6 }}><CharIconText ch="⚠">{t("unverified_numbers_warning")}</CharIconText></span>
      {list.map((n, i) => (
        <code
          key={i}
          style={{
            marginRight: 6,
            padding: "1px 5px",
            background: "color-mix(in srgb, var(--c-warn) 12%, transparent)",
            borderRadius: 3,
          }}
        >
          {n}
        </code>
      ))}
    </div>
  );
}
