const MAX_JSON_DEPTH = 64;
const MAX_JSON_NODES = 100_000;

export type EmbeddedDataProvenance = {
  status: "injected" | "computed_unchecked";
  sourceIndexes: number[];
  hasSources: boolean;
};

function isPlainObject(value: object): value is Record<string, unknown> {
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}

function isJsonValue(
  value: unknown,
  depth: number,
  ancestors: Set<object>,
  budget: { count: number },
): boolean {
  if (depth > MAX_JSON_DEPTH) return false;
  budget.count += 1;
  if (budget.count > MAX_JSON_NODES) return false;

  if (value === null || typeof value === "string" || typeof value === "boolean") return true;
  if (typeof value === "number") return Number.isFinite(value);
  if (typeof value !== "object") return false;

  if (ancestors.has(value)) return false;
  ancestors.add(value);
  try {
    if (Array.isArray(value)) {
      return value.every((item) => isJsonValue(item, depth + 1, ancestors, budget));
    }
    if (!isPlainObject(value)) return false;
    if (Object.getOwnPropertySymbols(value).length > 0) return false;
    return Object.values(value).every((item) => isJsonValue(item, depth + 1, ancestors, budget));
  } finally {
    ancestors.delete(value);
  }
}

function isNonemptyStructured(value: unknown): boolean {
  if (Array.isArray(value)) {
    return value.length > 0 && isJsonValue(value, 0, new Set(), { count: 0 });
  }
  if (value && typeof value === "object" && isPlainObject(value)) {
    return Object.keys(value).length > 0 && isJsonValue(value, 0, new Set(), { count: 0 });
  }
  return false;
}

function normalizeSourceIndexes(value: unknown): number[] {
  if (!Array.isArray(value)) return [];
  const seen = new Set<number>();
  const out: number[] = [];
  for (const item of value) {
    if (typeof item !== "number" || !Number.isSafeInteger(item) || item <= 0 || seen.has(item)) {
      continue;
    }
    seen.add(item);
    out.push(item);
  }
  return out;
}

export function projectEmbeddedDataProvenance(value: unknown): EmbeddedDataProvenance {
  const raw = value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
  const hasUnverified = Object.prototype.hasOwnProperty.call(raw, "unverified");
  const allowsInjection = !hasUnverified || raw.unverified === null || raw.unverified === false;
  const sourceIndexes = normalizeSourceIndexes(raw.sources);
  const status = allowsInjection && isNonemptyStructured(raw.data)
    ? "injected"
    : "computed_unchecked";
  return { status, sourceIndexes, hasSources: sourceIndexes.length > 0 };
}
