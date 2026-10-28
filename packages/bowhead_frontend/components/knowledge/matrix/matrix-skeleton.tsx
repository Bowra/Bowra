"use client";

/**
 * kb-matrix M2 — first-open matrix skeleton (FR-27, kb-skel pattern): solid column headers, shimmering cells.
 * M2's initial snapshot fetch is the first genuinely slow scenario (review P1 adds its concrete coverage).
 */

import { useTranslations } from "next-intl";

const SKEL_ROWS = 8;
const SKEL_COLS = 5;

export function MatrixSkeleton() {
  const t = useTranslations("kbMatrix");
  const template = ["232px", ...Array.from({ length: SKEL_COLS }, () => "152px")].join(" ");
  return (
    <div className="mx-scroll" data-testid="kb-matrix-skeleton">
      <div className="mx-headrow" style={{ gridTemplateColumns: template }}>
        <div className="mx-colh freeze">
          <span className="cname" style={{ fontWeight: 600 }}>{t("col_file")}</span>
        </div>
        {Array.from({ length: SKEL_COLS }, (_, i) => (
          <div key={i} className="mx-colh">
            <span className="mx-skelbar" style={{ width: 64 + (i % 3) * 18 }} />
          </div>
        ))}
      </div>
      <div>
        {Array.from({ length: SKEL_ROWS }, (_, r) => (
          <div key={r} className="mx-vrow static" style={{ gridTemplateColumns: template, height: 44 }}>
            <div className="gcell freeze">
              <span className="mx-skelbar" style={{ width: 150 }} />
            </div>
            {Array.from({ length: SKEL_COLS }, (_, c) => (
              <div key={c} className="mx-cell gcell is-empty">
                <span className="mx-shim" style={{ width: `${46 + ((r * 7 + c * 13) % 40)}%` }} />
              </div>
            ))}
          </div>
        ))}
      </div>
    </div>
  );
}
