"use client";

/**
 * CsvDownloadButton lists installed credit export options.
 *
 * Trigger with <a href download> (cookie-bearing same-origin GET), not fetch streaming.
 * Core exports cover summaries and users.
 */

import { useState, useRef, useEffect } from "react";
import { useTranslations } from "next-intl";

import { additionalCreditExports } from "@/edition/credit-exports";
import { csvDownloadUrl } from "@/lib/admin-credits-api";

export function CsvDownloadButton() {
  const t = useTranslations("admin.credits");
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!open) return;
    const onClick = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", onClick);
    return () => document.removeEventListener("mousedown", onClick);
  }, [open]);

  return (
    <div ref={ref} className="legacy-credits-csv">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="ui-btn legacy-credits-head-btn"
      >
        ⬇ {t("csv_download")}
      </button>
      {open && (
        <div className="legacy-credits-csv-menu">
          <DownloadItem
            href={csvDownloadUrl("summary")}
            label={t("csv_summary")}
            onClick={() => setOpen(false)}
          />
          <DownloadItem
            href={csvDownloadUrl("users")}
            label={t("csv_users")}
            onClick={() => setOpen(false)}
          />
          {additionalCreditExports().map(({ href, labelKey }) => (
            <DownloadItem key={labelKey} href={href} label={t(labelKey)} onClick={() => setOpen(false)} />
          ))}
        </div>
      )}
    </div>
  );
}

function DownloadItem({ href, label, onClick }: {
  href: string; label: string; onClick: () => void;
}) {
  return (
    <a
      href={href}
      download
      onClick={onClick}
      className="legacy-credits-csv-item"
      onMouseEnter={(e) => {
        (e.currentTarget as HTMLAnchorElement).style.background = "var(--c-surface-2)";
      }}
      onMouseLeave={(e) => {
        (e.currentTarget as HTMLAnchorElement).style.background = "transparent";
      }}
    >{label}</a>
  );
}
