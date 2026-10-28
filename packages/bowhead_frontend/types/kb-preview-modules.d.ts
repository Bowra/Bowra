/**
 * Module declarations for V7 KB preview modules without first-party types.
 *
 * - pdfjs-dist worker URL import: Next.js webpack handles ?url suffix → string export
 */

declare module "pdfjs-dist/build/pdf.worker.min.mjs?url" {
  const src: string;
  export default src;
}
