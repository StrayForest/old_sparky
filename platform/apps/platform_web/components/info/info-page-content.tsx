import { BookOpen, CheckCircle2, HelpCircle, Mail, ShieldCheck } from "lucide-react";
import { SupportForm } from "@/components/info/support-form";
import { Hero } from "@/components/layout/hero";
import { translate as t } from "@/lib/i18n";
import { platformApiRequest } from "@/lib/platform-api";

const guideKeys = ["account", "profile", "find", "ready", "distribution", "selection"] as const;
const ruleKeys = ["account", "profile", "fairPlay", "conduct", "timing", "result", "abuse", "moderation"] as const;
const faqKeys = ["manyTournaments", "notInTeam", "captains", "teamName", "busyPlayer", "ready", "private", "publicTournament", "privateAllowance"] as const;

async function loadSupportConfigured(): Promise<boolean> {
  try {
    const value = await platformApiRequest<{ configured: boolean }>("/content/support/status", {
      cache: "no-store",
      credentials: "omit"
    });
    return value.configured === true;
  } catch {
    return false;
  }
}

export async function InfoPageContent() {
  const supportConfigured = await loadSupportConfigured();

  return (
    <>
      <div className="page-noise" aria-hidden="true" />
      <Hero title={t("info.title")} subtitle={t("info.subtitle")} />
      <main className="main info-main">
        <nav className="info-anchor-nav" aria-label={t("info.sectionsLabel")}>
          <a href="#guide"><BookOpen aria-hidden="true" size={16} />{t("info.guideTitle")}</a>
          <a href="#rules"><ShieldCheck aria-hidden="true" size={16} />{t("info.rulesTitle")}</a>
          <a href="#faq"><HelpCircle aria-hidden="true" size={16} />{t("info.faqTitle")}</a>
          <a href="#support"><Mail aria-hidden="true" size={16} />{t("info.supportTitle")}</a>
        </nav>

        <section className="info-section" id="guide">
          <div className="info-section-heading"><span>01</span><div><h2>{t("info.guideTitle")}</h2><p>{t("info.guideCopy")}</p></div></div>
          <ol className="info-guide-grid">
            {guideKeys.map((key, index) => (
              <li key={key}><span>{String(index + 1).padStart(2, "0")}</span><strong>{t(`info.guide.${key}.title`)}</strong><p>{t(`info.guide.${key}.copy`)}</p></li>
            ))}
          </ol>
        </section>

        <section className="info-section" id="rules">
          <div className="info-section-heading"><span>02</span><div><h2>{t("info.rulesTitle")}</h2><p>{t("info.rulesCopy")}</p></div></div>
          <div className="info-rules-list">
            {ruleKeys.map((key) => (
              <article key={key}><CheckCircle2 aria-hidden="true" size={18} /><div><strong>{t(`info.rules.${key}.title`)}</strong><p>{t(`info.rules.${key}.copy`)}</p></div></article>
            ))}
          </div>
        </section>

        <section className="info-section" id="faq">
          <div className="info-section-heading"><span>03</span><div><h2>{t("info.faqTitle")}</h2><p>{t("info.faqCopy")}</p></div></div>
          <div className="info-faq-list">
            {faqKeys.map((key) => (
              <details key={key}><summary>{t(`info.faq.${key}.question`)}</summary><p>{t(`info.faq.${key}.answer`)}</p></details>
            ))}
          </div>
        </section>

        <section className="info-section support-section" id="support">
          <div className="info-section-heading"><span>04</span><div><h2>{t("info.supportTitle")}</h2><p>{t("info.supportCopy")}</p></div></div>
          <SupportForm supportConfigured={supportConfigured} />
        </section>
      </main>
    </>
  );
}
