import { CspImage } from "@/components/media/csp-image";

export function BrandMark() {
  return (
    <CspImage
      alt=""
      aria-hidden="true"
      className="brand-mark"
      height={256}
      src="/assets/main_logo/old-sparky-arena-logo-v3.webp"
      sizes="(max-width: 520px) 36px, (max-width: 820px) 42px, 54px"
      srcSet="/assets/main_logo/old-sparky-arena-logo-v3-64.webp 64w, /assets/main_logo/old-sparky-arena-logo-v3-128.webp 128w"
      width={256}
    />
  );
}
