"use client";

/**
 * file-management-v2 P1 — category badges (verbatim port of the design's CategoryBadge).
 *
 * Four categories: Upload / Deliverable / Export / Knowledge Base. Subtle differences within the same accent family
 * (fill vs outline + icon), without high-saturation colors.
 *
 * Knowledge Base has two substates: manually added (solid outline) vs automatically added (dashed outline + ✨),
 * distinguishing user-added from system-added content; deliberately subtle, without two separate labels.
 */
import clsx from "clsx";
import { useTranslations } from "next-intl";

import type { AssetClass } from "@/lib/files-taxonomy";
import {
  Library as LucideLibrary,
  Sparkle as LucideSparkle,
} from "lucide-react";
import { LucideGlyph } from "@/components/icon-refresh";

function BookIcon({ size = 11 }: { size?: number }) {
  return (
    <LucideGlyph icon={LucideLibrary} size={size} aria-hidden />
  );
}

function SparkleIcon({ size = 10 }: { size?: number }) {
  return (
    <LucideGlyph icon={LucideSparkle} size={size} aria-hidden />
  );
}

export function CategoryBadge({
  cls,
  auto = false,
  onRemove,
}: {
  cls: AssetClass;
  /** System auto-indexed (D7), not manually added by the user — meaningful only for kb category. */
  auto?: boolean;
  onRemove?: () => void;
}) {
  const t = useTranslations("files_page");

  if (cls !== "kb") {
    return <span className={clsx("cbadge", cls)}>{t(`asset_class.${cls}`)}</span>;
  }

  return (
    <span className="tip" onClick={(e) => e.stopPropagation()}>
      <span className={clsx("cbadge kb", { auto })}>
        <BookIcon />
        {t("asset_class.kb")}
        {auto ? <SparkleIcon /> : null}
      </span>
      <span className="tipbody" role="tooltip">
        {auto ? t("kb.auto_tip") : t("kb.manual_tip")}
        {onRemove ? (
          <button type="button" className="tipact" onClick={onRemove}>
            {t("kb.remove")}
          </button>
        ) : null}
      </span>
    </span>
  );
}
