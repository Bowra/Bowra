"use client";

import { useEffect, useState } from "react";
import { useTranslations } from "next-intl";

import {
  BrandGraphic,
  BrandName,
  BrandedText,
} from "@/components/brand-identity";
import { useBrand } from "@/components/brand-provider";
import type { AppChromePolicy } from "@/lib/app-route-policy";

interface InertAppFallbackProps {
  chrome: AppChromePolicy;
  error?: string | null;
  onRetry?: () => void;
}

export function InertAppFallback({
  chrome,
  error = null,
  onRetry,
}: InertAppFallbackProps) {
  const t = useTranslations("app_boot");
  const { brand } = useBrand();
  const [showStatus, setShowStatus] = useState(Boolean(error));

  useEffect(() => {
    if (error) {
      setShowStatus(true);
      return;
    }
    setShowStatus(false);
    const timer = window.setTimeout(() => setShowStatus(true), 300);
    return () => window.clearTimeout(timer);
  }, [error]);

  return (
    <div
      className={`app-boot-root app-boot-root--${chrome}`}
      aria-busy={!error}
    >
      <h1 className="app-boot-sr-only" translate="no">
        {brand.applicationName}
      </h1>
      {chrome === "product-shell" ? <InertSidebar /> : null}
      <main className="app-boot-main">
        <div className="app-boot-mobile-bar" aria-hidden="true">
          <BrandGraphic
            variant="mark"
            size={28}
            className="app-boot-mark"
          />
          <BrandName className="app-boot-wordmark" />
        </div>
        <div className="app-boot-content" aria-hidden="true">
          <div className="app-boot-kicker" />
          <div className="app-boot-title" />
          <div className="app-boot-title app-boot-title--short" />
          <div className="app-boot-panel">
            <div className="app-boot-line" />
            <div className="app-boot-line app-boot-line--medium" />
            <div className="app-boot-line app-boot-line--short" />
          </div>
        </div>
        {showStatus ? (
          <div className="app-boot-status" role={error ? "alert" : "status"}>
            <span>
              <BrandedText>
                {error ? t("unavailable") : t("opening")}
              </BrandedText>
            </span>
            {error && onRetry ? (
              <button type="button" onClick={onRetry}>
                {t("retry")}
              </button>
            ) : null}
          </div>
        ) : null}
      </main>
    </div>
  );
}

function InertSidebar() {
  return (
    <aside className="app-boot-sidebar" aria-hidden="true">
      <div className="app-boot-brand">
        <BrandGraphic
          variant="mark"
          size={28}
          className="app-boot-mark"
        />
        <BrandName className="app-boot-wordmark" />
      </div>
      <div className="app-boot-new" />
      <div className="app-boot-search" />
      <div className="app-boot-nav">
        <span />
        <span />
        <span />
        <span />
      </div>
      <div className="app-boot-account" />
    </aside>
  );
}
