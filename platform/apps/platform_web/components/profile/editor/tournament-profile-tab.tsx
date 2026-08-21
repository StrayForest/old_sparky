"use client";

import { useState } from "react";
import { Sword } from "lucide-react";
import { CspImage } from "@/components/media/csp-image";
import {
  captainPreferenceOptions,
  cloneTournamentProfile,
  hoursOptions,
  ProfileActionButtons,
  ProfileBanner,
  roleOptions,
  subrankOptions,
  tournamentProfileEqual,
  ValidationMessage,
  type SaveState,
} from "@/components/profile/editor/profile-editor-shared";
import {
  deadlockHeroIconPath,
  deadlockHeroPlaceholderPath,
  deadlockRankIconPath,
  deadlockRankPlaceholderPath,
  rankOptions,
  toggleHeroSelection,
} from "@/lib/deadlock";
import {
  updateTournamentProfile,
  type TournamentProfileUpdate,
} from "@/lib/profile-api";
import type { CaptainPreference } from "@/lib/profile-model";
import type { PlayerProfile } from "@/lib/types";
import { z } from "@/lib/zod";

const profileUpdateSchema = z.object({
  rank: z.enum(rankOptions),
  subrank: z.enum(subrankOptions),
  hoursRange: z.enum(hoursOptions),
  roles: z.array(z.enum(roleOptions)).min(1).max(roleOptions.length),
  heroes: z.array(z.string().trim().min(1)).length(3),
  captainPreference: z.enum(captainPreferenceOptions),
});

export function TournamentProfileTab({
  initialProfile,
  initialCaptainPreference,
  heroOptions,
  onPreview,
}: {
  initialProfile: PlayerProfile;
  initialCaptainPreference: CaptainPreference;
  heroOptions: PlayerProfile["heroPool"];
  onPreview: (profile: PlayerProfile) => void;
}) {
  const [draft, setDraft] = useState(() => cloneTournamentProfile(initialProfile));
  const [saved, setSaved] = useState(() => cloneTournamentProfile(initialProfile));
  const [captainPreference, setCaptainPreference] =
    useState<CaptainPreference>(initialCaptainPreference);
  const [savedCaptainPreference, setSavedCaptainPreference] =
    useState<CaptainPreference>(initialCaptainPreference);
  const [saveState, setSaveState] = useState<SaveState>("idle");
  const [validationMessage, setValidationMessage] = useState("");

  const hasChanges = !tournamentProfileEqual(
    draft,
    saved,
    captainPreference,
    savedCaptainPreference
  );

  function applyDraft(next: PlayerProfile) {
    setDraft(next);
    onPreview(next);
    setSaveState("idle");
    setValidationMessage("");
  }

  function setSingle(
    field: "rank" | "subrank" | "hoursRange",
    value: string
  ) {
    applyDraft({ ...draft, [field]: value });
  }

  function toggleRole(value: string) {
    const roles = draft.roles.includes(value)
      ? draft.roles.filter((role) => role !== value)
      : [...draft.roles, value];
    applyDraft({ ...draft, roles });
  }

  function toggleHero(value: string) {
    const heroes = toggleHeroSelection(draft.heroes, value, 3);
    applyDraft({
      ...draft,
      heroes,
      heroPool: heroOptions.map((hero) => ({ ...hero })),
    });
  }

  function updateCaptainPreference(value: CaptainPreference) {
    setCaptainPreference(value);
    setSaveState("idle");
    setValidationMessage("");
  }

  async function saveProfile() {
    const parsed = profileUpdateSchema.safeParse({
      rank: draft.rank,
      subrank: draft.subrank,
      hoursRange: draft.hoursRange,
      roles: draft.roles,
      heroes: draft.heroes,
      captainPreference,
    });
    if (!parsed.success) {
      setValidationMessage(
        "Выберите минимум одну роль и ровно 3 героя."
      );
      setSaveState("error");
      return;
    }

    setSaveState("saving");
    setValidationMessage("");
    try {
      const updated = await updateTournamentProfile(
        parsed.data satisfies TournamentProfileUpdate
      );
      const next = {
        ...draft,
        rank: updated.rank,
        subrank: updated.subrank,
        hoursRange: updated.hoursRange,
        roles: [...updated.roles],
        heroes: [...updated.heroes],
        heroPool: heroOptions.map((hero) => ({ ...hero })),
      };
      setDraft(next);
      setSaved(cloneTournamentProfile(next));
      setCaptainPreference(updated.captainPreference);
      setSavedCaptainPreference(updated.captainPreference);
      onPreview(next);
      setSaveState("saved");
    } catch {
      setSaveState("error");
      setValidationMessage("Не удалось сохранить профиль. Попробуйте еще раз.");
    }
  }

  function cancelChanges() {
    const next = cloneTournamentProfile(saved);
    setDraft(next);
    setCaptainPreference(savedCaptainPreference);
    onPreview(next);
    setSaveState("idle");
    setValidationMessage("");
  }

  return (
    <section>
      <ProfileBanner
        icon={<Sword size={40} />}
        title="Турнирные данные во время регистрации"
        text="Ваши данные будут использоваться при регистрации на турниры. Держите их актуальными и перепроверяйте перед регистрацией."
      />
      <div className="profile-grid tournament-profile-grid">
        <div className="panel tournament-profile-panel">
          <div className="tournament-profile-columns">
            <section className="tournament-profile-section tournament-profile-settings">
              <h3 className="panel-title">Основные параметры</h3>
              <div className="contact-list profile-settings-list">
                <RankPicker
                  active={draft.rank}
                  onSelect={(value) => setSingle("rank", value)}
                />
                <PillRow
                  label="Подранг"
                  values={subrankOptions}
                  active={draft.subrank}
                  onSelect={(value) => setSingle("subrank", value)}
                />
                <PillRow
                  label="Часов в игре"
                  values={hoursOptions}
                  active={draft.hoursRange}
                  onSelect={(value) => setSingle("hoursRange", value)}
                />
                <PillRow
                  label="Роли"
                  values={roleOptions}
                  active={draft.roles}
                  onSelect={toggleRole}
                />
                <PillRow
                  label="Вероятность стать капитаном"
                  values={captainPreferenceOptions}
                  active={captainPreference}
                  onSelect={(value) =>
                    updateCaptainPreference(value as CaptainPreference)
                  }
                />
              </div>
            </section>
            <section className="tournament-profile-section tournament-profile-heroes">
              <div className="panel-title-row">
                <h3 className="panel-title">Пул героев</h3>
                <div className="selected-badge">
                  Выбрано: {draft.heroes.length}/3
                </div>
              </div>
              <div className="heroes-grid">
                {heroOptions.map((hero) => {
                  const selected = draft.heroes.includes(hero.name);
                  const unavailable = !selected && draft.heroes.length >= 3;
                  return (
                    <button
                      className={`hero-card ${hero.theme} ${
                        selected ? "selected" : ""
                      }`}
                      type="button"
                      key={hero.name}
                      disabled={unavailable}
                      onClick={() => toggleHero(hero.name)}
                    >
                      <CspImage
                        alt=""
                        className="hero-card-image"
                        fill
                        onError={(event) => {
                          event.currentTarget.onerror = null;
                          event.currentTarget.src = deadlockHeroPlaceholderPath;
                        }}
                        sizes="160px"
                        src={deadlockHeroIconPath(hero.name)}
                      />
                      <span>{hero.name}</span>
                    </button>
                  );
                })}
              </div>
            </section>
          </div>
          <ValidationMessage
            message={validationMessage}
            testId="profile-settings-validation"
          />
          <div className="profile-footer-actions tournament-profile-actions">
            <ProfileActionButtons
              cancelTestId="profile-cancel-settings-button"
              hasChanges={hasChanges}
              onCancel={cancelChanges}
              onSave={() => void saveProfile()}
              saveState={saveState}
              saveTestId="profile-save-settings-button"
            />
          </div>
        </div>
      </div>
    </section>
  );
}

function PillRow({
  label,
  values,
  active,
  onSelect,
}: {
  label: string;
  values: readonly string[];
  active: string | string[];
  onSelect: (value: string) => void;
}) {
  const activeValues = Array.isArray(active) ? active : [active];
  return (
    <div>
      <div className="label mb-2">{label}</div>
      <div className="pill-row">
        {values.map((value) => (
          <button
            className={activeValues.includes(value) ? "pill active" : "pill"}
            data-testid={`profile-pill-${value
              .toLowerCase()
              .replace(/\s+/g, "-")}`}
            type="button"
            key={value}
            onClick={() => onSelect(value)}
          >
            {value}
          </button>
        ))}
      </div>
    </div>
  );
}

function RankPicker({
  active,
  onSelect,
}: {
  active: string;
  onSelect: (value: string) => void;
}) {
  return (
    <div className="profile-rank-picker">
      <div className="profile-rank-grid">
        {rankOptions.map((rank) => (
          <button
            aria-label={`Ранг ${rank}`}
            className={active === rank ? "profile-rank-card active" : "profile-rank-card"}
            data-testid={`profile-pill-${rank.toLowerCase()}`}
            key={rank}
            onClick={() => onSelect(rank)}
            type="button"
          >
            <CspImage
              alt=""
              height={44}
              onError={(event) => {
                event.currentTarget.onerror = null;
                event.currentTarget.src = deadlockRankPlaceholderPath;
              }}
              src={deadlockRankIconPath(rank)}
              width={44}
            />
            <span>{rank}</span>
          </button>
        ))}
      </div>
    </div>
  );
}
