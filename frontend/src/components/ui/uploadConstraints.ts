import type { UploadContentType } from "../../api/client";

/** 20 MB cap, per docs/api.md POST /recipes/upload. */
export const MAX_UPLOAD_BYTES = 20 * 1024 * 1024;

/** Accepted MIME types for upload (docs/api.md). */
export const ACCEPTED_TYPES: UploadContentType[] = [
  "image/jpeg",
  "image/png",
  "image/heic",
  "application/pdf",
];

/** The `accept` attribute value for the file input (MIME types + extensions). */
export const ACCEPT_ATTR = [
  ...ACCEPTED_TYPES,
  ".heic",
  ".pdf",
  ".jpg",
  ".jpeg",
  ".png",
].join(",");

/** Validate a picked file against the accepted types and size cap. */
export function validateUploadFile(file: File): string | null {
  // Some browsers report an empty/odd type for .heic — fall back to extension.
  const type = file.type as UploadContentType;
  const byExt = /\.(jpe?g|png|heic|pdf)$/i.test(file.name);
  if (!ACCEPTED_TYPES.includes(type) && !byExt) {
    return "Unsupported file type. Use JPEG, PNG, HEIC, or PDF.";
  }
  if (file.size > MAX_UPLOAD_BYTES) {
    return "File is too large. The maximum size is 20 MB.";
  }
  return null;
}
