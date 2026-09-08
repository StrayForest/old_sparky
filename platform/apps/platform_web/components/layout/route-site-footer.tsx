"use client";

import dynamic from "next/dynamic";
import { usePathname } from "next/navigation";
import { SiteFooter } from "@/components/layout/site-footer";

const DeferredTournamentSiteFooter = dynamic(
  () => import("@/components/layout/site-footer").then((module) => module.SiteFooter),
  {
    ssr: false,
    loading: () => null
  }
);

export function RouteSiteFooter() {
  const pathname = usePathname();

  if (/^\/tournaments\/[^/]+\/?$/u.test(pathname)) {
    return <DeferredTournamentSiteFooter />;
  }

  return <SiteFooter />;
}
