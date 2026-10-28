"use client";

/**
 * Route-segment error boundary: map render/chunk failures to a bounded, localized recovery UI.
 * Raw error.message/stack never enters the DOM, copy payload or this component's console events.
 */
import { BoundaryErrorPage } from "@/components/boundary-error";
import { boundaryBuildLocale } from "@/lib/boundary-error-copy";

export default function RouteError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  return (
    <BoundaryErrorPage
      error={error}
      reset={reset}
      surface="route"
      locale={boundaryBuildLocale()}
    />
  );
}
