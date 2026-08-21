"use client";

import { useEffect, useMemo, useState } from "react";
import type { PlatformMediaDescriptor, PlatformMediaVariant } from "@/lib/platform-types";

type PreparedMediaProps = {
  alt: string;
  className?: string;
  descriptor?: PlatformMediaDescriptor | null;
  fallbackUrl?: string | null;
  height?: number;
  loading?: "eager" | "lazy";
  priority?: boolean;
  sizes: string;
  width?: number;
};

export function PreparedMedia({
  alt,
  className,
  descriptor,
  fallbackUrl,
  height,
  loading = "lazy",
  priority = false,
  sizes,
  width
}: PreparedMediaProps) {
  const preparedVariants = useMemo(
    () => normalizePreparedVariants(descriptor),
    [descriptor]
  );
  const preparedUrl = preparedVariants.at(-1)?.url ?? null;
  const [preparedFailed, setPreparedFailed] = useState(false);
  const sourceUrl = !preparedFailed && preparedUrl ? preparedUrl : fallbackUrl ?? null;
  const usePreparedSource = !preparedFailed && Boolean(preparedUrl && sourceUrl === preparedUrl);

  useEffect(() => {
    setPreparedFailed(false);
  }, [descriptor?.asset_id, preparedUrl]);

  if (!sourceUrl) {
    return null;
  }

  const srcSet = usePreparedSource
    ? preparedVariants.map((variant) => `${variant.url} ${variant.width}w`).join(", ")
    : undefined;

  // Prepared variants already come from the media CDN. Native srcSet keeps those
  // immutable URLs direct and avoids another optimization/proxy hop.
  // eslint-disable-next-line @next/next/no-img-element
  return <img
    alt={alt}
    className={className}
    data-media-asset-id={usePreparedSource ? descriptor?.asset_id : undefined}
    data-media-source={usePreparedSource ? "prepared" : "legacy"}
    decoding="async"
    fetchPriority={priority ? "high" : "auto"}
    height={height}
    loading={priority ? "eager" : loading}
    onError={() => {
      if (usePreparedSource) {
        setPreparedFailed(true);
      }
    }}
    sizes={srcSet ? sizes : undefined}
    src={sourceUrl}
    srcSet={srcSet}
    width={width}
  />;
}

function normalizePreparedVariants(
  descriptor: PlatformMediaDescriptor | null | undefined
): PlatformMediaVariant[] {
  if (descriptor?.status !== "ready") {
    return [];
  }
  const byWidth = new Map<number, PlatformMediaVariant>();
  for (const variant of descriptor.variants ?? []) {
    if (
      Number.isFinite(variant.width)
      && variant.width > 0
      && typeof variant.url === "string"
      && variant.url.length > 0
    ) {
      byWidth.set(variant.width, variant);
    }
  }
  return [...byWidth.values()].sort((left, right) => left.width - right.width);
}
