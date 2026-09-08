import Link from "next/link";
import { ExternalLink } from "lucide-react";
import { BrandMark } from "@/components/layout/brand-mark";
import { SiteFooterAccountLinks } from "@/components/layout/site-footer-account-links";
import { translate } from "@/lib/i18n";

const platformLinks = [
  { href: "/", labelKey: "footer.home" },
  { href: "/tournaments", labelKey: "footer.tournaments" },
  { href: "/tournaments/new", labelKey: "footer.createTournament" },
  { href: "/info", labelKey: "footer.info" },
  { href: "/privacy", labelKey: "footer.privacy" },
  { href: "/terms", labelKey: "footer.terms" }
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
            <p>{translate("footer.description")}</p>
          </section>

          <nav className="site-footer-nav" aria-labelledby="site-footer-platform-title">
            <h2 id="site-footer-platform-title">{translate("footer.platform")}</h2>
            <ul>
              {platformLinks.map((item) => (
                <li key={item.href}>
                  <Link href={item.href}>{translate(item.labelKey)}</Link>
                </li>
              ))}
            </ul>
          </nav>

          <SiteFooterAccountLinks />

          <nav className="site-footer-nav" aria-labelledby="site-footer-game-title">
            <h2 id="site-footer-game-title">{translate("footer.gameResources")}</h2>
            <ul>
              {gameLinks.map((item) => (
                <li key={item.href}>
                  <a href={item.href} rel="noreferrer" target="_blank">
                    <span>{translate(item.labelKey)}</span>
                    <span className="sr-only">{translate("footer.opensNewTab")}</span>
                    <ExternalLink aria-hidden="true" size={14} />
                  </a>
                </li>
              ))}
            </ul>
          </nav>
        </div>

        <div className="site-footer-bottom">
          <span>{translate("footer.copyright", { year: currentYear })}</span>
          <span>{translate("footer.valveDisclaimer")}</span>
        </div>
      </div>
    </footer>
  );
}
