#!/usr/bin/env node
/** Materialize the fixed frontend edition before Next.js or TypeScript reads source. */
import { createHash } from "node:crypto";
import { existsSync, lstatSync, mkdirSync, readFileSync, readdirSync, rmSync, writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const privateRoot = path.resolve(root, "../../enterprise/frontend");
const recordPath = path.join(root, ".edition-files.json");
const sha = (value) => createHash("sha256").update(value).digest("hex");
const readJson = (file) => JSON.parse(readFileSync(file, "utf8"));
const manifest = readJson(path.join(root, "build-manifest.json"));
if (
  Object.keys(manifest).sort().join(",") !== "edition,schema_version" ||
  manifest.schema_version !== 1 ||
  !["community", "business"].includes(manifest.edition)
) {
  throw new Error("Invalid frontend build manifest");
}
const edition = manifest.edition;
if (edition === "business" && !existsSync(privateRoot)) throw new Error("Business frontend source is missing");

function safeTarget(relative) {
  if (
    typeof relative !== "string" ||
    relative.includes("\\") ||
    relative.startsWith("/") ||
    relative.split("/").some((part) => !part || part === "." || part === "..")
  )
    throw new Error("Unsafe generated frontend path");
  const target = path.join(root, relative);
  let parent = target;
  while (parent !== root) {
    if (existsSync(parent) && lstatSync(parent).isSymbolicLink())
      throw new Error(`Generated frontend path is a symlink: ${relative}`);
    parent = path.dirname(parent);
  }
  return target;
}
function files(directory, prefix = "") {
  return readdirSync(directory, { withFileTypes: true })
    .sort((a, b) => a.name.localeCompare(b.name))
    .flatMap((entry) => {
      if (entry.isSymbolicLink()) throw new Error(`Edition source is a symlink: ${entry.name}`);
      const relative = prefix + entry.name;
      return entry.isDirectory() ? files(path.join(directory, entry.name), `${relative}/`) : [relative];
    });
}
function merge(base, extra) {
  const result = { ...base };
  for (const [key, value] of Object.entries(extra))
    result[key] = value && typeof value === "object" && !Array.isArray(value) ? merge(result[key] ?? {}, value) : value;
  return result;
}
function productCopy(value) {
  if (typeof value === "string") return value.replaceAll("Bowhead", "Bowra");
  if (Array.isArray(value)) return value.map(productCopy);
  if (value && typeof value === "object")
    return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, productCopy(item)]));
  return value;
}
const outputs = new Map();
for (const file of files(path.join(root, "edition/community"))) {
  if (file.includes(".test.") || file.startsWith("messages/")) continue;
  outputs.set(`edition/${file}`, readFileSync(path.join(root, "edition/community", file)));
}
if (edition === "business") {
  for (const file of files(privateRoot)) {
    if (file.startsWith("messages/")) continue;
    const target = file.startsWith("help/") ? `edition/help-sources/${file.slice(5)}` : file;
    outputs.set(target, readFileSync(path.join(privateRoot, file)));
  }
}
for (const file of files(path.join(root, "edition/shared-messages"))) {
  const shared = readJson(path.join(root, "edition/shared-messages", file));
  const communityPath = path.join(root, "edition/community/messages", file);
  const catalog =
    edition === "business"
      ? merge(shared, readJson(path.join(privateRoot, "messages", file)))
      : productCopy(merge(shared, existsSync(communityPath) ? readJson(communityPath) : {}));
  outputs.set(`messages/${file}`, Buffer.from(`${JSON.stringify(catalog, null, 2)}\n`));
}
const prior = existsSync(recordPath) ? readJson(recordPath) : { edition: null, files: {} };
for (const [relative, checksum] of Object.entries(prior.files)) {
  const target = safeTarget(relative);
  if (
    existsSync(target) &&
    sha(readFileSync(target)) !== checksum &&
    (!outputs.has(relative) || sha(readFileSync(target)) !== sha(outputs.get(relative)))
  )
    throw new Error(`Generated frontend file was edited; move the edit to its edition source: ${relative}`);
}
for (const relative of outputs.keys()) {
  const target = safeTarget(relative);
  if (existsSync(target) && !(relative in prior.files))
    throw new Error(`Edition output would overwrite an unowned file: ${relative}`);
}
for (const relative of Object.keys(prior.files)) if (!outputs.has(relative)) rmSync(safeTarget(relative));
if (prior.edition !== edition) {
  for (const directory of [".next", "out", "out-en-US", "out-zh-CN", "out-de-DE", "out-fr-FR"])
    rmSync(path.join(root, directory), { recursive: true, force: true });
  rmSync(path.join(root, "tsconfig.tsbuildinfo"), { force: true });
}
const checksums = {};
for (const [relative, bytes] of [...outputs.entries()].sort(([a], [b]) => a.localeCompare(b))) {
  const target = safeTarget(relative);
  mkdirSync(path.dirname(target), { recursive: true });
  if (!existsSync(target) || sha(readFileSync(target)) !== sha(bytes)) writeFileSync(target, bytes);
  checksums[relative] = sha(bytes);
}
writeFileSync(recordPath, `${JSON.stringify({ schema_version: 1, edition, files: checksums }, null, 2)}\n`);
console.log(`Assembled ${edition} frontend (${outputs.size} generated files).`);
