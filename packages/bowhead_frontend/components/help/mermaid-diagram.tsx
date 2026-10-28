"use client";

import { useEffect, useId, useState } from "react";

import styles from "./help-center-page.module.css";

interface MermaidDiagramProps {
  source: string;
  diagramLabel: string;
  loadingLabel: string;
  errorLabel: string;
}

export function MermaidDiagram({
  source,
  diagramLabel,
  loadingLabel,
  errorLabel,
}: MermaidDiagramProps) {
  const reactId = useId();
  const [theme, setTheme] = useState<"default" | "dark">("default");
  const [svg, setSvg] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    const root = document.documentElement;
    const syncTheme = () => {
      setTheme(root.dataset.theme === "dark" ? "dark" : "default");
    };
    syncTheme();
    const observer = new MutationObserver(syncTheme);
    observer.observe(root, { attributes: true, attributeFilter: ["data-theme"] });
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    let cancelled = false;
    setSvg(null);
    setFailed(false);
    const diagramId = `help-diagram-${reactId.replace(/[^a-zA-Z0-9_-]/g, "")}`;

    void import("mermaid")
      .then(async ({ default: mermaid }) => {
        mermaid.initialize({
          startOnLoad: false,
          securityLevel: "strict",
          theme,
          fontFamily: "var(--font-v2-sans)",
          flowchart: { htmlLabels: true, curve: "basis" },
        });
        const result = await mermaid.render(diagramId, source);
        if (!cancelled) setSvg(result.svg);
      })
      .catch(() => {
        if (!cancelled) setFailed(true);
      });

    return () => {
      cancelled = true;
    };
  }, [reactId, source, theme]);

  if (failed) {
    return (
      <figure className={`${styles.diagram} ${styles.diagramError}`}>
        <figcaption>{errorLabel}</figcaption>
      </figure>
    );
  }

  return (
    <figure
      className={styles.diagram}
      aria-label={diagramLabel}
      aria-busy={!svg}
    >
      {svg ? (
        <div
          className={styles.diagramCanvas}
          // Mermaid receives repository-owned Help Markdown and escapes labels in
          // strict mode. The generated SVG is never built from user input.
          dangerouslySetInnerHTML={{ __html: svg }}
        />
      ) : (
        <div className={styles.diagramLoading}>{loadingLabel}</div>
      )}
    </figure>
  );
}
