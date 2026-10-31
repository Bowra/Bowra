"use client";

/**
 * file-management-v2 P1 — category badges (verbatim port of the design's CategoryBadge).
 *
 * Four categories: Upload / Deliverable / Export / Knowledge Base. Subtle differences within the same accent family
 * (fill vs outline + icon), without high-saturation colors.
 *
 * Knowledge membership is shown from the persisted file state, never classification.
 */
import clsx from "clsx";
import { useTranslations } from "next-intl";

import type { AssetClass } from "@/lib/files-taxonomy";
import {
  Library as LucideLibrary,
} from "lucide-react";
import { LucideGlyph } from "@/components/icon-refresh";

function BookIcon({ size = 11 }: { size?: number }) {
  return (
    <LucideGlyph icon={LucideLibrary} size={size} aria-hidden />
  );
}


export function CategoryBadge({
  cls,
  onRemove,
}: {
  cls: AssetClass;
  onRemove?: () => void;
}) {
  const t = useTranslations("files_page");

  if (cls !== "kb") {
    return <span className={clsx("cbadge", cls)}>{t(`asset_class.${cls}`)}</span>;
  }

  return (
    <span className="tip" onClick={(e) => e.stopPropagation()}>
      <span className="cbadge kb">
        <BookIcon />
        {t("asset_class.kb")}
      </span>
      <span className="tipbody" role="tooltip">
        {t("kb.manual_tip")}
        {onRemove ? (
          <button type="button" className="tipact" onClick={onRemove}>
            {t("kb.remove")}
          </button>
        ) : null}
      </span>
    </span>
  );
}
