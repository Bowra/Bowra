import localFont from "next/font/local";

// Self-hosted fonts (remove runtime dependencies on fonts.googleapis.com / fonts.gstatic.com).
// - Variable fonts: one file covers 400/500/600/700 weights, from @fontsource-variable.
// - Latin subset only: CJK uses system fonts (PingFang / Source Han) in --font-* fallback stacks, never bundled.
// - next/font generates hashed family names; CSS references --font-* exposed by variable below (see globals.css).
// - display:swap + adjustFontFallback size-adjust fallback reduces layout shifts during font swapping.
//
// Font files use Next's resource pipeline (_next/static/media/*), the same working path as JS/CSS chunks,
// naturally inheriting runtime public-entry prefixes, without handwritten @font-face url().

const inter = localFont({
  src: "./inter-latin-wght-normal.woff2",
  weight: "100 900",
  style: "normal",
  display: "swap",
  variable: "--font-inter",
  fallback: [
    "-apple-system",
    "BlinkMacSystemFont",
    "Segoe UI",
    "system-ui",
    "sans-serif",
  ],
});

const jetbrainsMono = localFont({
  src: "./jetbrains-mono-latin-wght-normal.woff2",
  weight: "100 800",
  style: "normal",
  display: "swap",
  variable: "--font-jetbrains-mono",
  fallback: ["ui-monospace", "SFMono-Regular", "Menlo", "monospace"],
});

const sourceSerif = localFont({
  src: [
    {
      path: "./source-serif-4-latin-wght-normal.woff2",
      weight: "200 900",
      style: "normal",
    },
    {
      path: "./source-serif-4-latin-wght-italic.woff2",
      weight: "200 900",
      style: "italic",
    },
  ],
  display: "swap",
  variable: "--font-source-serif",
  fallback: ["Georgia", "Cambria", "Times New Roman", "serif"],
});

// Attach to <html> className: inject --font-inter / --font-jetbrains-mono / --font-source-serif together.
export const fontVariables = `${inter.variable} ${jetbrainsMono.variable} ${sourceSerif.variable}`;
