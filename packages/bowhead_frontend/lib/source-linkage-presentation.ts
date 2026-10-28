export const MAX_JSON_SAFE_INTEGER = Number.MAX_SAFE_INTEGER;

export type SourceLinkagePresentation = {
  citationCount: number | undefined;
  figureReviewCount: number | undefined;
  sourceCount: number | undefined;
  visible: boolean;
};

export function normalizeNonnegativeCount(value: unknown): number | undefined {
  if (typeof value !== "number" || !Number.isSafeInteger(value) || value < 0) {
    return undefined;
  }
  return value === 0 ? 0 : value;
}

export function projectSourceLinkage(value: unknown): SourceLinkagePresentation {
  const raw = value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
  const citationCount = normalizeNonnegativeCount(raw.verified);
  const figureReviewCount = normalizeNonnegativeCount(raw.unverified);
  const sourceCount = normalizeNonnegativeCount(raw.sources);
  return {
    citationCount,
    figureReviewCount,
    sourceCount,
    visible: [citationCount, figureReviewCount, sourceCount].some(
      (item) => item !== undefined && item > 0,
    ),
  };
}
