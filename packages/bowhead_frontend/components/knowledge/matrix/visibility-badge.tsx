"use client";

/**
 * "仅自己可见" (document matrix step 1, S8): the badge of a matrix only its creator can see, in the matrix list and the
 * matrix page header. Space matrices show no badge.
 */

import { useTranslations } from "next-intl";

import { Badge } from "@/components/ui/badge";
import type { MatrixVisibility } from "@/lib/kb-matrix/types";

import { IcLock } from "./matrix-icons";

export function MatrixVisibilityBadge({ visibility }: { visibility: MatrixVisibility | undefined }) {
  const t = useTranslations("kbMatrix");
  if (visibility !== "private") return null;
  return (
    <Badge tone="outline" size="sm" className="mx-visbadge" title={t("visibility_badge_private_desc")} data-testid="kb-matrix-private-badge">
      <IcLock s={12} />
      {t("visibility_badge_private")}
    </Badge>
  );
}
