"use client";

import { useTranslations } from "next-intl";
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import type { CSSProperties } from "react";
import type { DesignTextAlign, DesignTextLayer, ReportSection } from "@/lib/types";
import { InlineEditableText } from "@/lib/canvas-pm-adapter/extensions/inline-text";
import { useArtifactHref } from "@/lib/canvas-pm-adapter/canvas-context";
import {
  Download as LucideDownload,
} from "lucide-react";
import { LucideGlyph } from "@/components/icon-refresh";

type E = Extract<ReportSection, { kind: "design" }>;

export interface DesignSectionProps {
  data: E;
  isEditMode?: boolean;
  selectedLayerId?: string | null;
  downloadUrl?: string | null;
  isFlattening?: boolean;
  flattenError?: string | null;
  onSelectLayer?: (layerId: string) => void;
  onLayerTextChange?: (layerId: string, text: string) => void;
  onLayerStyleChange?: (layerId: string, patch: Partial<DesignTextLayer>) => void;
  onFlatten?: () => void;
}

const HEX_COLOR_RE = /^#[0-9a-fA-F]{6}$/;
const ALIGN_OPTIONS: DesignTextAlign[] = ["left", "center", "right"];
const useIsomorphicLayoutEffect = typeof window !== "undefined" ? useLayoutEffect : useEffect;

function clampNumber(value: unknown, fallback: number, min: number, max: number): number {
  const n = typeof value === "number" ? value : Number(value);
  if (!Number.isFinite(n)) return fallback;
  return Math.min(max, Math.max(min, n));
}

function safeHex(value: unknown): string {
  return typeof value === "string" && HEX_COLOR_RE.test(value) ? value : "#111111";
}

function layerBoxStyle(layer: DesignTextLayer): CSSProperties {
  const x = clampNumber(layer.x, 0, 0, 1);
  const y = clampNumber(layer.y, 0, 0, 1);
  const w = clampNumber(layer.w, 0.8, 0.01, 1);
  const h = clampNumber(layer.h, 0.12, 0.01, 1);
  const rotation = clampNumber(layer.rotation, 0, -180, 180);
  return {
    position: "absolute",
    left: `${x * 100}%`,
    top: `${y * 100}%`,
    width: `${w * 100}%`,
    height: `${h * 100}%`,
    transform: rotation ? `rotate(${rotation}deg)` : undefined,
    transformOrigin: "center center",
    zIndex: Math.round(clampNumber(layer.z, 0, -1000, 1000)),
  };
}

function layerTextStyle(layer: DesignTextLayer, canvasWidth: number): CSSProperties {
  const fontSize = clampNumber(layer.font_size, 56, 8, 160);
  const fontWeight = Math.round(clampNumber(layer.font_weight, 700, 300, 900));
  const lineHeight = clampNumber(layer.line_height, 1.08, 0.9, 1.8);
  const letterSpacing = clampNumber(layer.letter_spacing, 0, -2, 8);
  const align = ALIGN_OPTIONS.includes(layer.align) ? layer.align : "center";
  return {
    display: "flex",
    alignItems: "center",
    justifyContent:
      align === "left" ? "flex-start" : align === "right" ? "flex-end" : "center",
    width: "100%",
    height: "100%",
    overflow: "hidden",
    color: safeHex(layer.color),
    fontFamily: layer.font_family || "Noto Sans SC, system-ui, sans-serif",
    fontSize: `clamp(8px, ${((fontSize / canvasWidth) * 100).toFixed(4)}cqw, ${fontSize}px)`,
    fontWeight,
    lineHeight,
    letterSpacing: `${((letterSpacing / canvasWidth) * 100).toFixed(4)}cqw`,
    textAlign: align,
    whiteSpace: "pre-wrap",
    overflowWrap: "anywhere",
    textShadow: "0 1px 2px rgba(0, 0, 0, 0.18)",
  };
}

function canvasMaxWidth(width: number, height: number): number {
  const aspect = width / height;
  return Math.round(Math.max(320, Math.min(760, 720 * aspect)));
}

function isTextOverflowing(element: HTMLElement): boolean {
  return element.scrollWidth > element.clientWidth + 1
    || element.scrollHeight > element.clientHeight + 1;
}

interface DesignLayerViewProps {
  layer: DesignTextLayer;
  canvasWidth: number;
  isEditMode: boolean;
  isSelected: boolean;
  isOverflowing: boolean;
  onSelectLayer?: (layerId: string) => void;
  onLayerTextChange?: (layerId: string, text: string) => void;
  onOverflowChange: (layerId: string, overflowing: boolean) => void;
}

function DesignLayerView({
  layer,
  canvasWidth,
  isEditMode,
  isSelected,
  isOverflowing,
  onSelectLayer,
  onLayerTextChange,
  onOverflowChange,
}: DesignLayerViewProps) {
  const t = useTranslations("canvas");
  const textElementRef = useRef<HTMLElement | null>(null);
  const canEditLayer = isEditMode && !layer.locked && Boolean(onLayerTextChange);
  const setTextElement = useCallback((element: HTMLElement | null) => {
    textElementRef.current = element;
  }, []);
  const measureOverflow = useCallback(() => {
    const element = textElementRef.current;
    if (!element) return;
    onOverflowChange(layer.id, isTextOverflowing(element));
  }, [layer.id, onOverflowChange]);

  useIsomorphicLayoutEffect(() => {
    measureOverflow();
    const element = textElementRef.current;
    if (!element || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(measureOverflow);
    observer.observe(element);
    return () => observer.disconnect();
  }, [
    measureOverflow,
    layer.text,
    layer.font_size,
    layer.font_family,
    layer.line_height,
    layer.letter_spacing,
    layer.w,
    layer.h,
  ]);

  useEffect(() => {
    return () => onOverflowChange(layer.id, false);
  }, [layer.id, onOverflowChange]);

  return (
    <div
      data-testid={`design-layer-${layer.id}`}
      data-layer-id={layer.id}
      onMouseDown={(event) => {
        if (!isEditMode) return;
        event.stopPropagation();
        onSelectLayer?.(layer.id);
      }}
      onClick={(event) => {
        if (!isEditMode) return;
        event.stopPropagation();
        onSelectLayer?.(layer.id);
      }}
      style={{
        ...layerBoxStyle(layer),
        pointerEvents: isEditMode ? "auto" : "none",
        outline: isSelected
          ? "1px solid rgba(59, 130, 246, 0.8)"
          : isEditMode && isOverflowing
            ? "1px solid rgba(217, 119, 6, 0.85)"
            : isEditMode
              ? "1px solid rgba(255, 255, 255, 0.28)"
              : "none",
        outlineOffset: 2,
        borderRadius: 4,
      }}
    >
      <InlineEditableText
        as="div"
        value={layer.text ?? ""}
        isEditMode={canEditLayer}
        multiline
        onChange={(text) => onLayerTextChange?.(layer.id, text)}
        elementRef={setTextElement}
        style={layerTextStyle(layer, canvasWidth)}
      />
      {isEditMode && isOverflowing ? (
        <span
          aria-label={t("design_text_overflow")}
          title={t("design_text_overflow")}
          style={{
            position: "absolute",
            top: -10,
            right: -10,
            width: 18,
            height: 18,
            display: "grid",
            placeItems: "center",
            border: "1px solid rgba(255, 255, 255, 0.9)",
            borderRadius: 999,
            background: "#d97706",
            color: "#ffffff",
            fontSize: "0.75rem",
            fontWeight: 800,
            lineHeight: 1,
            pointerEvents: "none",
          }}
        >
          !
        </span>
      ) : null}
    </div>
  );
}

export function DesignSection({
  data,
  isEditMode = false,
  selectedLayerId = null,
  downloadUrl = null,
  isFlattening = false,
  flattenError = null,
  onSelectLayer,
  onLayerTextChange,
  onLayerStyleChange,
  onFlatten,
}: DesignSectionProps) {
  const t = useTranslations("canvas");
  const [imageState, setImageState] = useState<"loading" | "loaded" | "error">("loading");
  const [overflowingLayerIds, setOverflowingLayerIds] = useState<Set<string>>(() => new Set());
  const canvasWidth = Math.max(1, Math.round(data.canvas_width || 1080));
  const canvasHeight = Math.max(1, Math.round(data.canvas_height || 1440));
  const backgroundArtifactId = data.background?.artifact_id;
  // content-permissions: report views use the permission intermediary (share recipients see poster backgrounds); otherwise /api/v1/files/{id}.
  // Call hooks unconditionally → use "" as a placeholder, then decide based on backgroundArtifactId.
  const bgInlineHref = useArtifactHref(backgroundArtifactId ?? "", { inline: true });
  const bgDownloadHref = useArtifactHref(backgroundArtifactId ?? "");
  const inlineUrl = backgroundArtifactId ? bgInlineHref : "";
  const backgroundDownloadUrl = backgroundArtifactId ? bgDownloadHref : "";

  useEffect(() => {
    setImageState(backgroundArtifactId ? "loading" : "error");
  }, [backgroundArtifactId]);

  const layers = useMemo(
    () => [...(data.layers ?? [])].sort((a, b) => (a.z ?? 0) - (b.z ?? 0)),
    [data.layers],
  );
  const selectedLayer = layers.find((layer) => layer.id === selectedLayerId) ?? null;
  const selectedLayerOverflowing = selectedLayer
    ? overflowingLayerIds.has(selectedLayer.id)
    : false;
  const canEditSelectedStyle = isEditMode
    && Boolean(selectedLayer)
    && !selectedLayer?.locked
    && Boolean(onLayerStyleChange);
  const imageFit = data.background?.fit === "contain"
    ? "contain"
    : data.background?.fit === "fill"
      ? "fill"
      : "cover";
  const imageOpacity = clampNumber(data.background?.opacity, 1, 0, 1);
  const handleLayerOverflowChange = useCallback((layerId: string, overflowing: boolean) => {
    setOverflowingLayerIds((prev) => {
      if (prev.has(layerId) === overflowing) return prev;
      const next = new Set(prev);
      if (overflowing) {
        next.add(layerId);
      } else {
        next.delete(layerId);
      }
      return next;
    });
  }, []);

  useEffect(() => {
    const validLayerIds = new Set(layers.map((layer) => layer.id));
    setOverflowingLayerIds((prev) => {
      let changed = false;
      const next = new Set<string>();
      prev.forEach((layerId) => {
        if (validLayerIds.has(layerId)) {
          next.add(layerId);
        } else {
          changed = true;
        }
      });
      return changed ? next : prev;
    });
  }, [layers]);

  return (
    <section
      className="v2-design-section"
      data-export-render-state={imageState}
      style={{ margin: "8px 0 16px" }}
    >
      {data.title || onFlatten || downloadUrl ? (
        <div
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            gap: 12,
            margin: "0 0 8px",
          }}
        >
          {data.title ? (
            <div
              style={{
                fontSize: "0.8125rem",
                fontWeight: 600,
                color: "var(--c-ink)",
              }}
            >
              {data.title}
            </div>
          ) : <span />}
          {onFlatten || downloadUrl ? (
            <button
              data-testid="design-flatten-button"
              type="button"
              onClick={onFlatten ?? (() => {
                if (downloadUrl && typeof window !== "undefined") {
                  window.location.href = downloadUrl;
                }
              })}
              disabled={isFlattening || (!onFlatten && !downloadUrl)}
              title={isFlattening ? t("design_exporting_title") : t("design_download_png")}
              style={{
                display: "inline-flex",
                alignItems: "center",
                gap: 6,
                minHeight: 28,
                padding: "0 9px",
                border: "1px solid var(--c-line)",
                borderRadius: 6,
                background: "var(--c-surface)",
                color: "var(--c-ink-2)",
                fontSize: "0.75rem",
                cursor: isFlattening ? "wait" : "pointer",
                opacity: isFlattening ? 0.72 : 1,
              }}
            >
              <LucideGlyph icon={LucideDownload} size={13} aria-hidden="true" />
              <span>{isFlattening ? t("design_exporting") : "PNG"}</span>
            </button>
          ) : null}
        </div>
      ) : null}
      <div
        style={{
          width: "100%",
          maxWidth: canvasMaxWidth(canvasWidth, canvasHeight),
          margin: "0 auto",
        }}
      >
        <div
          data-testid="design-stage"
          style={{
            position: "relative",
            width: "100%",
            aspectRatio: `${canvasWidth} / ${canvasHeight}`,
            containerType: "inline-size" as CSSProperties["containerType"],
            border: "1px solid var(--c-line)",
            borderRadius: 8,
            overflow: "hidden",
            background: "var(--c-surface-2)",
            boxShadow: "0 1px 2px rgba(0, 0, 0, 0.04)",
          }}
        >
          {imageState === "error" ? (
            <div
              style={{
                position: "absolute",
                inset: 0,
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                padding: 20,
                color: "var(--c-ink-3)",
                fontSize: "0.75rem",
              }}
            >
              {t("design_bg_load_failed")}
              {backgroundDownloadUrl ? (
                <a
                  href={backgroundDownloadUrl}
                  rel="noreferrer"
                  style={{ color: "var(--c-ink-2)", marginLeft: 6 }}
                >
                  {t("try_download")}
                </a>
              ) : null}
            </div>
          ) : null}
          {imageState === "loading" ? (
            <div
              aria-hidden="true"
              style={{
                position: "absolute",
                inset: 0,
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                color: "var(--c-ink-3)",
                fontSize: "0.75rem",
              }}
            >
              {t("loading")}
            </div>
          ) : null}
          {inlineUrl ? (
            // eslint-disable-next-line @next/next/no-img-element
            <img
              src={inlineUrl}
              alt={data.title || t("design_bg_alt")}
              loading="lazy"
              onLoad={() => setImageState("loaded")}
              onError={() => setImageState("error")}
              style={{
                position: "absolute",
                inset: 0,
                width: "100%",
                height: "100%",
                objectFit: imageFit,
                opacity: imageState === "loaded" ? imageOpacity : 0,
                transition: "opacity 160ms ease",
              }}
            />
          ) : null}
          {layers.map((layer) => {
            const isSelected = selectedLayerId === layer.id;
            return (
              <DesignLayerView
                key={layer.id}
                layer={layer}
                canvasWidth={canvasWidth}
                isEditMode={isEditMode}
                isSelected={isSelected}
                isOverflowing={overflowingLayerIds.has(layer.id)}
                onSelectLayer={onSelectLayer}
                onLayerTextChange={onLayerTextChange}
                onOverflowChange={handleLayerOverflowChange}
              />
            );
          })}
        </div>
        {canEditSelectedStyle && selectedLayer && onLayerStyleChange ? (
          <div
            data-testid="design-style-toolbar"
            role="toolbar"
            aria-label={t("design_text_style")}
            onMouseDown={(event) => event.stopPropagation()}
            onClick={(event) => event.stopPropagation()}
            style={{
              display: "flex",
              alignItems: "center",
              gap: 8,
              flexWrap: "wrap",
              marginTop: 8,
              padding: "8px 0",
            }}
          >
            <input
              type="color"
              aria-label={t("design_text_color")}
              title={t("design_text_color")}
              value={safeHex(selectedLayer.color)}
              onChange={(event) => {
                onLayerStyleChange(selectedLayer.id, { color: event.target.value });
              }}
              style={{
                width: 32,
                height: 32,
                padding: 2,
                border: "1px solid var(--c-line)",
                borderRadius: 6,
                background: "var(--c-surface)",
                cursor: "pointer",
              }}
            />
            <input
              type="number"
              aria-label={t("design_font_size")}
              title={t("design_font_size")}
              min={8}
              max={160}
              value={Math.round(clampNumber(selectedLayer.font_size, 56, 8, 160))}
              onChange={(event) => {
                onLayerStyleChange(selectedLayer.id, {
                  font_size: clampNumber(event.target.value, 56, 8, 160),
                });
              }}
              style={{
                width: 72,
                height: 32,
                border: "1px solid var(--c-line)",
                borderRadius: 6,
                padding: "0 8px",
                color: "var(--c-ink)",
                background: "var(--c-surface)",
                fontSize: "0.75rem",
              }}
            />
            <div
              role="group"
              aria-label={t("design_text_align")}
              style={{
                display: "flex",
                border: "1px solid var(--c-line)",
                borderRadius: 6,
                overflow: "hidden",
              }}
            >
              {ALIGN_OPTIONS.map((align) => {
                const active = selectedLayer.align === align;
                return (
                  <button
                    key={align}
                    type="button"
                    aria-label={t(align === "left" ? "design_align_left" : align === "right" ? "design_align_right" : "design_align_center")}
                    title={t(align === "left" ? "design_align_left" : align === "right" ? "design_align_right" : "design_align_center")}
                    onClick={() => onLayerStyleChange(selectedLayer.id, { align })}
                    style={{
                      width: 34,
                      height: 30,
                      border: 0,
                      borderRight: align === "right" ? 0 : "1px solid var(--c-line)",
                      background: active ? "var(--c-ink)" : "var(--c-surface)",
                      color: active ? "var(--c-surface)" : "var(--c-ink-2)",
                      fontSize: "0.6875rem",
                      cursor: "pointer",
                    }}
                  >
                    {t(align === "left" ? "design_align_left_short" : align === "right" ? "design_align_right_short" : "design_align_center_short")}
                  </button>
                );
              })}
            </div>
            {selectedLayerOverflowing ? (
              <span
                role="status"
                aria-live="polite"
                style={{
                  color: "#92400e",
                  fontSize: "0.75rem",
                  fontWeight: 600,
                }}
              >
                {t("design_text_overflow")}
              </span>
            ) : null}
          </div>
        ) : null}
        {flattenError ? (
          <div
            role="alert"
            style={{
              marginTop: 6,
              fontSize: "0.75rem",
              color: "var(--c-err, #9f2438)",
            }}
          >
            {flattenError}
          </div>
        ) : null}
      </div>
    </section>
  );
}
