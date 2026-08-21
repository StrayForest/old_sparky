"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { ExternalLink } from "lucide-react";
import { BrandMark } from "@/components/layout/brand-mark";
import { useI18n } from "@/components/i18n-provider";

const platformLinks = [
  { href: "/", labelKey: "footer.home" },
  { href: "/tournaments", labelKey: "footer.tournaments" },
  { href: "/tournaments/new", labelKey: "footer.createTournament" },
  { href: "/info", labelKey: "footer.info" },
  { href: "/privacy", labelKey: "footer.privacy" },
  { href: "/terms", labelKey: "footer.terms" }
] as const;

const accountLinks = [
  { href: "/profile/me", labelKey: "footer.myProfile" },
  { href: "/auth/login", labelKey: "footer.signIn" },
  { href: "/auth/register", labelKey: "footer.createAccount" }
] as const;

const gameLinks = [
  {
    href: "https://www.playdeadlock.com/",
    labelKey: "footer.officialGameSite"
  },
  {
    href: "https://forums.playdeadlock.com/",
    labelKey: "footer.officialForum"
  }
] as const;

export function SiteFooter() {
  const { t } = useI18n();
  const pathname = usePathname();
  const currentYear = new Date().getUTCFullYear();

  return (
    <footer className="site-footer">
      <div className="site-footer-inner">
        <div className="site-footer-grid">
          <section className="site-footer-brand" aria-labelledby="site-footer-brand-title">
            <Link className="footer-brand-link" href="/" aria-label="Old Sparky Arena — главная">
              <BrandMark />
              <span className="brand-text">
                <span className="brand-title" id="site-footer-brand-title">OLD SPARKY</span>
                <span className="brand-sub">ARENA</span>
              </span>
            </Link>
            <p>{t("footer.description")}</p>
          </section>

          <nav className="site-footer-nav" aria-labelledby="site-footer-platform-title">
            <h2 id="site-footer-platform-title">{t("footer.platform")}</h2>
            <ul>
              {platformLinks.map((item) => (
                <li key={item.href}>
                  <Link href={item.href}>{t(item.labelKey)}</Link>
                </li>
              ))}
            </ul>
          </nav>

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

          <nav className="site-footer-nav" aria-labelledby="site-footer-game-title">
            <h2 id="site-footer-game-title">{t("footer.gameResources")}</h2>
            <ul>
              {gameLinks.map((item) => (
                <li key={item.href}>
                  <a href={item.href} rel="noreferrer" target="_blank">
                    <span>{t(item.labelKey)}</span>
                    <span className="sr-only">{t("footer.opensNewTab")}</span>
                    <ExternalLink aria-hidden="true" size={14} />
                  </a>
                </li>
              ))}
            </ul>
          </nav>
        </div>

        <div className="site-footer-bottom">
          <span>{t("footer.copyright", { year: currentYear })}</span>
          <span>{t("footer.valveDisclaimer")}</span>
        </div>
      </div>
    </footer>
  );
}

function authHref(href: string, pathname: string): string {
  if (!href.startsWith("/auth/") || pathname.startsWith("/auth/")) {
    return href;
  }
  return `${href}?returnTo=${encodeURIComponent(pathname)}`;
}
