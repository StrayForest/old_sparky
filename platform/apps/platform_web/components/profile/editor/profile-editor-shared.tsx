"use client";

import { type ReactNode, type Ref } from "react";
import { RotateCcw, Save } from "lucide-react";
import type {
  PlatformDeadlockDreamSlot,
  PlatformMediaDescriptor,
} from "@/lib/platform-types";
import type { ContactField, PlayerProfile } from "@/lib/types";

export const subrankOptions = ["I", "II", "III", "IV", "V", "VI"] as const;
export const hoursOptions = [
  "0-500",
  "501-1000",
  "1001-1500",
  "1501-2000",
  "2001-3000",
  "3000+",
] as const;
export const roleOptions = [
  "Carry",
  "Semi-Carry",
  "Support",
  "Semi-Support",
] as const;
export const captainPreferenceOptions = [
  "Повысить",
  "Нейтрально",
  "Понизить",
] as const;

export type SaveState = "idle" | "saving" | "saved" | "error";

export function ProfileBanner({
  icon,
  title,
  text,
}: {
  icon: ReactNode;
  title: string;
  text: string;
}) {
  return (
    <div className="panel captain-banner">
      <div className="balance-icon">{icon}</div>
      <div>
        <h3>{title}</h3>
        <p className="description-text">{text}</p>
      </div>
      <div className="compass" aria-hidden="true" />
    </div>
  );
}

export function ValidationMessage({
  message,
  testId,
}: {
  message: string;
  testId: string;
}) {
  if (!message) {
    return null;
  }
  return (
    <div className="account-help" data-testid={testId}>
      {message}
    </div>
  );
}

export function ProfileActionButtons({
  saveState,
  saveTestId,
  cancelTestId,
  hasChanges,
  onSave,
  onCancel,
}: {
  saveState: SaveState;
  saveTestId: string;
  cancelTestId: string;
  hasChanges: boolean;
  onSave: () => void;
  onCancel: () => void;
}) {
  const disabled = !hasChanges || saveState === "saving";
  return (
    <>
      <button
        className="ghost-button"
        data-testid={cancelTestId}
        type="button"
        disabled={disabled}
        onClick={onCancel}
      >
        <RotateCcw size={20} />
        Отменить
      </button>
      <button
        aria-disabled={disabled}
        className="primary-button"
        data-testid={saveTestId}
        onClick={() => {
          if (!disabled) {
            onSave();
          }
        }}
        type="button"
      >
        <Save size={18} />
        {saveState === "saving"
          ? "Сохранение..."
          : saveState === "saved"
            ? "Сохранено"
            : saveState === "error"
              ? "Не сохранено"
              : "Сохранить"}
      </button>
    </>
  );
}

export function AccountField({
  autoComplete,
  disabled = false,
  icon,
  inputRef,
  label,
  maxLength,
  name,
  onChange,
  placeholder = "Не заполнено",
  type = "text",
  value,
}: {
  autoComplete?: string;
  disabled?: boolean;
  icon: ReactNode;
  inputRef?: Ref<HTMLInputElement>;
  label: string;
  maxLength?: number;
  name?: string;
  onChange: (value: string) => void;
  placeholder?: string;
  type?: "email" | "password" | "text";
  value: string;
}) {
  const inputId = name?.replaceAll("_", "-");

  return (
    <label className="field account-field" htmlFor={inputId}>
      <span className="label">{label}</span>
      <span className="account-input-wrap">
        {icon}
        <input
          autoComplete={autoComplete}
          className="input account-input"
          disabled={disabled}
          id={inputId}
          ref={inputRef}
          maxLength={maxLength}
          name={name}
          placeholder={placeholder}
          type={type}
          value={value}
          onChange={(event) => onChange(event.target.value)}
        />
      </span>
    </label>
  );
}

export function hasPreparedOrLegacyMedia(
  descriptor: PlatformMediaDescriptor | null | undefined,
  legacyUrl: string | null | undefined
): boolean {
  return Boolean(
    legacyUrl ||
      (descriptor?.status === "ready" && descriptor.variants.length > 0)
  );
}

export function cloneContacts(contacts: ContactField[]): ContactField[] {
  return contacts.map((contact) => ({ ...contact }));
}

export function contactsEqual(
  left: ContactField[],
  right: ContactField[]
): boolean {
  return (
    left.length === right.length &&
    left.every(
      (contact, index) =>
        right[index]?.label === contact.label &&
        right[index]?.value === contact.value
    )
  );
}

export function cloneTournamentProfile(profile: PlayerProfile): PlayerProfile {
  return {
    ...profile,
    roles: [...profile.roles],
    heroes: [...profile.heroes],
    heroPool: profile.heroPool.map((hero) => ({ ...hero })),
    contacts: cloneContacts(profile.contacts),
    teammatePreferences: profile.teammatePreferences.map((preference) => ({
      ...preference,
      roles: [...preference.roles],
      heroes: [...preference.heroes],
    })),
  };
}

export function tournamentProfileEqual(
  current: PlayerProfile,
  saved: PlayerProfile,
  captainPreference: string,
  savedCaptainPreference: string
): boolean {
  return (
    current.rank === saved.rank &&
    current.subrank === saved.subrank &&
    current.hoursRange === saved.hoursRange &&
    stringArraysEqual(current.roles, saved.roles) &&
    stringArraysEqual(current.heroes, saved.heroes) &&
    captainPreference === savedCaptainPreference
  );
}

export function calculateProfileCompletion(
  profile: PlayerProfile,
  dreamSlots: PlatformDeadlockDreamSlot[],
  contacts: ContactField[]
): number {
  const checks = [
    Boolean(profile.rank),
    Boolean(profile.subrank),
    Boolean(profile.hoursRange),
    profile.roles.length > 0,
    profile.heroes.length === 3,
    ...Array.from({ length: 6 }, (_, index) => {
      const slot = dreamSlots.find((item) => item.slot_number === index + 1);
      return Boolean(slot && isConfiguredDreamSlot(slot));
    }),
    ...contacts.map((contact) => Boolean(contact.value.trim())),
  ];
  return Math.round((checks.filter(Boolean).length / checks.length) * 100);
}

export function isValidDreamSlotDraft(
  slot: PlatformDeadlockDreamSlot
): boolean {
  const hasRoles = slot.allowed_roles.length > 0;
  const heroCount = slot.desired_heroes.length;
  const empty = !hasRoles && heroCount === 0;
  return empty || (hasRoles && heroCount <= 5);
}

export function isConfiguredDreamSlot(
  slot: PlatformDeadlockDreamSlot
): boolean {
  return slot.allowed_roles.length > 0 && slot.desired_heroes.length <= 5;
}

export function cloneDreamSlot(
  slot: PlatformDeadlockDreamSlot
): PlatformDeadlockDreamSlot {
  return {
    ...slot,
    allowed_roles: [...slot.allowed_roles],
    desired_heroes: [...slot.desired_heroes],
  };
}

export function dreamSlotsEqual(
  left: PlatformDeadlockDreamSlot[],
  right: PlatformDeadlockDreamSlot[]
): boolean {
  return (
    left.length === right.length &&
    left.every(
      (slot, index) => Boolean(right[index] && dreamSlotEqual(slot, right[index]))
    )
  );
}

export function dreamSlotEqual(
  left: PlatformDeadlockDreamSlot,
  right: PlatformDeadlockDreamSlot
): boolean {
  return (
    left.slot_number === right.slot_number &&
    left.allowed_roles.length === right.allowed_roles.length &&
    left.allowed_roles.every((role, index) => role === right.allowed_roles[index]) &&
    left.desired_heroes.length === right.desired_heroes.length &&
    left.desired_heroes.every(
      (hero, index) => hero === right.desired_heroes[index]
    )
  );
}

function stringArraysEqual(left: string[], right: string[]): boolean {
  return (
    left.length === right.length &&
    left.every((value, index) => value === right[index])
  );
}
