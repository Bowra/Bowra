import type { LoginCapabilities } from "./auth-api";
export interface EditionLoginProps {
  available: boolean;
  pending: boolean;
  disabled: boolean;
  next: string;
  onPending: () => void;
  onError?: (error: unknown) => void;
}
export type EditionLoginCapabilities = LoginCapabilities;
