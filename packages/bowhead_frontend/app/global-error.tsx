"use client";

/**
 * Root error boundary: provide a safe provider-free recovery UI even when root layout fails.
 * Must supply its own html/body without depending on next-intl, the authenticated shell or theme bootstrap.
 */
import { BoundaryErrorPage } from "@/components/boundary-error";
import { boundaryBuildLocale } from "@/lib/boundary-error-copy";

export default function GlobalError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  const locale = boundaryBuildLocale();
  // <html lang> matches error-page copy: zh-CN for Chinese builds; en-US for other builds whose copy falls back to English.
  return (
    <html lang={boundaryBuildLocale()}>
      <body style={{ margin: 0 }}>
        <BoundaryErrorPage
          error={error}
          reset={reset}
          surface="global"
          locale={locale}
        />
      </body>
    </html>
  );
}
