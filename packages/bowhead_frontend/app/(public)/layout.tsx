import type { Metadata, Viewport } from "next";

import { BrandMetadataController } from "@/components/brand-metadata-controller";
import { PublicBrandProvider } from "@/components/brand-provider";
import { ClientLocaleProvider } from "@/components/client-locale-provider";
import { CANONICAL_BRAND_METADATA_BOOTSTRAP_SCRIPT } from "@/lib/brand-metadata-bootstrap-script";
import { PUBLIC_COLOR_SCHEME_BOOTSTRAP_SCRIPT } from "@/lib/public-color-scheme-bootstrap-script";
import { buildLocale, buildLocaleDescription } from "@/lib/locale";
import "../globals.css";
// 1024–1199 page scrolling for public pages only (the (app) layout starts its desktop layout at 1024).
import "../../styles/public-narrow-band.css";
import "../../styles/theta.css";
import "../../styles/report-canvas.css";
import "../../styles/kb-management.css";
import "../../styles/plan-card.css";
import "../../styles/plan-graph.css";
import "../../styles/models-settings.css";
import "../../styles/skills-settings.css";
import "../../styles/brand-identity.css";

const BUILD_LOCALE = buildLocale();

export const metadata: Metadata = {
  description: buildLocaleDescription(BUILD_LOCALE),
};

/** When the soft keyboard opens (Chrome), lay out by the visual viewport so the focused field and primary button stay above it. */
export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  interactiveWidget: "resizes-content",
};

/**
 * Public startup pages deliberately have a separate root layout.  They must
 * never load authenticated application chrome, because a first-run instance
 * has no valid session and authenticated requests redirect to login.
 */
export default function PublicLayout({ children }: { children: React.ReactNode }) {
  return (
    // suppressHydrationWarning: first-paint scripts (gateway brand injection and the color-scheme follower below)
    // write data-theme and similar attributes on <html> before hydration; this only exempts <html>'s own attributes.
    <html lang={BUILD_LOCALE} suppressHydrationWarning>
      <head>
        <script
          dangerouslySetInnerHTML={{
            __html: CANONICAL_BRAND_METADATA_BOOTSTRAP_SCRIPT,
          }}
        />
        <script
          dangerouslySetInnerHTML={{
            __html: PUBLIC_COLOR_SCHEME_BOOTSTRAP_SCRIPT,
          }}
        />
      </head>
      <body>
        <ClientLocaleProvider>
          <PublicBrandProvider>
            <BrandMetadataController whiteLabel />
            {children}
          </PublicBrandProvider>
        </ClientLocaleProvider>
      </body>
    </html>
  );
}
