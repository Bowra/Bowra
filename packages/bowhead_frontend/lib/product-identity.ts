import manifest from "@/build-manifest.json";

/** Fixed artifact identity. Runtime deployment settings never select an edition. */
export const BUILD_EDITION = manifest.edition as "community" | "business";
export const PRODUCT_NAME = BUILD_EDITION === "community" ? "Bowra" : "Bowhead";
