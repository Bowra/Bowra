import { useTranslations } from "next-intl";

import type { ReportSection } from "@/lib/types";

type E = Extract<ReportSection, { kind: "unknown" }>;

/**
 * UnknownSection — **fallback render target** for unrecognized sections.
 *
 * Reached when an emergency image rollback leaves the frontend older than the database and it reads a
 * section kind this version does not recognize (the `sectionToPMNode` fallback in `canvas-to-pm.ts`), or when a kind
 * is retired but other deployments retain existing data.
 *
 * ⚠ This path previously fell back to `sql_block`, creating two problems:
 *   ① It disguised **any** unrecognized content as SQL / code, making readers think the report actually contained code;
 *   ② It turned an ordinary content kind into a load-bearing safety net — retiring `sql_block` would first require
 *      dismantling the protection needed precisely when something goes wrong.
 * Therefore use a **dedicated kind that business changes will never retire**.
 *
 * Design intent: **honest + recoverable**. State that the section cannot be displayed and explain its original type,
 * while preserving any extractable text verbatim (an old frontend should not lose user content during an emergency rollback).
 */
export function UnknownSection({ data }: { data: E }) {
  const t = useTranslations("unknown_section");
  const original = data.original_kind || "unknown";
  const text = (data.text_content || "").trim();
  return (
    <div className="v2-unknown-section" data-original-kind={original}>
      <div className="v2-unknown-head">
        {t.rich("head", { kind: original, code: (chunks) => <code>{chunks}</code> })}
      </div>
      {text ? <pre className="v2-unknown-body">{text}</pre> : null}
    </div>
  );
}
