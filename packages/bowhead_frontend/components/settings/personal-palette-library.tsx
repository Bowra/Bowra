"use client";

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ChangeEvent,
  type CSSProperties,
} from "react";
import { useTranslations } from "next-intl";

import { Check, FileDown, Palette, Upload } from "@/components/icons";
import { PersonalColorPicker } from "@/components/settings/personal-color-picker";
import type { usePersonalAppearance } from "@/components/settings/personal-appearance-provider";
import {
  paletteFileToPersonalPalette,
  parsePersonalPaletteFile,
  PERSONAL_PALETTE_FILE_MAX_BYTES,
  PERSONAL_PALETTE_TEMPLATE,
  type PersonalPaletteFile,
} from "@/lib/personal-palette-file";
import type {
  PersonalAppearanceOverrides,
  PersonalAppearanceRegistryItem,
  PersonalAppearanceResource,
  PersonalPalette,
} from "@/lib/personal-appearance";

import styles from "./personal-appearance-settings.module.css";

let importCandidateSequence = 0;

interface PersonalPaletteLibraryProps {
  resource: PersonalAppearanceResource;
  etag: string;
  busy: boolean;
  saving: boolean;
  failed: boolean;
  compile: ReturnType<typeof usePersonalAppearance>["compile"];
  onApply: (palette: PersonalPalette) => Promise<void>;
}

type ImportValidation =
  | { kind: "idle" | "checking" }
  | {
      kind: "valid";
      colorScheme: PersonalAppearanceResource["effective"]["colorScheme"];
    }
  | { kind: "invalid" };

export function PersonalPaletteLibrary({
  resource,
  etag,
  busy,
  saving,
  failed,
  compile,
  onApply,
}: PersonalPaletteLibraryProps) {
  const t = useTranslations("settings.appearance.palette");
  const inputRef = useRef<HTMLInputElement | null>(null);
  const [draft, setDraft] = useState<PersonalPaletteFile | null>(null);
  const [fileError, setFileError] = useState<string | null>(null);

  const paletteCopy = useMemo(
    () => ({
      "bowhead-editorial": {
        title: t("options.bowheadEditorial.title"),
        description: t("options.bowheadEditorial.description"),
      },
      "saas-blue": {
        title: t("options.saasBlue.title"),
        description: t("options.saasBlue.description"),
      },
      "product-indigo": {
        title: t("options.productIndigo.title"),
        description: t("options.productIndigo.description"),
      },
      "collaboration-teal": {
        title: t("options.collaborationTeal.title"),
        description: t("options.collaborationTeal.description"),
      },
      "focus-violet": {
        title: t("options.focusViolet.title"),
        description: t("options.focusViolet.description"),
      },
      "neutral-graphite": {
        title: t("options.neutralGraphite.title"),
        description: t("options.neutralGraphite.description"),
      },
    }),
    [t],
  );

  const importFile = async (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.currentTarget.files?.[0];
    event.currentTarget.value = "";
    if (!file) return;
    setFileError(null);
    if (file.size > PERSONAL_PALETTE_FILE_MAX_BYTES) {
      setFileError(t("import.errors.tooLarge"));
      return;
    }
    try {
      setDraft(parsePersonalPaletteFile(await file.text()));
    } catch (error) {
      const code = error instanceof Error ? error.message : "";
      setFileError(
        code === "palette_file_too_large"
          ? t("import.errors.tooLarge")
          : code === "palette_file_invalid_color"
            ? t("import.errors.color")
            : t("import.errors.format"),
      );
    }
  };

  return (
    <section
      className={`${styles.paletteLibrary} pa-palette-library`}
      data-testid="personal-appearance-palettes"
    >
      <header className={styles.paletteLibraryHead}>
        <div>
          <span className={styles.paletteIcon} aria-hidden>
            <Palette size={15} />
          </span>
          <div>
            <h3>{t("title")}</h3>
            <p>{t("desc")}</p>
          </div>
        </div>
        {saving || failed ? (
          <span
            className={styles.paletteStatus}
            data-state={failed ? "error" : "saving"}
            role={failed ? "alert" : "status"}
          >
            {failed ? t("saveFailed") : t("saving")}
          </span>
        ) : null}
      </header>

      <div className={styles.paletteGrid}>
        {resource.registries.palette.items.map((item) => {
          const copy = paletteCopy[item.id as keyof typeof paletteCopy] ?? {
            title: item.id,
            description: t("fallbackDescription"),
          };
          const selected = paletteMatches(resource, item);
          return (
            <button
              key={item.id}
              type="button"
              className={styles.paletteChoice}
              aria-label={`${copy.title}. ${copy.description}`}
              aria-pressed={selected}
              disabled={busy || item.availability !== "available"}
              title={
                item.availability === "available"
                  ? copy.description
                  : t("unavailable")
              }
              onClick={() => {
                if (!selected) void onApply(item.value).catch(() => undefined);
              }}
            >
              <PaletteSwatch item={item} />
              <span className={styles.paletteChoiceCopy}>
                <strong>{copy.title}</strong>
                <small>{copy.description}</small>
              </span>
              {selected ? (
                <span className={styles.paletteSelected} aria-hidden>
                  <Check size={11} />
                </span>
              ) : null}
            </button>
          );
        })}
      </div>

      <div className={styles.paletteImportBar}>
        <div>
          <strong>{t("import.title")}</strong>
          <span>{t("import.desc")}</span>
        </div>
        <div className={styles.paletteImportActions}>
          <a
            href={`data:application/json;charset=utf-8,${encodeURIComponent(PERSONAL_PALETTE_TEMPLATE)}`}
            download="bowhead-palette.json"
          >
            <FileDown size={14} aria-hidden />
            {t("import.template")}
          </a>
          <button
            type="button"
            disabled={busy}
            onClick={() => inputRef.current?.click()}
          >
            <Upload size={14} aria-hidden />
            {t("import.action")}
          </button>
          <input
            ref={inputRef}
            className={styles.visuallyHidden}
            type="file"
            accept=".json,application/json"
            aria-hidden="true"
            tabIndex={-1}
            onChange={(event) => void importFile(event)}
          />
        </div>
      </div>

      {fileError ? (
        <p className={styles.paletteImportError} role="alert">
          {fileError}
        </p>
      ) : null}

      {draft ? (
        <PaletteImportEditor
          draft={draft}
          resource={resource}
          etag={etag}
          busy={busy}
          compile={compile}
          onChange={setDraft}
          onCancel={() => setDraft(null)}
          onApply={async (palette) => {
            await onApply(palette);
            setDraft(null);
          }}
        />
      ) : null}
    </section>
  );
}

function PaletteSwatch({
  item,
}: {
  item: PersonalAppearanceRegistryItem<PersonalPalette>;
}) {
  return (
    <span className={styles.paletteSwatch} aria-hidden>
      {(["light", "dark"] as const).map((mode) => {
        const swatch = item.swatch[mode];
        const style = {
          "--palette-bg": swatch?.secondary,
          "--palette-accent": swatch?.primary,
        } as CSSProperties;
        return (
          <i key={mode} style={style}>
            <b />
            <em />
            <span />
          </i>
        );
      })}
    </span>
  );
}

function PaletteImportEditor({
  draft,
  resource,
  etag,
  busy,
  compile,
  onChange,
  onCancel,
  onApply,
}: {
  draft: PersonalPaletteFile;
  resource: PersonalAppearanceResource;
  etag: string;
  busy: boolean;
  compile: ReturnType<typeof usePersonalAppearance>["compile"];
  onChange: (draft: PersonalPaletteFile) => void;
  onCancel: () => void;
  onApply: (palette: PersonalPalette) => Promise<void>;
}) {
  const t = useTranslations("settings.appearance.palette.import");
  const [validation, setValidation] = useState<ImportValidation>({
    kind: "idle",
  });
  const [applying, setApplying] = useState(false);
  const palette = useMemo(() => paletteFileToPersonalPalette(draft), [draft]);
  const candidate = useMemo(
    () => ({ ...resource.overrides, ...palette }),
    [palette, resource.overrides],
  );

  useEffect(() => {
    const controller = new AbortController();
    importCandidateSequence += 1;
    const candidateId = `palette-import-${Date.now()}-${importCandidateSequence}`;
    setValidation({ kind: "checking" });
    const timer = window.setTimeout(() => {
      void compile(candidate, candidateId, controller.signal)
        .then((result) => {
          if (controller.signal.aborted || result.baseEtag !== etag) return;
          setValidation(
            result.validation.blocking.length
              ? { kind: "invalid" }
              : { kind: "valid", colorScheme: result.colorScheme },
          );
        })
        .catch((error: unknown) => {
          if (
            controller.signal.aborted ||
            (error instanceof Error && error.name === "AbortError")
          ) {
            return;
          }
          setValidation({ kind: "invalid" });
        });
    }, 180);
    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [candidate, compile, etag]);

  const recommendations = useMemo(
    () => ({
      accent: resource.registries.palette.items.flatMap((item) =>
        item.swatch.light?.primary ? [item.swatch.light.primary] : [],
      ),
      light: resource.registries.palette.items.flatMap((item) =>
        item.value.background.kind === "custom"
          ? [item.value.background.light]
          : [],
      ),
      dark: resource.registries.palette.items.flatMap((item) =>
        item.value.background.kind === "custom"
          ? [item.value.background.dark]
          : [],
      ),
    }),
    [resource.registries.palette.items],
  );

  const changeColor = useCallback(
    (field: "accent" | "light" | "dark", hex: string) => {
      onChange(
        field === "accent"
          ? { ...draft, accent: hex }
          : {
              ...draft,
              background: { ...draft.background, [field]: hex },
            },
      );
      return Promise.resolve();
    },
    [draft, onChange],
  );

  const buildCandidate = useCallback(
    (
      field: "accent" | "light" | "dark",
      hex: string,
    ): PersonalAppearanceOverrides => {
      const next =
        field === "accent"
          ? { ...draft, accent: hex }
          : {
              ...draft,
              background: { ...draft.background, [field]: hex },
            };
      return {
        ...resource.overrides,
        ...paletteFileToPersonalPalette(next),
      };
    },
    [draft, resource.overrides],
  );

  const preview = validation.kind === "valid" ? validation.colorScheme : null;
  const apply = async () => {
    if (validation.kind !== "valid" || applying) return;
    setApplying(true);
    try {
      await onApply(palette);
    } finally {
      setApplying(false);
    }
  };

  return (
    <section
      className={styles.paletteDraft}
      aria-labelledby="personal-palette-draft-title"
      data-testid="personal-palette-import-draft"
    >
      <header>
        <div>
          <span>{t("draftEyebrow")}</span>
          <h4 id="personal-palette-draft-title">{draft.name}</h4>
        </div>
        <span
          className={styles.paletteValidation}
          data-state={validation.kind}
          role={validation.kind === "invalid" ? "alert" : "status"}
        >
          {validation.kind === "checking"
            ? t("checking")
            : validation.kind === "valid"
              ? t("valid")
              : validation.kind === "invalid"
                ? t("invalid")
                : t("waiting")}
        </span>
      </header>

      {preview ? <ImportedPalettePreview colorScheme={preview} /> : null}

      <div className={styles.paletteDraftPickers}>
        <PersonalColorPicker
          label={t("accent")}
          value={draft.accent}
          mode="light"
          target="accent"
          etag={etag}
          recommendations={recommendations.accent}
          disabled={busy || applying}
          buildCandidate={(hex) => buildCandidate("accent", hex)}
          compile={compile}
          onApply={(hex) => changeColor("accent", hex)}
        />
        <PersonalColorPicker
          label={t("light")}
          value={draft.background.light}
          mode="light"
          target="background"
          etag={etag}
          recommendations={recommendations.light}
          disabled={busy || applying}
          buildCandidate={(hex) => buildCandidate("light", hex)}
          compile={compile}
          onApply={(hex) => changeColor("light", hex)}
        />
        <PersonalColorPicker
          label={t("dark")}
          value={draft.background.dark}
          mode="dark"
          target="background"
          etag={etag}
          recommendations={recommendations.dark}
          disabled={busy || applying}
          buildCandidate={(hex) => buildCandidate("dark", hex)}
          compile={compile}
          onApply={(hex) => changeColor("dark", hex)}
        />
      </div>

      <footer>
        <button type="button" disabled={applying} onClick={onCancel}>
          {t("cancel")}
        </button>
        <button
          type="button"
          className={styles.paletteApply}
          disabled={validation.kind !== "valid" || busy || applying}
          onClick={() => void apply().catch(() => undefined)}
        >
          {applying ? t("applying") : t("apply")}
        </button>
      </footer>
    </section>
  );
}

function ImportedPalettePreview({
  colorScheme,
}: {
  colorScheme: PersonalAppearanceResource["effective"]["colorScheme"];
}) {
  const t = useTranslations("settings.appearance.palette.import");
  return (
    <div className={styles.importedPalettePreview} aria-label={t("preview")}>
      {(["light", "dark"] as const).map((mode) => {
        const colors = colorScheme[mode];
        const style = {
          "--palette-bg": colors.background,
          "--palette-surface": colors.surface,
          "--palette-line": colors.line,
          "--palette-ink": colors.ink,
          "--palette-muted": colors.ink3,
          "--palette-accent": colors.accent,
          "--palette-accent-ink": colors.accentInk,
        } as CSSProperties;
        return (
          <div key={mode} style={style}>
            <span>{t(mode)}</span>
            <section>
              <i />
              <b>{t("sample")}</b>
              <small>{t("sampleBody")}</small>
              <em>{t("actionSample")}</em>
            </section>
          </div>
        );
      })}
    </div>
  );
}

function paletteMatches(
  resource: PersonalAppearanceResource,
  item: PersonalAppearanceRegistryItem<PersonalPalette>,
): boolean {
  return (["light", "dark"] as const).every((mode) => {
    const swatch = item.swatch[mode];
    const effective = resource.effective.colorScheme[mode];
    return Boolean(
      swatch &&
      swatch.primary === effective.accent &&
      swatch.secondary === effective.background,
    );
  });
}
