/**
 * Team frame options: how frames are drawn for every member of a Team. The Team sets them in
 * Brand & Appearance; there is no personal override. Each field's first value is the product
 * look, which is never transmitted or written to the page, so an absent field means "unchanged".
 * The matching rules live in styles/theme-frame.css.
 */
export const FRAME_OPTION_VALUES = {
  corners: ["radius", "square"],
  border: ["standard", "bold"],
  elevation: ["soft", "hard", "flat"],
  lineContrast: ["standard", "strong"],
} as const;

export type FrameOptionField = keyof typeof FRAME_OPTION_VALUES;
export type FrameOptionValue<F extends FrameOptionField> =
  (typeof FRAME_OPTION_VALUES)[F][number];
export type ThemeFrameOptions = {
  [F in FrameOptionField]?: Exclude<FrameOptionValue<F>, (typeof FRAME_OPTION_VALUES)[F][0]>;
};

export const FRAME_OPTION_FIELDS = Object.keys(FRAME_OPTION_VALUES) as FrameOptionField[];

const DATASET_KEYS: Record<FrameOptionField, string> = {
  corners: "frameCorners",
  border: "frameBorder",
  elevation: "frameElevation",
  lineContrast: "frameLineContrast",
};

/**
 * The same table for the pre-hydration bootstrap script, which cannot import modules:
 * field -> [dataset key, values that differ from the product look].
 */
export const FRAME_OPTION_BOOTSTRAP_TABLE: Record<string, [string, string[]]> =
  Object.fromEntries(
    FRAME_OPTION_FIELDS.map((field) => [
      field,
      [DATASET_KEYS[field], FRAME_OPTION_VALUES[field].slice(1)],
    ]),
  );

/** Keep known fields set to a non-default value; anything else is dropped. */
export function normalizeFrameOptions(value: unknown): ThemeFrameOptions {
  if (!value || typeof value !== "object" || Array.isArray(value)) return {};
  const raw = value as Record<string, unknown>;
  const normalized: Record<string, string> = {};
  for (const field of FRAME_OPTION_FIELDS) {
    const choice = raw[field];
    const allowed: readonly string[] = FRAME_OPTION_VALUES[field];
    if (typeof choice === "string" && allowed.includes(choice) && choice !== allowed[0]) {
      normalized[field] = choice;
    }
  }
  return normalized as ThemeFrameOptions;
}

/** Write the options to <html> as data attributes, removing those left at the product look. */
export function applyFrameOptionsToDoc(frame: ThemeFrameOptions | undefined): void {
  if (typeof document === "undefined") return;
  const root = document.documentElement;
  for (const field of FRAME_OPTION_FIELDS) {
    const choice = frame?.[field];
    if (choice) root.dataset[DATASET_KEYS[field]] = choice;
    else delete root.dataset[DATASET_KEYS[field]];
  }
}
