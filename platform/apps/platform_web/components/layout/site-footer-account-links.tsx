"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useI18n } from "@/components/i18n-provider";

const accountLinks = [
  { href: "/profile/me", labelKey: "footer.myProfile" },
  { href: "/auth/login", labelKey: "footer.signIn" },
  { href: "/auth/register", labelKey: "footer.createAccount" }
] as const;

export function SiteFooterAccountLinks() {
  const { t } = useI18n();
  const pathname = usePathname();

  return (
    <nav className="site-footer-nav" aria-labelledby="site-footer-account-title">
      <h2 id="site-footer-account-title">{t("footer.account")}</h2>
      <ul>
        {accountLinks.map((item) => (
          <li key={item.href}>
            <Link
              href={authHref(item.href, pathname)}
              prefetch={!item.href.startsWith("/auth/")}
            >
              {t(item.labelKey)}
            </Link>
          </li>
        ))}
      </ul>
    </nav>
  );
}

function authHref(href: string, pathname: string): string {
  if (!href.startsWith("/auth/") || pathname.startsWith("/auth/")) {
    return href;
  }
  return `${href}?returnTo=${encodeURIComponent(pathname)}`;
}
