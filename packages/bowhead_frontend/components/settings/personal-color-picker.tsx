"use client";

import {
  autoUpdate,
  flip,
  FloatingFocusManager,
  FloatingPortal,
  offset,
  shift,
  size,
  useClick,
  useDismiss,
  useFloating,
  useInteractions,
  useRole,
} from "@floating-ui/react";
import {
  useEffect,
  useId,
  useRef,
  useState,
  type KeyboardEvent,
  type PointerEvent,
} from "react";
import { useTranslations } from "next-intl";

import { Check, ChevronDown, X } from "@/components/icons";
import { ApiError } from "@/lib/api-client";
import type { PersonalAppearanceOverrides } from "@/lib/personal-appearance";
import type { EffectiveColorScheme } from "@/lib/theme-config";

import styles from "./personal-color-picker.module.css";

export interface ThemeColorCompileResult {
  candidateId: string;
  baseEtag: string;
  colorScheme: EffectiveColorScheme;
  validation: { blocking: unknown[]; warnings: unknown[] };
}

interface ThemeColorPickerProps<TCandidate> {
  label: string;
  value: string;
  mode: "light" | "dark";
  target: "accent" | "background";
  etag: string;
  recommendations: string[];
  disabled?: boolean;
  buildCandidate: (hex: string) => TCandidate;
  compile: (
    candidate: TCandidate,
    candidateId: string,
    signal?: AbortSignal,
  ) => Promise<ThemeColorCompileResult>;
  onApply: (hex: string) => Promise<void>;
}

const HEX_RE = /^#[0-9A-F]{6}$/;
let candidateSequence = 0;

export function ThemeColorPicker<TCandidate>({
  label,
  value,
  mode,
  target,
  etag,
  recommendations,
  disabled = false,
  buildCandidate,
  compile,
  onApply,
}: ThemeColorPickerProps<TCandidate>) {
  const t = useTranslations("settings.appearance.colorPicker");
  const titleId = useId();
  const [open, setOpen] = useState(false);
  const [hexDraft, setHexDraft] = useState(value);
  const [hsv, setHsv] = useState(() => hexToHsv(value));
  const [compileState, setCompileState] = useState<
    | { kind: "idle" | "checking" }
    | { kind: "valid"; result: ThemeColorCompileResult; hex: string }
    | { kind: "base-incompatible" }
    | { kind: "invalid" }
  >({ kind: "idle" });
  const [applying, setApplying] = useState(false);
  const latestCandidateRef = useRef("");
  const normalizedValue = normalizeHex(value);
  const normalizedDraft = normalizeHex(hexDraft);
  const unchanged =
    normalizedDraft !== null && normalizedDraft === normalizedValue;

  const { refs, floatingStyles, context } = useFloating({
    open,
    onOpenChange: setOpen,
    placement: "bottom-start",
    strategy: "fixed",
    whileElementsMounted: autoUpdate,
    middleware: [
      offset(8),
      flip({ padding: 12 }),
      shift({ padding: 12, crossAxis: true }),
      size({
        padding: 12,
        apply({ availableHeight, elements }) {
          elements.floating.style.setProperty(
            "--picker-available-height",
            `${Math.max(0, Math.floor(availableHeight))}px`,
          );
        },
      }),
    ],
  });
  const click = useClick(context);
  const dismiss = useDismiss(context);
  const role = useRole(context, { role: "dialog" });
  const { getReferenceProps, getFloatingProps } = useInteractions([
    click,
    dismiss,
    role,
  ]);

  useEffect(() => {
    if (!open) return;
    const normalized = normalizeHex(value) ?? "#2563EB";
    setHexDraft(normalized);
    setHsv(hexToHsv(normalized));
  }, [open, value]);

  useEffect(() => {
    if (!open) return;
    const normalized = normalizeHex(hexDraft);
    if (!normalized) {
      latestCandidateRef.current = "";
      setCompileState({ kind: "idle" });
      return;
    }
    if (normalized === normalizedValue) {
      latestCandidateRef.current = "";
      setCompileState({ kind: "idle" });
      return;
    }
    const controller = new AbortController();
    candidateSequence += 1;
    const candidateId = `picker-${Date.now()}-${candidateSequence}`;
    latestCandidateRef.current = candidateId;
    setCompileState({ kind: "checking" });
    const timer = window.setTimeout(() => {
      void compile(buildCandidate(normalized), candidateId, controller.signal)
        .then((result) => {
          if (
            controller.signal.aborted ||
            latestCandidateRef.current !== result.candidateId ||
            result.baseEtag !== etag
          ) {
            return;
          }
          setCompileState(
            result.validation.blocking.length
              ? { kind: "invalid" }
              : { kind: "valid", result, hex: normalized },
          );
        })
        .catch((error: unknown) => {
          if (
            controller.signal.aborted ||
            (error instanceof Error && error.name === "AbortError")
          ) {
            return;
          }
          setCompileState(
            error instanceof ApiError &&
              error.code === "personal_theme_base_incompatible"
              ? { kind: "base-incompatible" }
              : { kind: "invalid" },
          );
        });
    }, 180);
    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [buildCandidate, compile, etag, hexDraft, normalizedValue, open]);

  const setColor = (next: Hsv) => {
    const constrained = {
      h: wrapHue(next.h),
      s: clamp(next.s, 0, 100),
      v: clamp(next.v, 0, 100),
    };
    const hex = hsvToHex(constrained);
    setHsv(constrained);
    setHexDraft(hex);
  };

  const movePlane = (event: PointerEvent<HTMLDivElement>) => {
    const rect = event.currentTarget.getBoundingClientRect();
    if (!rect.width || !rect.height) return;
    setColor({
      h: hsv.h,
      s: ((event.clientX - rect.left) / rect.width) * 100,
      v: (1 - (event.clientY - rect.top) / rect.height) * 100,
    });
  };

  const movePlaneWithKeyboard = (event: KeyboardEvent<HTMLDivElement>) => {
    const step = event.shiftKey ? 10 : 1;
    const movement = {
      ArrowLeft: { s: -step, v: 0 },
      ArrowRight: { s: step, v: 0 },
      ArrowUp: { s: 0, v: step },
      ArrowDown: { s: 0, v: -step },
    }[event.key];
    if (!movement) return;
    event.preventDefault();
    setColor({ h: hsv.h, s: hsv.s + movement.s, v: hsv.v + movement.v });
  };

  const validCompile =
    !unchanged &&
    compileState.kind === "valid" &&
    compileState.hex === normalizedDraft &&
    compileState.result.baseEtag === etag;
  const previewScheme =
    compileState.kind === "valid"
      ? compileState.result.colorScheme[mode]
      : null;
  const ratio = previewScheme
    ? target === "accent"
      ? contrastRatio(previewScheme.accentInk, previewScheme.accent)
      : contrastRatio(previewScheme.ink, previewScheme.background)
    : null;

  const apply = async () => {
    const normalized = normalizeHex(hexDraft);
    if (!normalized || !validCompile || applying) return;
    setApplying(true);
    try {
      await onApply(normalized);
      setOpen(false);
    } finally {
      setApplying(false);
    }
  };

  return (
    <>
      <button
        ref={refs.setReference}
        type="button"
        className={styles.trigger}
        disabled={disabled}
        {...getReferenceProps()}
      >
        <span
          className={styles.triggerSwatch}
          style={{ background: value }}
          aria-hidden
        />
        <span className={styles.triggerCopy}>
          <strong>{label}</strong>
          <span>{value}</span>
        </span>
        <span className={styles.triggerState}>{t("custom")}</span>
        <ChevronDown size={15} aria-hidden />
      </button>
      {open ? (
        <FloatingPortal>
          <div className={styles.mobileBackdrop} aria-hidden />
          <FloatingFocusManager context={context} modal returnFocus>
            <section
              ref={refs.setFloating}
              className={styles.surface}
              style={floatingStyles}
              data-settings-nested-dialog="true"
              aria-labelledby={titleId}
              aria-modal="true"
              {...getFloatingProps()}
            >
              <div className={styles.sheetHandle} aria-hidden />
              <header className={styles.header}>
                <div>
                  <h3 id={titleId}>{label}</h3>
                  <p>{t("subtitle")}</p>
                </div>
                <button
                  type="button"
                  className={styles.close}
                  aria-label={t("close")}
                  onClick={() => setOpen(false)}
                >
                  <X size={15} aria-hidden />
                </button>
              </header>

              <div className={styles.current}>
                <div>
                  <span style={{ backgroundColor: value }} aria-hidden />
                  <small>{t("original")}</small>
                  <strong>{value}</strong>
                </div>
                <i aria-hidden>→</i>
                <div>
                  <span
                    style={{
                      backgroundColor:
                        normalizeHex(hexDraft) ?? "transparent",
                    }}
                    aria-hidden
                  />
                  <small>{t("new")}</small>
                  <strong>{normalizeHex(hexDraft) ?? t("incomplete")}</strong>
                </div>
              </div>

              <div
                className={styles.plane}
                role="slider"
                tabIndex={0}
                aria-label={t("plane")}
                aria-valuetext={t("planeValue", {
                  saturation: Math.round(hsv.s),
                  brightness: Math.round(hsv.v),
                })}
                style={{
                  backgroundColor: `hsl(${hsv.h} 100% 50%)`,
                }}
                onPointerDown={(event) => {
                  event.currentTarget.setPointerCapture(event.pointerId);
                  movePlane(event);
                }}
                onPointerMove={(event) => {
                  if (event.currentTarget.hasPointerCapture(event.pointerId)) {
                    movePlane(event);
                  }
                }}
                onKeyDown={movePlaneWithKeyboard}
              >
                <span
                  className={styles.planeThumb}
                  style={{ left: `${hsv.s}%`, top: `${100 - hsv.v}%` }}
                  aria-hidden
                />
              </div>

              <label className={styles.hueLabel}>
                <span>{t("hue")}</span>
                <input
                  type="range"
                  min="0"
                  max="359"
                  value={Math.round(hsv.h)}
                  onChange={(event) =>
                    setColor({ ...hsv, h: Number(event.currentTarget.value) })
                  }
                />
              </label>

              {recommendations.length ? (
                <div className={styles.recommendations}>
                  <span>{t("recommended")}</span>
                  <div>
                    {recommendations.slice(0, 6).map((color) => (
                      <button
                        key={color}
                        type="button"
                        aria-label={color}
                        title={color}
                        style={{ background: color }}
                        onClick={() => {
                          setHexDraft(color);
                          setHsv(hexToHsv(color));
                        }}
                      />
                    ))}
                  </div>
                </div>
              ) : null}

              <label className={styles.hexField}>
                <span>Hex</span>
                <input
                  name={`theme-color-${target}-${mode}`}
                  value={hexDraft}
                  inputMode="text"
                  autoComplete="off"
                  autoCapitalize="characters"
                  spellCheck={false}
                  maxLength={7}
                  aria-invalid={Boolean(hexDraft && !normalizeHex(hexDraft))}
                  onChange={(event) => {
                    const raw = event.currentTarget.value.toUpperCase();
                    setHexDraft(raw.startsWith("#") ? raw : `#${raw}`);
                    const normalized = normalizeHex(raw.startsWith("#") ? raw : `#${raw}`);
                    if (normalized) setHsv(hexToHsv(normalized));
                  }}
                />
              </label>

              <div className={styles.validation} aria-live="polite">
                <span
                  className={
                    compileState.kind === "valid"
                      ? styles.validationIconValid
                      : styles.validationIcon
                  }
                  aria-hidden
                >
                  {compileState.kind === "valid" ? <Check size={13} /> : "·"}
                </span>
                <div>
                  <strong>
                    {unchanged
                      ? t("unchanged")
                      : compileState.kind === "checking"
                      ? t("checking")
                      : compileState.kind === "valid"
                        ? t("valid")
                        : compileState.kind === "base-incompatible"
                          ? t("baseIncompatible")
                        : compileState.kind === "invalid"
                          ? t("invalid")
                          : t("waiting")}
                  </strong>
                  <span>
                    {unchanged
                      ? t("unchangedHint")
                      : ratio === null
                      ? t("validationHint")
                      : t("contrast", { ratio: ratio.toFixed(1) })}
                  </span>
                </div>
              </div>

              <footer className={styles.footer}>
                <button type="button" onClick={() => setOpen(false)}>
                  {t("cancel")}
                </button>
                <button
                  type="button"
                  className={styles.apply}
                  disabled={!validCompile || applying}
                  onClick={() => void apply()}
                >
                  {applying ? t("applying") : t("apply")}
                </button>
              </footer>
            </section>
          </FloatingFocusManager>
        </FloatingPortal>
      ) : null}
    </>
  );
}

export function PersonalColorPicker(
  props: ThemeColorPickerProps<PersonalAppearanceOverrides>,
) {
  return <ThemeColorPicker {...props} />;
}

interface Hsv {
  h: number;
  s: number;
  v: number;
}

function normalizeHex(value: string): string | null {
  const normalized = value.trim().toUpperCase();
  return HEX_RE.test(normalized) ? normalized : null;
}

function hexToHsv(hex: string): Hsv {
  const normalized = normalizeHex(hex) ?? "#2563EB";
  const red = Number.parseInt(normalized.slice(1, 3), 16) / 255;
  const green = Number.parseInt(normalized.slice(3, 5), 16) / 255;
  const blue = Number.parseInt(normalized.slice(5, 7), 16) / 255;
  const max = Math.max(red, green, blue);
  const min = Math.min(red, green, blue);
  const delta = max - min;
  let hue = 0;
  if (delta) {
    if (max === red) hue = 60 * (((green - blue) / delta) % 6);
    else if (max === green) hue = 60 * ((blue - red) / delta + 2);
    else hue = 60 * ((red - green) / delta + 4);
  }
  return {
    h: wrapHue(hue),
    s: max ? (delta / max) * 100 : 0,
    v: max * 100,
  };
}

function hsvToHex({ h, s, v }: Hsv): string {
  const saturation = s / 100;
  const brightness = v / 100;
  const chroma = brightness * saturation;
  const segment = h / 60;
  const x = chroma * (1 - Math.abs((segment % 2) - 1));
  const [red, green, blue] =
    segment < 1
      ? [chroma, x, 0]
      : segment < 2
        ? [x, chroma, 0]
        : segment < 3
          ? [0, chroma, x]
          : segment < 4
            ? [0, x, chroma]
            : segment < 5
              ? [x, 0, chroma]
              : [chroma, 0, x];
  const match = brightness - chroma;
  return `#${[red, green, blue]
    .map((channel) =>
      Math.round((channel + match) * 255)
        .toString(16)
        .padStart(2, "0")
        .toUpperCase(),
    )
    .join("")}`;
}

function contrastRatio(first: string, second: string): number {
  const a = relativeLuminance(first);
  const b = relativeLuminance(second);
  return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
}

function relativeLuminance(hex: string): number {
  const normalized = normalizeHex(hex) ?? "#000000";
  const channels = [1, 3, 5].map((start) => {
    const value = Number.parseInt(normalized.slice(start, start + 2), 16) / 255;
    return value <= 0.04045
      ? value / 12.92
      : ((value + 0.055) / 1.055) ** 2.4;
  });
  return channels[0] * 0.2126 + channels[1] * 0.7152 + channels[2] * 0.0722;
}

function wrapHue(value: number): number {
  return ((value % 360) + 360) % 360;
}

function clamp(value: number, minimum: number, maximum: number): number {
  return Math.min(maximum, Math.max(minimum, value));
}
