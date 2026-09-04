"use client";

import Link from "next/link";
import { ChevronRight, Play } from "lucide-react";
import { Fragment, useEffect, useState } from "react";
import { useI18n } from "@/components/i18n-provider";
import { DiagnosticAd } from "@/components/adsense/diagnostic-ad";
import { Hero } from "@/components/layout/hero";
import { CspImage } from "@/components/media/csp-image";
import {
  platformApiMessage,
  platformApiRequest
} from "@/lib/platform-api";
import { formatPatchDate, formatPatchTitle } from "@/lib/patch-format";
import type { PlatformHomeContent } from "@/lib/platform-types";

const dateFormatter = new Intl.DateTimeFormat("ru-RU", {
  day: "numeric",
  month: "long",
  year: "numeric"
});

const socialLinks = [
  { id: "youtube", label: "YouTube", href: "https://www.youtube.com/@deadlockOldSparky" },
  { id: "twitch", label: "Twitch", href: "https://www.twitch.tv/old_sparky" },
  { id: "discord", label: "Discord", href: "https://discord.com/invite/cWVh7fT" },
  { id: "telegram", label: "Telegram", href: "https://t.me/oldsparkydeadlock" },
  { id: "vk", label: "VK", href: "https://vk.ru/osdota" }
] as const;

const flowCards = [
  {
    id: "find",
    number: "01",
    titleKey: "home.flowOneTitle",
    copyKey: "home.flowOneCopy"
  },
  {
    id: "ready",
    number: "02",
    titleKey: "home.flowTwoTitle",
    copyKey: "home.flowTwoCopy"
  },
  {
    id: "team",
    number: "03",
    titleKey: "home.flowThreeTitle",
    copyKey: "home.flowThreeCopy"
  }
] as const;

const patchArtwork = [
  "/assets/preview/patch-featured.webp",
  "/assets/preview/patch-archive-city.webp",
  "/assets/preview/patch-archive-transit.webp",
  "/assets/preview/patch-archive-rift.webp"
] as const;

const NEW_PATCH_WINDOW_MS = 3 * 24 * 60 * 60 * 1000;

function isNewPatch(publishedAt: string, now = Date.now()) {
  const publishedTime = Date.parse(publishedAt);
  const age = now - publishedTime;
  return Number.isFinite(publishedTime) && age >= 0 && age < NEW_PATCH_WINDOW_MS;
}

export function HomePageContent() {
  const { t } = useI18n();
  const [content, setContent] = useState<PlatformHomeContent | null>(null);
  const [loadError, setLoadError] = useState("");

  useEffect(() => {
    let cancelled = false;
    void platformApiRequest<PlatformHomeContent>("/content/home")
      .then((payload) => {
        if (!cancelled) {
          setContent(payload);
        }
      })
      .catch((error) => {
        if (!cancelled) {
          setLoadError(platformApiMessage(error, t("home.contentLoadFailed")));
        }
      });
    return () => {
      cancelled = true;
    };
  }, [t]);

  return (
    <>
      <div className="page-noise" aria-hidden="true" />
      <Hero
        actions={(
          <>
            <Link className="hero-action hero-action-primary" href="/tournaments">
              {t("home.findTournament")}
            </Link>
            <Link className="hero-action hero-action-secondary" href="/tournaments/new">
              {t("home.createTournament")}
            </Link>
          </>
        )}
        title={t("home.title")}
        subtitle={t("home.subtitle")}
        variant="home"
      />
      <main className="main home-main">
        <section className="home-flow" aria-labelledby="home-flow-title">
          <h2 className="sr-only" id="home-flow-title">{t("home.flowTitle")}</h2>
          {flowCards.map((card, index) => (
            <Fragment key={card.id}>
              <article className="home-flow-card">
                <div className="home-flow-copy">
                  <span aria-hidden="true" className="home-flow-number">{card.number}</span>
                  <div className="home-flow-text">
                    <h3>{t(card.titleKey)}</h3>
                    <p>{t(card.copyKey)}</p>
                  </div>
                </div>
              </article>
              {index < flowCards.length - 1 ? (
                <span aria-hidden="true" className="home-flow-arrow">
                  <ChevronRight size={24} strokeWidth={1.6} />
                </span>
              ) : null}
            </Fragment>
          ))}
        </section>

        {loadError ? <div className="home-content-error" role="status">{loadError}</div> : null}

        <section className="home-content-group" aria-labelledby="home-patches-title">
          <HomeSectionHeading
            eyebrow={t("home.patchesEyebrow")}
            id="home-patches-title"
            title={t("home.patchesTitle")}
          />
          {!content && !loadError ? <HomePatchLoading /> : null}
          {content && content.patches.length === 0 ? (
            <div className="home-empty-state" role="status">{t("home.noPatches")}</div>
          ) : null}
          {content && content.patches.length > 0 ? (
            <div className="patch-showcase">
              <article className="patch-featured">
                {isNewPatch(content.patches[0].published_at) ? <span className="patch-new-ribbon">NEW</span> : null}
                <span aria-hidden="true" className="patch-featured-art">
                  <CspImage fill sizes="(max-width: 820px) 100vw, 60vw" src={patchArtwork[0]} alt="" />
                </span>
                <div className="patch-featured-copy">
                  <h3 className="patch-title">{formatPatchTitle(content.patches[0].title)}</h3>
                  <span className="patch-date">{formatPatchDate(content.patches[0].published_at)}</span>
                  <Link className="patch-read-action" href={`/patches/${content.patches[0].id}`}>
                    {t("home.readPatch")}
                    <ChevronRight aria-hidden="true" size={16} />
                  </Link>
                </div>
              </article>
              <div className="patch-archive">
                {content.patches.slice(1, 4).map((patch, index) => (
                  <Link className="patch-compact-card" href={`/patches/${patch.id}`} key={patch.id}>
                    <span aria-hidden="true" className="patch-compact-art">
                      <CspImage fill sizes="(max-width: 820px) 100vw, 32vw" src={patchArtwork[index + 1]} alt="" />
                    </span>
                    <span className="patch-compact-copy">
                      <strong className="patch-title">{formatPatchTitle(patch.title)}</strong>
                      <span className="patch-date">{formatPatchDate(patch.published_at)}</span>
                    </span>
                  </Link>
                ))}
              </div>
            </div>
          ) : null}
        </section>

        <section className="home-content-group" aria-labelledby="home-videos-title">
          <HomeSectionHeading
            eyebrow={t("home.videosEyebrow")}
            id="home-videos-title"
            title={t("home.videosTitle")}
          />
          {!content && !loadError ? <HomeVideoLoading count={4} /> : null}
          {content && content.videos.length === 0 ? (
            <div className="home-empty-state" role="status">{t("home.noVideos")}</div>
          ) : null}
          <div className="video-feed">
            {content?.videos.slice(0, 4).map((video) => (
              <a className="video-card" href={video.url} key={video.id} rel="noreferrer" target="_blank">
                <span className="video-thumbnail">
                  <CspImage alt="" height={270} src={video.thumbnail_url} width={480} />
                  <span className="video-play"><Play aria-hidden="true" fill="currentColor" size={18} /></span>
                </span>
                <span className="video-card-copy">
                  <strong>{video.title}</strong>
                  <span>{dateFormatter.format(new Date(video.published_at))}</span>
                </span>
              </a>
            ))}
          </div>
        </section>

        <nav className="home-socials" aria-labelledby="home-socials-title">
          <HomeSectionHeading
            eyebrow={t("home.socialsEyebrow")}
            id="home-socials-title"
            title={t("home.socialsTitle")}
          />
          <div className="home-socials-grid">
            {socialLinks.map((social) => (
              <a
                aria-label={social.label}
                className={`home-social-card home-social-${social.id}`}
                href={social.href}
                key={social.id}
                rel="noreferrer"
                target="_blank"
              >
                <span
                  aria-hidden="true"
                  className="home-social-logo"
                />
              </a>
            ))}
          </div>
        </nav>

        <DiagnosticAd />
      </main>
    </>
  );
}

function HomeSectionHeading({ eyebrow, id, title }: { eyebrow: string; id: string; title: string }) {
  return (
    <header className="home-section-heading">
      <span aria-hidden="true" className="home-section-accent" />
      <span className="home-section-heading-copy">
        <span className="home-section-eyebrow">{eyebrow}</span>
        <h2 id={id}>{title}</h2>
      </span>
    </header>
  );
}

function HomeVideoLoading({ count }: { count: number }) {
  return (
    <div className="home-loading-grid home-loading-grid-videos" aria-label="Загрузка" role="status">
      {Array.from({ length: count }, (_, index) => <span key={index} />)}
    </div>
  );
}

function HomePatchLoading() {
  return (
    <div aria-label="Загрузка" className="patch-showcase patch-showcase-loading" role="status">
      <span className="patch-featured" />
      <span className="patch-archive">
        {Array.from({ length: 3 }, (_, index) => <span className="patch-compact-card" key={index} />)}
      </span>
    </div>
  );
}
