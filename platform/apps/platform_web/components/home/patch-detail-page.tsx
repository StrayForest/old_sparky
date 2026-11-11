"use client";

import { Landmark, Package, Sparkles, Target } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { useI18n } from "@/components/i18n-provider";
import { Hero } from "@/components/layout/hero";
import { CspImage } from "@/components/media/csp-image";
import { DeadlockHeroImage } from "@/components/media/deadlock-hero-image";
import { formatPatchDate, formatPatchTitle } from "@/lib/patch-format";
import { platformApiMessage, platformApiRequest } from "@/lib/platform-api";
import type { PlatformPatchDetail } from "@/lib/platform-types";

const steamPatchImagePattern = /^https:\/\/clan\.fastly\.steamstatic\.com\/images\/\d+\/[a-f0-9]{32,64}\.(?:avif|gif|jpe?g|png|webp)$/i;

export function PatchDetailPage({ patchId }: { patchId: string }) {
  const { t } = useI18n();
  const [patch, setPatch] = useState<PlatformPatchDetail | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    const controller = new AbortController();
    void platformApiRequest<PlatformPatchDetail>(`/content/patches/${patchId}`, {
      signal: controller.signal
    })
      .then(setPatch)
      .catch((requestError) => {
        if (!controller.signal.aborted) {
          setError(platformApiMessage(requestError, t("patch.loadFailed")));
        }
      });
    return () => controller.abort();
  }, [patchId, t]);

  return (
    <>
      <div className="page-noise" aria-hidden="true" />
      <Hero
        eyebrow={t("patch.eyebrow")}
        title={patch ? `${formatPatchTitle(patch.title)} - ${formatPatchDate(patch.published_at)}` : t("patch.title")}
        subtitle={patch ? undefined : t("patch.loading")}
      />
      <main className="main patch-page-main">
        {error ? <div className="home-content-error" role="alert">{error}</div> : null}
        {!patch && !error ? <PatchPageSkeleton /> : null}
        {patch ? (
          <article className="patch-article">
            <div className="patch-article-cover" aria-hidden="true" />
            <div className="patch-sections">
              {patch.sections.map((section, sectionIndex) => (
                <section className={`patch-section ${section.kind}-patch-section`} key={`${section.title}-${sectionIndex}`}>
                  <header className="patch-section-header">
                    {section.hero_name ? (
                      <DeadlockHeroImage
                        alt=""
                        heroName={section.hero_name}
                        className="patch-hero-image"
                        height={88}
                        imageUrl={section.hero_image_url}
                        width={88}
                      />
                    ) : section.item_icon_url ? (
                      <CspImage
                        alt=""
                        className="patch-item-image"
                        height={72}
                        src={section.item_icon_url}
                        width={72}
                      />
                    ) : section.kind === "objective" ? (
                      <ObjectiveIcon iconUrl={section.objective_icon_url ?? null} />
                    ) : (
                      <span className="patch-general-icon">
                        {section.kind === "item" ? <Package aria-hidden="true" size={28} /> : <Landmark aria-hidden="true" size={28} />}
                      </span>
                    )}
                    <div>
                      {section.kind === "general" ? <span>{t("patch.generalChanges")}</span> : null}
                      <h2>{section.objective_key === "urn"
                        ? t("patch.objective.urn")
                        : section.objective_key === "unstable_rift"
                          ? t("patch.objective.unstableRift")
                          : section.objective_key === "mid_boss"
                            ? "Mid Boss"
                          : section.title}</h2>
                      {section.kind === "hero" && section.hero_presentation_kind === "introduction" && section.hero_description ? (
                        <p className="patch-hero-description">{section.hero_description}</p>
                      ) : null}
                      {section.kind === "hero" && section.hero_presentation_kind === "introduction" && section.hero_metadata_pending ? (
                        <p className="patch-hero-description">{t("patch.heroMetadataPending")}</p>
                      ) : null}
                    </div>
                  </header>
                  {section.changes.length ? <ChangeList changes={section.changes} /> : null}
                  {section.abilities.length ? (
                    <div className="patch-abilities">
                      {section.abilities.map((ability) => (
                        <section className="patch-ability" key={ability.name}>
                          <header>
                            <PatchAbilityIcon iconUrl={ability.icon_url} />
                            <h3>{ability.display_name || ability.name}</h3>
                          </header>
                          {ability.presentation_kind === "introduction" || ability.presentation_kind === "replacement" ? (
                            <>
                              {ability.description ? <p className="patch-ability-description">{ability.description}</p> : null}
                              {ability.stats?.length ? (
                                <>
                                  <h4 className="patch-ability-stats-title">{t("patch.abilityCurrentStats")}</h4>
                                  <dl className="patch-ability-stats">
                                    {ability.stats.map((stat, statIndex) => (
                                      <div key={`${stat.label}-${statIndex}`}>
                                        <dt>{stat.label}</dt>
                                        <dd>{stat.value}</dd>
                                      </div>
                                    ))}
                                  </dl>
                                </>
                              ) : null}
                              {ability.stats_pending ? <p className="patch-ability-stats-pending">{t("patch.abilityStatsPending")}</p> : null}
                            </>
                          ) : null}
                          {ability.changes.length ? <ChangeList changes={ability.changes} /> : null}
                        </section>
                      ))}
                    </div>
                  ) : null}
                </section>
              ))}
              {patch.sections.length === 0 ? <p className="patch-raw-content">{patch.content}</p> : null}
            </div>
          </article>
        ) : null}
      </main>
    </>
  );
}

function PatchAbilityIcon({ iconUrl }: { iconUrl: string | null }) {
  const imageRef = useRef<HTMLImageElement>(null);
  const [failedSource, setFailedSource] = useState<string | null>(null);
  const failed = Boolean(iconUrl && failedSource === iconUrl);

  useEffect(() => {
    const image = imageRef.current;
    if (iconUrl && image?.complete && image.naturalWidth === 0 && image.currentSrc) {
      setFailedSource(iconUrl);
    }
  }, [iconUrl]);

  if (!iconUrl || failed) {
    return <span className="patch-ability-icon-fallback" aria-hidden="true"><Sparkles size={20} /></span>;
  }
  return (
    <CspImage
      alt=""
      className="patch-ability-icon"
      height={48}
      onError={() => setFailedSource(iconUrl)}
      ref={imageRef}
      src={iconUrl}
      width={48}
    />
  );
}

function ObjectiveIcon({ iconUrl }: { iconUrl: string | null }) {
  const [failed, setFailed] = useState(false);
  if (!iconUrl || failed) {
    return (
      <span className="patch-objective-fallback" data-objective-icon="fallback">
        <Target aria-hidden="true" size={30} />
      </span>
    );
  }
  return (
    <CspImage
      alt=""
      className="patch-objective-image"
      data-objective-icon="source"
      height={72}
      onError={() => setFailed(true)}
      src={iconUrl}
      width={72}
    />
  );
}

function ChangeList({ changes }: { changes: string[] }) {
  return (
    <ul className="patch-change-list">
      {changes.map((change, index) => steamPatchImagePattern.test(change) ? (
        <li className="patch-inline-image" key={`${change}-${index}`}>
          <CspImage
            alt=""
            height={825}
            sizes="(max-width: 820px) calc(100vw - 48px), 1000px"
            src={change}
            width={1000}
          />
        </li>
      ) : <li key={`${change}-${index}`}>{change}</li>)}
    </ul>
  );
}

function PatchPageSkeleton() {
  return <div className="patch-page-skeleton" aria-label="Загрузка" role="status"><span /><span /><span /></div>;
}
