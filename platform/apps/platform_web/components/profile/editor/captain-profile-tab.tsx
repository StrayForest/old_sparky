"use client";

import { Fragment, useEffect, useRef, useState } from "react";
import { ChevronDown, Crown } from "lucide-react";
import { useI18n } from "@/components/i18n-provider";
import { CspImage } from "@/components/media/csp-image";
import {
  cloneDreamSlot,
  dreamSlotEqual,
  dreamSlotsEqual,
  isConfiguredDreamSlot,
  isValidDreamSlotDraft,
  ProfileActionButtons,
  ProfileBanner,
  roleOptions,
  ValidationMessage,
  type SaveState,
} from "@/components/profile/editor/profile-editor-shared";
import {
  deadlockHeroIconPath,
  deadlockHeroPlaceholderPath,
  toggleHeroSelection,
} from "@/lib/deadlock";
import { updateCaptainProfile } from "@/lib/profile-api";
import type { PlatformDeadlockDreamSlot } from "@/lib/platform-types";
import type { PlayerProfile } from "@/lib/types";

export function CaptainProfileTab({
  initialTeamName,
  initialDreamSlots,
  heroOptions,
  onPreview,
}: {
  initialTeamName: string;
  initialDreamSlots: PlatformDeadlockDreamSlot[];
  heroOptions: PlayerProfile["heroPool"];
  onPreview: (
    teamName: string,
    dreamSlots: PlatformDeadlockDreamSlot[]
  ) => void;
}) {
  const { t } = useI18n();
  const [teamName, setTeamName] = useState(initialTeamName);
  const [savedTeamName, setSavedTeamName] = useState(initialTeamName);
  const [dreamSlots, setDreamSlots] = useState(() =>
    initialDreamSlots.map(cloneDreamSlot)
  );
  const [savedDreamSlots, setSavedDreamSlots] = useState(() =>
    initialDreamSlots.map(cloneDreamSlot)
  );
  const [saveState, setSaveState] = useState<SaveState>("idle");
  const [validationMessage, setValidationMessage] = useState("");
  const [activeHeroSlotNumber, setActiveHeroSlotNumber] =
    useState<number | null>(null);
  const [teammateColumns, setTeammateColumns] = useState(3);
  const teammateGridRef = useRef<HTMLDivElement | null>(null);
  const saveInFlightRef = useRef(false);

  const activeHeroSlot =
    dreamSlots.find((slot) => slot.slot_number === activeHeroSlotNumber) ?? null;
  const activeHeroSlotIndex = activeHeroSlot
    ? dreamSlots.findIndex(
        (slot) => slot.slot_number === activeHeroSlot.slot_number
      )
    : -1;
  const heroPickerInsertIndex =
    activeHeroSlotIndex >= 0
      ? Math.min(
          dreamSlots.length - 1,
          Math.ceil((activeHeroSlotIndex + 1) / teammateColumns) *
            teammateColumns -
            1
        )
      : -1;
  const hasChanges =
    teamName.trim() !== savedTeamName.trim() ||
    !dreamSlotsEqual(dreamSlots, savedDreamSlots);
  const savedSlotsByNumber = new Map(
    savedDreamSlots.map((slot) => [slot.slot_number, slot])
  );

  useEffect(() => {
    const gridElement = teammateGridRef.current;
    if (!gridElement) {
      return;
    }
    const observedGrid = gridElement;

    function updateColumns() {
      const template = window.getComputedStyle(observedGrid).gridTemplateColumns;
      const columns = template.split(" ").filter(Boolean).length;
      setTeammateColumns(Math.max(1, columns || 1));
    }

    updateColumns();
    const resizeObserver = new ResizeObserver(updateColumns);
    resizeObserver.observe(observedGrid);
    return () => resizeObserver.disconnect();
  }, []);

  function applyTeamName(value: string) {
    if (saveInFlightRef.current) {
      return;
    }
    const next = value.slice(0, 15);
    setTeamName(next);
    onPreview(next, dreamSlots);
    setSaveState("idle");
    setValidationMessage("");
  }

  function applyDreamSlots(next: PlatformDeadlockDreamSlot[]) {
    if (saveInFlightRef.current) {
      return;
    }
    setDreamSlots(next);
    onPreview(teamName, next);
    setSaveState("idle");
    setValidationMessage("");
  }

  function updateSlot(
    slotNumber: number,
    next: Partial<PlatformDeadlockDreamSlot>
  ) {
    applyDreamSlots(
      dreamSlots.map((slot) =>
        slot.slot_number === slotNumber ? { ...slot, ...next } : slot
      )
    );
  }

  function toggleSlotRole(slot: PlatformDeadlockDreamSlot, role: string) {
    updateSlot(slot.slot_number, {
      allowed_roles: slot.allowed_roles.includes(role)
        ? slot.allowed_roles.filter((item) => item !== role)
        : [...slot.allowed_roles, role],
    });
  }

  function toggleSlotHero(slot: PlatformDeadlockDreamSlot, hero: string) {
    updateSlot(slot.slot_number, {
      desired_heroes: toggleHeroSelection(slot.desired_heroes, hero, 5),
    });
  }

  async function saveCaptain() {
    if (saveInFlightRef.current) {
      return;
    }
    if (!dreamSlots.every(isValidDreamSlotDraft)) {
      setSaveState("error");
      setValidationMessage(
        "Для настроенного слота выберите минимум одну роль. Для каждого тиммейта можно выбрать от 0 до 5 героев."
      );
      return;
    }

    saveInFlightRef.current = true;
    setSaveState("saving");
    setValidationMessage("");
    try {
      const updated = await updateCaptainProfile(teamName.trim(), dreamSlots);
      const nextSlots = updated.dreamSlots.map(cloneDreamSlot);
      setTeamName(updated.teamName);
      setSavedTeamName(updated.teamName);
      setDreamSlots(nextSlots);
      setSavedDreamSlots(nextSlots.map(cloneDreamSlot));
      onPreview(updated.teamName, nextSlots);
      setSaveState("saved");
    } catch {
      setSaveState("error");
      setValidationMessage("Не удалось сохранить профиль капитана.");
    } finally {
      saveInFlightRef.current = false;
    }
  }

  function cancelChanges() {
    if (saveInFlightRef.current) {
      return;
    }
    const nextSlots = savedDreamSlots.map(cloneDreamSlot);
    setTeamName(savedTeamName);
    setDreamSlots(nextSlots);
    setActiveHeroSlotNumber(null);
    onPreview(savedTeamName, nextSlots);
    setSaveState("idle");
    setValidationMessage("");
  }

  function renderHeroPicker(slot: PlatformDeadlockDreamSlot) {
    return (
      <div
        className="dream-hero-picker-panel"
        data-testid={`profile-dream-slot-${slot.slot_number}-hero-picker`}
      >
        <div className="dream-hero-picker-head">
          <div>
            <div className="label">{t("deadlock.heroPool")}</div>
            <h4 className="dream-hero-picker-title">
              {slot.slot_number < 6
                ? `Тиммейт ${slot.slot_number}`
                : "Замена"}
              : {slot.desired_heroes.length}/5
            </h4>
          </div>
          <button
            className="ghost-button"
            disabled={saveState === "saving"}
            type="button"
            onClick={() => setActiveHeroSlotNumber(null)}
          >
            {t("common.close")}
          </button>
        </div>
        <div className="dream-hero-grid dream-hero-picker-grid">
          {heroOptions.map((hero) => {
            const selected = slot.desired_heroes.includes(hero.name);
            const unavailable =
              !selected && slot.desired_heroes.length >= 5;
            return (
              <button
                className={selected ? "dream-hero selected" : "dream-hero"}
                data-testid={`profile-dream-slot-${
                  slot.slot_number
                }-hero-${hero.name.toLowerCase().replace(/\s+/g, "-")}`}
                disabled={saveState === "saving" || unavailable}
                type="button"
                key={hero.name}
                onClick={() => toggleSlotHero(slot, hero.name)}
              >
                <CspImage
                  alt=""
                  fill
                  onError={(event) => {
                    event.currentTarget.onerror = null;
                    event.currentTarget.src = deadlockHeroPlaceholderPath;
                  }}
                  sizes="120px"
                  src={deadlockHeroIconPath(hero.name)}
                />
                <span>{hero.name}</span>
              </button>
            );
          })}
        </div>
      </div>
    );
  }

  return (
    <section aria-busy={saveState === "saving"}>
      <ProfileBanner
        icon={<Crown size={40} />}
        title="Автоматическое формирование команд для капитана"
        text="Команды формируются автоматически с учетом ваших пожеланий, если это возможно в рамках баланса среди команд."
      />
      <div className="panel teammates-panel captain-settings-panel">
        <div className="captain-settings-head">
          <label className="field captain-team-name-field">
            <span className="panel-subtitle">
              Название команды {teamName.length}/15
            </span>
            <input
              className="input"
              data-testid="profile-captain-team-name"
              disabled={saveState === "saving"}
              maxLength={15}
              value={teamName}
              onChange={(event) => applyTeamName(event.target.value)}
            />
          </label>
        </div>
        <h4 className="panel-subtitle">Пожелания по тиммейтам</h4>
        <div className="teammate-grid" ref={teammateGridRef}>
          {dreamSlots.map((slot, index) => {
            const savedSlot = savedSlotsByNumber.get(slot.slot_number);
            const configured = Boolean(
              savedSlot &&
                isConfiguredDreamSlot(savedSlot) &&
                dreamSlotEqual(slot, savedSlot)
            );
            return (
              <Fragment key={slot.slot_number}>
                <article className="mate-card dream-slot-card">
                  <div className="mate-head">
                    <h4 className="mate-title">
                      {slot.slot_number < 6
                        ? `Тиммейт ${slot.slot_number}`
                        : "Замена"}
                    </h4>
                    <span
                      className={
                        configured ? "state-badge" : "state-badge empty"
                      }
                    >
                      {configured ? "Настроен" : "Не настроен"}
                    </span>
                  </div>
                  <div className="dream-slot-section">
                    <div className="label">Роли</div>
                    <div className="pill-row dream-pill-row">
                      {roleOptions.map((role) => (
                        <button
                          className={
                            slot.allowed_roles.includes(role)
                              ? "pill active"
                              : "pill"
                          }
                          disabled={saveState === "saving"}
                          type="button"
                          key={role}
                          onClick={() => toggleSlotRole(slot, role)}
                        >
                          {role}
                        </button>
                      ))}
                    </div>
                  </div>
                  <div className="dream-slot-section">
                    <div className="label">
                      Герои: {slot.desired_heroes.length}/5
                    </div>
                    <div
                      className={
                        slot.desired_heroes.length > 0
                          ? "dream-selected-heroes"
                          : "dream-selected-heroes empty"
                      }
                    >
                      {slot.desired_heroes.length === 0 ? (
                        <span className="dream-selected-heroes-empty">
                          {t("profile.heroPoolEmpty")}
                        </span>
                      ) : (
                        slot.desired_heroes.map((hero) => (
                          <div className="dream-selected-hero" key={hero}>
                            <CspImage
                              alt=""
                              fill
                              onError={(event) => {
                                event.currentTarget.onerror = null;
                                event.currentTarget.src =
                                  deadlockHeroPlaceholderPath;
                              }}
                              sizes="78px"
                              src={deadlockHeroIconPath(hero)}
                            />
                            <span>{hero}</span>
                          </div>
                        ))
                      )}
                    </div>
                    <button
                      aria-expanded={
                        activeHeroSlotNumber === slot.slot_number
                      }
                      className={
                        activeHeroSlotNumber === slot.slot_number
                          ? "dream-hero-picker-toggle active"
                          : "dream-hero-picker-toggle"
                      }
                      data-testid={`profile-dream-slot-${slot.slot_number}-heroes-toggle`}
                      disabled={saveState === "saving"}
                      type="button"
                      onClick={() =>
                        setActiveHeroSlotNumber((current) =>
                          current === slot.slot_number
                            ? null
                            : slot.slot_number
                        )
                      }
                    >
                      <span>{t("deadlock.selectHeroes")}</span>
                      <ChevronDown size={18} />
                    </button>
                  </div>
                </article>
                {activeHeroSlot && index === heroPickerInsertIndex
                  ? renderHeroPicker(activeHeroSlot)
                  : null}
              </Fragment>
            );
          })}
        </div>
        <div className="captain-profile-actions">
          <ValidationMessage
            message={validationMessage}
            testId="profile-captain-validation"
          />
          <ProfileActionButtons
            cancelTestId="profile-cancel-captain-button"
            hasChanges={hasChanges}
            onCancel={cancelChanges}
            onSave={() => {
              setActiveHeroSlotNumber(null);
              void saveCaptain();
            }}
            saveState={saveState}
            saveTestId="profile-save-captain-button"
          />
        </div>
      </div>
    </section>
  );
}
