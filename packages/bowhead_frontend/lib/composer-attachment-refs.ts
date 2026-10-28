import { canonicalizeChartSelectedContext } from "./chart/selected-context";
import type { AttachmentRef } from "./types";

export const MAX_COMPOSER_RUNTIME_ATTACHMENTS = 100;

export function canonicalizeComposerAttachmentRef(
  value: unknown,
): AttachmentRef | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const attachment = value as Record<string, unknown>;
  const allowed = new Set([
    "bundleArtifactIds",
    "fileId",
    "filename",
    "mime",
    "referenceKind",
    "size",
    "selectedContext",
  ]);
  if (Object.keys(attachment).some((key) => !allowed.has(key))) return null;
  if (
    typeof attachment.fileId !== "string"
    || attachment.fileId.length < 1
    || attachment.fileId.length > 128
    || typeof attachment.filename !== "string"
    || attachment.filename.length < 1
    || attachment.filename.length > 512
    || typeof attachment.mime !== "string"
    || typeof attachment.size !== "number"
    || !Number.isSafeInteger(attachment.size)
    || attachment.size < 0
    || (
      attachment.referenceKind !== undefined
      && attachment.referenceKind !== "upload"
      && attachment.referenceKind !== "result"
    )
  ) return null;
  if (
    attachment.bundleArtifactIds !== undefined
    && (
      !Array.isArray(attachment.bundleArtifactIds)
      || attachment.bundleArtifactIds.length > MAX_COMPOSER_RUNTIME_ATTACHMENTS
      || !attachment.bundleArtifactIds.every((id) => (
      typeof id === "string" && id.length >= 1 && id.length <= 128
      ))
    )
  ) return null;
  const selectedContext = attachment.selectedContext === undefined ? undefined : canonicalizeChartSelectedContext(attachment.selectedContext);
  if (selectedContext === null || (selectedContext && (attachment.referenceKind !== "result" || (attachment.bundleArtifactIds as string[] | undefined)?.length))) return null;
  return {
    ...(selectedContext ? { selectedContext } : {}),
    fileId: attachment.fileId,
    filename: attachment.filename,
    size: attachment.size,
    mime: (attachment.mime || "application/octet-stream").slice(0, 200),
    ...(attachment.referenceKind
      ? { referenceKind: attachment.referenceKind }
      : {}),
    ...(attachment.bundleArtifactIds
      ? { bundleArtifactIds: [...attachment.bundleArtifactIds] as string[] }
      : {}),
  };
}

export function canonicalizeComposerAttachmentRefs(
  value: unknown,
): AttachmentRef[] | null {
  if (!Array.isArray(value) || value.length > 32) return null;
  const attachments: AttachmentRef[] = [];
  for (const item of value) {
    const attachment = canonicalizeComposerAttachmentRef(item);
    if (!attachment) return null;
    attachments.push(attachment);
  }
  return attachments;
}
