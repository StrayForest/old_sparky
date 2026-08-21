"use client";

import { Landmark, Package, Sparkles, Target } from "lucide-react";
import { useEffect, useState } from "react";
import { useI18n } from "@/components/i18n-provider";
import { Hero } from "@/components/layout/hero";
import { CspImage } from "@/components/media/csp-image";
import { deadlockHeroIconPath, deadlockHeroPlaceholderPath } from "@/lib/deadlock";
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
                      <CspImage
                        alt=""
                        className="patch-hero-image"
                        height={88}
                        onError={(event) => {
                          event.currentTarget.onerror = null;
                          event.currentTarget.src = deadlockHeroPlaceholderPath;
                        }}
                        src={deadlockHeroIconPath(section.hero_name)}
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
                          : section.title}</h2>
                    </div>
                  </header>
                  {section.changes.length ? <ChangeList changes={section.changes} /> : null}
                  {section.abilities.length ? (
                    <div className="patch-abilities">
                      {section.abilities.map((ability) => (
                        <section className="patch-ability" key={ability.name}>
                          <header>
                            {ability.icon_url ? (
                              <CspImage
                                alt=""
                                className="patch-ability-icon"
                                height={48}
                                src={ability.icon_url}
                                width={48}
                              />
                            ) : <span><Sparkles aria-hidden="true" size={20} /></span>}
                            <h3>{ability.name}</h3>
                          </header>
                          <ChangeList changes={ability.changes} />
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
