"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useI18n } from "@/components/i18n-provider";
import { navItems } from "@/lib/routes";

const MOBILE_NAV =
  "max-[820px]:grid! max-[820px]:h-12! max-[820px]:w-full! max-[820px]:grid-cols-4! max-[820px]:gap-0! max-[820px]:overflow-x-visible!";
const MOBILE_NAV_LINK =
  "max-[820px]:h-12! max-[820px]:w-full! max-[820px]:justify-center! max-[820px]:px-0! max-[820px]:bg-transparent! max-[820px]:after:left-0! max-[820px]:after:right-0!";

export function SiteHeaderNavigation() {
  const { t } = useI18n();
  const pathname = usePathname();
  const isCreateTournament = pathname === "/tournaments/new";
  const exactActiveHref = navItems.find((item) => item.href === pathname)?.href;

  return (
    <nav className={`nav ${MOBILE_NAV}`} aria-label={t("header.mainNavigation")}>
      {navItems.map((item) => {
        const active = exactActiveHref
          ? item.href === exactActiveHref
          : Boolean(!isCreateTournament && item.matchPrefix && pathname.startsWith(item.matchPrefix));
        const className = `${active ? "nav-link active nav-link-active" : "nav-link"} ${MOBILE_NAV_LINK}`;
        if (item.hardNavigation) {
          return (
            <a key={item.href} className={className} href={item.href}>
              {item.label}
            </a>
          );
        }
        return (
          <Link key={item.href} className={className} href={item.href}>
            {item.label}
          </Link>
        );
      })}
    </nav>
  );
}
