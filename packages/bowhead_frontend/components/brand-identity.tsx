"use client";

import type { ImgHTMLAttributes } from "react";

import { BowheadLogo } from "@/components/bowhead-logo";
import { useBrand } from "@/components/brand-provider";
import { appPath } from "@/lib/base-path";
import {
  isCanonicalBrandBundle,
  type BrandRenderable,
} from "@/lib/theme-config";

export function BrandGraphic({
  variant = "logo",
  size = 32,
  className,
}: {
  variant?: "logo" | "mark";
  size?: number;
  className?: string;
}) {
  const { brand, bundle } = useBrand();
  // Team color/typography customization must not silently replace the Bowhead
  // mark with a generated "B". A supplied asset still wins below.
  const fallbackBowhead =
    isCanonicalBrandBundle(bundle) ||
    ["bowhead", "bowra"].includes(brand.applicationName.trim().toLowerCase());
  if (variant === "mark") {
    return (
      <RenderableGraphic
        renderable={brand.mark}
        applicationName={brand.applicationName}
        alt=""
        size={size}
        className={className}
        fallbackBowhead={fallbackBowhead}
      />
    );
  }
  return (
    <span
      className={["brand-graphic brand-graphic--logo", className]
        .filter(Boolean)
        .join(" ")}
      aria-hidden="true"
    >
      <RenderableGraphic
        renderable={brand.logoOnLight}
        applicationName={brand.applicationName}
        alt=""
        size={size}
        className="brand-graphic__light"
        fallbackBowhead={fallbackBowhead}
      />
      <RenderableGraphic
        renderable={brand.logoOnDark}
        applicationName={brand.applicationName}
        alt=""
        size={size}
        className="brand-graphic__dark"
        fallbackBowhead={fallbackBowhead}
      />
    </span>
  );
}

export function BrandName({ className }: { className?: string }) {
  const { brand } = useBrand();
  return (
    <span className={className} translate="no">
      {brand.applicationName}
    </span>
  );
}

export function BrandIdentity({
  variant = "logo",
  size = 32,
  className,
  graphicClassName,
  nameClassName,
  showName = true,
}: {
  variant?: "logo" | "mark";
  size?: number;
  className?: string;
  graphicClassName?: string;
  nameClassName?: string;
  showName?: boolean;
}) {
  const { brand } = useBrand();
  return (
    <span
      className={["brand-identity", className].filter(Boolean).join(" ")}
      aria-label={showName ? undefined : brand.applicationName}
    >
      <BrandGraphic
        variant={variant}
        size={size}
        className={graphicClassName}
      />
      {showName ? <BrandName className={nameClassName} /> : null}
    </span>
  );
}

export function BrandedText({ children }: { children: string }) {
  const { brand } = useBrand();
  // Function form, so `$&`, `$1` and the like in the application name are not treated as replacement patterns.
  return <>{children.replaceAll("Bowhead", () => brand.applicationName)}</>;
}

function RenderableGraphic({
  renderable,
  applicationName,
  alt,
  size,
  className,
  fallbackBowhead,
}: {
  renderable: BrandRenderable;
  applicationName: string;
  alt: string;
  size: number;
  className?: string;
  fallbackBowhead: boolean;
}) {
  if (renderable.kind === "asset") {
    const imageProps: ImgHTMLAttributes<HTMLImageElement> = {
      src: appPath(renderable.path),
      alt,
      className,
      style: { maxHeight: size, maxWidth: Math.max(size, size * 4) },
    };
    return <img {...imageProps} />;
  }
  if (fallbackBowhead) {
    return (
      <BowheadLogo
        size={size}
        className={className}
        aria-hidden="true"
        focusable="false"
      />
    );
  }
  const initial =
    renderable.kind === "generatedInitial"
      ? renderable.text
      : (Array.from(applicationName)[0] ?? "B");
  return (
    <span
      className={["brand-graphic__initial", className]
        .filter(Boolean)
        .join(" ")}
      style={{ width: size, height: size }}
      aria-hidden="true"
    >
      {initial}
    </span>
  );
}
