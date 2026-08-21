import type { ImgHTMLAttributes } from "react";

export type CspImageProps = Omit<
  ImgHTMLAttributes<HTMLImageElement>,
  "alt" | "height" | "src" | "style" | "width"
> & {
  alt: string;
  fill?: boolean;
  height?: number;
  src: string;
  width?: number;
};

export function CspImage({
  alt,
  className,
  fill = false,
  height,
  loading,
  src,
  width,
  ...imageProps
}: CspImageProps) {
  const resolvedClassName = [className, fill ? "csp-image-fill" : ""]
    .filter(Boolean)
    .join(" ");

  // Direct native URLs avoid an optimizer/proxy hop and, critically, avoid
  // Next/Image's generated inline style attribute under strict CSP.
  // eslint-disable-next-line @next/next/no-img-element
  return <img
    {...imageProps}
    alt={alt}
    className={resolvedClassName || undefined}
    decoding={imageProps.decoding ?? "async"}
    fetchPriority={imageProps.fetchPriority}
    height={fill ? undefined : height}
    loading={loading ?? "lazy"}
    src={src}
    width={fill ? undefined : width}
  />;
}
