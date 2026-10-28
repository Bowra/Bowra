import type { EditionLoginCapabilities, EditionLoginProps } from "@/lib/edition-login";
export function EditionLoginButton(_props: EditionLoginProps) {
  return null;
}
export function editionLoginAvailable(_capabilities: EditionLoginCapabilities): boolean {
  return false;
}
export function editionLoginError(_code: string | undefined): string | null {
  return null;
}
