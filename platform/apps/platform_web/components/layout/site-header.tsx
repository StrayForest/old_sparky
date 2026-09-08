import Link from "next/link";
import { BrandMark } from "@/components/layout/brand-mark";
import { SiteHeaderAuthActions } from "@/components/layout/site-header-auth-actions";
import { SiteHeaderNavigation } from "@/components/layout/site-header-navigation";
import { translate } from "@/lib/i18n";

const MOBILE_BRAND =
  "max-[820px]:w-auto! max-[820px]:gap-3! max-[820px]:[&_.brand-mark]:h-11! max-[820px]:[&_.brand-mark]:w-11! max-[820px]:[&_.brand-title]:text-[15px]! max-[820px]:[&_.brand-title]:tracking-[.08em]! max-[820px]:[&_.brand-sub]:text-[8px]!";

export function SiteHeader() {
  return (
    <header className="site-header">
      <div className="header-inner">
        <Link className={`brand ${MOBILE_BRAND}`} href="/" aria-label={translate("header.homeLabel")}>
          <BrandMark />
          <span className="brand-text">
            <span className="brand-title">OLD SPARKY</span>
            <span className="brand-sub">ARENA</span>
          </span>
        </Link>
        <SiteHeaderNavigation />
        <SiteHeaderAuthActions />
      </div>
    </header>
  );
}
