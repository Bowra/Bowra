import type { Metadata } from "next";

import { AuthenticatedAppRoot } from "@/components/auth/authenticated-app-root";
import { ClientLocaleProvider } from "@/components/client-locale-provider";
import { BRAND_BOOTSTRAP_SCRIPT } from "@/lib/brand-bootstrap-script";
import { CANONICAL_BRAND_METADATA_BOOTSTRAP_SCRIPT } from "@/lib/brand-metadata-bootstrap-script";
import { buildLocale, buildLocaleDescription } from "@/lib/locale";
import { fontVariables } from "../_fonts";
import "../globals.css";
import "../../styles/theta.css";
import "../../styles/report-canvas.css";
import "../../styles/save-to-memory.css";
import "../../styles/kb-management.css";
import "../../styles/kb-nav.css";
import "../../styles/plan-card.css";
import "../../styles/plan-graph.css";
import "../../styles/models-settings.css";
import "../../styles/skills-settings.css";
import "../../styles/config-copilot.css";
import "../../styles/chart-style-settings.css";
import "../../styles/brand-identity.css";
import "@/edition/global-styles.css";
import "../../styles/agent-marketplace.css";
import "../../styles/agent-charts.css";
import "../../styles/agent-dashboard.css";
import "../../styles/personal-mcp.css";
import "../../styles/access-request.css";
// Built-in Agent settings: scoped list and form layout; its :root:root overrides win by specificity,
// so it stays ahead of the refresh sheets that must be imported last.
import "../../styles/builtin-agent-settings.css";
// Interface styles: legacy inline-style classes first, then the refreshed appearance, imported last.
import "../../styles/ui-controls-legacy.css";
import "../../styles/ui-refresh.css";
// Appearance batch 5 (strongly styled/inline overlays, navigation drawer, autocomplete, business badges) follows batches 1–4.
import "../../styles/ui-refresh-surfaces.css";
// Page acceptance A (personal/team/space settings): grouped cards, left labels/right controls, Save bar, selected filters, skeletons, page mappings; import last.
import "../../styles/ui-refresh-pages-settings.css";
// Page acceptance B: administration and shared mobile list rows, primary function pages; import after A.
import "../../styles/ui-refresh-pages-admin.css";
import "../../styles/ui-refresh-pages-main.css";
// Chat Workbench/report canvas batch 3: canvas appearance (toolbar, editing/rewrite bars, chapter actions, citation/source overlays, details/history/chart frames).
import "../../styles/ui-refresh-canvas.css";
// Chat Workbench/report canvas batch 2: Workbench appearance (run strip, steps, message actions, card shells, homepage/result controls); import last.
import "../../styles/ui-refresh-workbench.css";
// P2 batch 1: three material/divider levels, material accent text, solid persistent sidebar; adjust prior backgrounds/blur, import last.
import "../../styles/ui-refresh-materials.css";
// P2 batch 3: sidebar and topbar visuals and icon size tiers; restyles objects from the files above, imported last.
import "../../styles/ui-refresh-chrome.css";
// P2 batch 6: content type scale (chat answers, report body, Help articles) and brand-once; restyles content from all files above, imported last.
import "../../styles/ui-refresh-content.css";
// P2 batch 5: knowledge base in-page tabs, scope filter and dialog interiors; restyles objects from the files above, imported last.
import "../../styles/ui-refresh-kb.css";
// First-level page header, tabs and content column (Header, Language & Paging Spec §2); restyles the page heads above.
import "../../styles/ui-refresh-page-frame.css";
// Chat cards: one card shell for the conversation stream (components/ui/card.tsx); restyles chat objects above, imported last.
import "../../styles/ui-refresh-cards.css";

// Build-time locale: shared with ClientLocaleProvider, <html lang>, and metadata.
const BUILD_LOCALE = buildLocale();
const IS_E2E = process.env.NEXT_PUBLIC_BOWHEAD_E2E === "1";

export const metadata: Metadata = {
  description: buildLocaleDescription(BUILD_LOCALE),
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  // i18n: client mode (compatible with NEXT_STATIC_EXPORT=1).
  // See components/client-locale-provider.tsx comment explaining why SSR cookies() cannot be used.
  return (
    <html
      lang={BUILD_LOCALE}
      className={IS_E2E ? undefined : fontVariables}
      suppressHydrationWarning
    >
      {/* suppressHydrationWarning ↑: the pre-hydration <script> below mutates
          documentElement's data-theme/-mode/-color-profile/... before React hydrates
          (avoids a first-paint theme flash). Those attrs aren't in this SSR markup, so
          React would log a hydration attribute mismatch on <html>. Suppression is scoped
          to this element's own attributes only — exactly the theme attrs. */}
      <head>
        <script
          dangerouslySetInnerHTML={{
            __html: CANONICAL_BRAND_METADATA_BOOTSTRAP_SCRIPT,
          }}
        />
        {/* V5 mobile sprint: viewport-fit=cover enables safe-area-inset-* env() (iOS notch);
            interactive-widget=resizes-content (Chrome 108+) resizes viewport for iOS keyboard instead of covering
            sticky composer. Older unsupported Safari falls back to padding-bottom env(). */}
        <meta
          name="viewport"
          content="width=device-width, initial-scale=1, viewport-fit=cover, interactive-widget=resizes-content"
        />
        <meta
          name="theme-color"
          content="#F4F7FB"
          data-bowhead-runtime="true"
        />
        {/* Team-scoped validated brand cache: color tokens + identity are applied
            before hydration so a returning member does not flash Bowhead defaults. */}
        <script dangerouslySetInnerHTML={{ __html: BRAND_BOOTSTRAP_SCRIPT }} />
        {/* W11-2 + theme config: synchronously set theme attributes before React hydration to avoid white first-frame flash. */}
        <script
          dangerouslySetInnerHTML={{
            __html: `(function(){try{var r=document.documentElement,c=null;try{var b=window.__BOWHEAD_PREHYDRATED_BRAND__;c=b&&b.effective?b.effective:JSON.parse(localStorage.getItem('bowhead.theme_config')||'null')}catch(_){}var legacy=localStorage.getItem('bowhead.theme');var mode=c&&/^(system|light|dark)$/.test(c.mode)?c.mode:(legacy==='light'||legacy==='dark'?legacy:'system');var dark=mode==='dark'||(mode==='system'&&window.matchMedia&&window.matchMedia('(prefers-color-scheme:dark)').matches);var cp=c&&c.colorProfile&&c.colorProfile.type==='preset'&&/^(bowhead|ocean|forest|plum|graphite)$/.test(c.colorProfile.id)?c.colorProfile.id:null;var la=c&&/^(default|ocean|forest|plum|graphite)$/.test(c.accent)?c.accent:null;if(!cp)cp=la&&la!=='default'?la:'bowhead';var acc=cp==='bowhead'?'default':cp;var legacyChart=localStorage.getItem('bowhead.chart_theme_preset');var chart=c&&/^(default|high-contrast|colorblind-safe)$/.test(c.chartPalette)?c.chartPalette:/^(default|high-contrast|colorblind-safe)$/.test(legacyChart||'')?legacyChart:'default';r.dataset.theme=dark?'dark':'light';r.dataset.themeMode=mode;r.dataset.colorProfile=cp;r.dataset.themeAccent=acc;r.dataset.typographyProfile=c&&c.typographyProfile&&/^(bowhead|system|readable|serif-display)$/.test(c.typographyProfile.id)?c.typographyProfile.id:'bowhead';r.dataset.typeScale=c&&/^(compact|standard|large|extra-large)$/.test(c.typeScale)?c.typeScale:'standard';r.dataset.themeDensity=c&&/^(comfortable|compact|spacious)$/.test(c.density)?c.density:'comfortable';r.dataset.themeRadius=c&&/^(balanced|sharp|soft)$/.test(c.radius)?c.radius:'balanced';r.dataset.themeMotion=c&&/^(standard|reduced)$/.test(c.motion)?c.motion:'standard';r.dataset.chartPalette=chart;r.style.colorScheme=dark?'dark':'light'}catch(e){}})();`,
          }}
        />
        {/* Sidebar mode follows the same first-paint contract as theme. The
            persistent product shell reads this attribute during hydration,
            while its static Suspense fallback uses it directly in CSS. The
            expanded sidebar is the default from 1024, settings routes included. */}
        <script
          dangerouslySetInnerHTML={{
            __html: `(function(){try{var r=document.documentElement,w=window.innerWidth,m=localStorage.getItem('bowhead.sidebar.mode.current');if(m!=='expanded'&&m!=='compact')m=w<1024?'compact':'expanded';r.dataset.sidebarMode=m}catch(e){}})();`,
          }}
        />
        {/* Self-host fonts: Inter + JetBrains Mono + Source Serif 4 use next/font/local
            (app/_fonts); <html className> injects --font-* variables, no runtime requests to
            fonts.googleapis.com. E2E retains system fonts (empty className). */}
      </head>
      <body>
        <ClientLocaleProvider>
          <AuthenticatedAppRoot>{children}</AuthenticatedAppRoot>
        </ClientLocaleProvider>
      </body>
    </html>
  );
}
