"use client";

import { useEffect, useRef, useState } from "react";
import {
  Check,
  Copy,
  Mail,
  MapPin,
  UserRound
} from "lucide-react";
import { DiscordIcon, SteamIcon } from "@/components/icons/brand-icons";
import { CspImage } from "@/components/media/csp-image";
import { PreparedMedia } from "@/components/media/prepared-media";
import { copyTextToClipboard } from "@/lib/clipboard";
import {
  deadlockHeroIconPath,
  deadlockRankIconPath,
  deadlockRankPlaceholderPath
} from "@/lib/deadlock";
import type { ContactField, PlayerProfile } from "@/lib/types";

const COPY_CONFIRMATION_MS = 3000;
const contactCopyLabels: Record<string, string> = {
  "Почта": "Скопировать почту",
  "Discord": "Скопировать Discord",
  "Steam ID": "Скопировать Steam ID",
  "Регион": "Скопировать регион"
};

export function PublicProfileView({ profile }: { profile: PlayerProfile }) {
  const [copiedContact, setCopiedContact] = useState<string | null>(null);
  const copyTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => () => {
    if (copyTimerRef.current) {
      clearTimeout(copyTimerRef.current);
    }
  }, []);

  async function copyContact(contact: ContactField) {
    const value = contact.value.trim();
    if (!value || copiedContact === contact.label) {
      return;
    }
    if (!await copyTextToClipboard(value)) {
      return;
    }
    if (copyTimerRef.current) {
      clearTimeout(copyTimerRef.current);
    }
    setCopiedContact(contact.label);
    copyTimerRef.current = setTimeout(() => {
      setCopiedContact(null);
      copyTimerRef.current = null;
    }, COPY_CONFIRMATION_MS);
  }

  return (
    <div className="public-profile-view">
      <section className="panel public-profile-summary">
          <PreparedMedia
            alt=""
            className="public-profile-banner"
            descriptor={profile.bannerMedia}
            fallbackUrl={profile.bannerUrl}
            sizes="(max-width: 820px) 100vw, 1200px"
          />
          <div className="public-profile-identity">
            <div className={profile.avatarUrl || profile.avatarMedia?.status === "ready" ? "public-profile-avatar has-image" : "public-profile-avatar profile-avatar-empty"}>
              {profile.avatarUrl || profile.avatarMedia?.status === "ready" ? (
                <PreparedMedia
                  alt=""
                  descriptor={profile.avatarMedia}
                  fallbackUrl={profile.avatarUrl}
                  height={112}
                  sizes="112px"
                  width={112}
                />
              ) : <UserRound aria-hidden="true" />}
            </div>
            <div className="public-profile-name-wrap">
              <span>Участник турнира</span>
              <h2>{profile.displayName}</h2>
              {profile.teamName ? <p>{profile.teamName}</p> : null}
            </div>
          </div>
      </section>

      <div className="public-profile-grid">
        <section className="panel public-profile-section public-profile-overview">
          <div className="public-profile-rank-column">
            <ProfileRank rank={profile.rank} subrank={profile.subrank} />
          </div>
          <div className="public-profile-details">
            <ProfileStat label="Часов в игре" value={profile.hoursRange} />
            <div className="public-profile-block">
              <span className="public-profile-label">Роли</span>
              <div className="public-profile-pills">
                {profile.roles.length ? profile.roles.map((role) => <span key={role}>{role}</span>) : <span>Не указаны</span>}
              </div>
            </div>
            <div className="public-profile-block">
              <span className="public-profile-label">Пул героев</span>
              <div className="public-profile-heroes">
                {profile.heroes.length ? profile.heroes.slice(0, 3).map((hero) => (
                  <div className="hero-card public-profile-hero" key={hero}>
                    <CspImage
                      alt=""
                      className="hero-card-image"
                      fill
                      sizes="96px"
                      src={deadlockHeroIconPath(hero)}
                    />
                    <span>{hero}</span>
                  </div>
                )) : <span className="public-profile-empty">Герои не выбраны</span>}
              </div>
            </div>
          </div>
        </section>

        <section className="panel public-profile-section public-profile-contact-section">
          <div className="public-profile-contacts">
            {profile.contacts.map((contact) => (
              <ProfileContact
                contact={contact}
                copied={copiedContact === contact.label}
                key={contact.label}
                onCopy={() => void copyContact(contact)}
              />
            ))}
          </div>
        </section>
      </div>
    </div>
  );
}

function ProfileRank({ rank, subrank }: { rank: string; subrank: string }) {
  const rankLabel = [rank, subrank].filter(Boolean).join(" ") || "Не указано";
  return (
    <div className="public-profile-rank">
      {rank ? (
        <CspImage
          alt=""
          height={76}
          onError={(event) => {
            event.currentTarget.onerror = null;
            event.currentTarget.src = deadlockRankPlaceholderPath;
          }}
          src={deadlockRankIconPath(rank)}
          width={76}
        />
      ) : null}
      <strong>{rankLabel}</strong>
    </div>
  );
}

function ProfileStat({ label, value }: { label: string; value: string }) {
  return (
    <div className="public-profile-stat">
      <small>{label}</small>
      <strong>{value || "Не указано"}</strong>
    </div>
  );
}

function ProfileContact({
  contact,
  copied,
  onCopy
}: {
  contact: ContactField;
  copied: boolean;
  onCopy: () => void;
}) {
  const rawValue = contact.value.trim();
  const value = rawValue || "Не указано";
  const content = contact.label === "Почта" && contact.value.trim()
    ? <a href={`mailto:${contact.value.trim()}`}>{value}</a>
    : <span>{value}</span>;
  const icon = contactIcon(contact.label);
  return (
    <div className="public-profile-contact">
      <span className="public-profile-contact-icon" aria-hidden="true">{icon}</span>
      <div>
        <small>{contact.label}</small>
        {content}
      </div>
      <button
        aria-label={copied ? `${contact.label}: скопировано` : (contactCopyLabels[contact.label] ?? `Скопировать ${contact.label}`)}
        className={copied ? "public-profile-copy-button copied" : "public-profile-copy-button"}
        disabled={copied || !rawValue}
        onClick={onCopy}
        title={copied ? "Скопировано" : "Скопировать"}
        type="button"
      >
        {copied ? <Check aria-hidden="true" size={17} /> : <Copy aria-hidden="true" size={17} />}
      </button>
    </div>
  );
}

function contactIcon(label: string) {
  if (label === "Почта") {
    return <Mail size={18} />;
  }
  if (label === "Discord") {
    return <DiscordIcon size={18} />;
  }
  if (label === "Steam ID") {
    return <SteamIcon size={18} />;
  }
  return <MapPin size={18} />;
}
