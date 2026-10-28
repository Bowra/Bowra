#!/usr/bin/env node
/**
 * Copies the repository's THIRD_PARTY_NOTICES.md into the static export as
 * public/legal/third-party-notices.txt, linked from Help › About ("Third-party licenses").
 *
 * Runs before `next build` (npm `prebuild`). The notices file is generated and checked by
 * scripts/check_third_party_notices.py at the repository root; this script only publishes it.
 * Lookup order: the frontend package directory (the image build copies the file there), then the
 * repository root. A missing file fails the build so the published link never points at nothing.
 * Served as .txt so browsers display it inline instead of downloading Markdown.
 */
import { copyFileSync, existsSync, mkdirSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const packageDir = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const candidates = [
  path.join(packageDir, "THIRD_PARTY_NOTICES.md"),
  path.resolve(packageDir, "../../THIRD_PARTY_NOTICES.md"),
];
const source = candidates.find((candidate) => existsSync(candidate));
if (!source) {
  console.error(
    `copy-legal-notices: THIRD_PARTY_NOTICES.md not found (looked in ${candidates.join(", ")})`,
  );
  process.exit(1);
}
const target = path.join(packageDir, "public", "legal", "third-party-notices.txt");
mkdirSync(path.dirname(target), { recursive: true });
copyFileSync(source, target);
console.log(`copy-legal-notices: ${path.relative(packageDir, source)} -> public/legal/third-party-notices.txt`);
