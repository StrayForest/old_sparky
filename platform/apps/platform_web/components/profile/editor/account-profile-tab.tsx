"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState, useTransition } from "react";
import {
  Camera,
  HeartHandshake,
  KeyRound,
  LogOut,
  Mail,
  MapPin,
  NotebookPen,
  Save,
  UserRound,
} from "lucide-react";
import { useAuth } from "@/components/auth/auth-provider";
import { useI18n } from "@/components/i18n-provider";
import { DiscordIcon } from "@/components/icons/brand-icons";
import { PreparedMedia } from "@/components/media/prepared-media";
import {
  AccountEmailIdentity,
  AccountSteamIdentity,
} from "@/components/profile/account-identities";
import {
  AccountField,
  cloneContacts,
  contactsEqual,
  hasPreparedOrLegacyMedia,
  ProfileActionButtons,
  ProfileBanner,
  ValidationMessage,
  type SaveState,
} from "@/components/profile/editor/profile-editor-shared";
import {
  deleteCurrentProfileAvatar,
  platformApiMessage,
  platformApiRequest,
  resetPlatformCsrfToken,
  updateCurrentAccountProfile,
  updateCurrentAccountSecurity,
  uploadCurrentProfileAvatar,
  waitForOwnedMedia,
  type AccountProfileUpdatePayload,
} from "@/lib/platform-api";
import type { PlatformUser } from "@/lib/platform-types";
import type { ContactField, PlayerProfile } from "@/lib/types";

const mediaSourceMaxBytes = 5 * 1024 * 1024;
const mediaSourceTypes = new Set(["image/jpeg", "image/png", "image/webp"]);

export function AccountProfileTab({
  initialProfile,
  steamAuthStatus,
  onPreview,
}: {
  initialProfile: PlayerProfile;
  steamAuthStatus?: "error" | "success";
  onPreview: (profile: PlayerProfile, contacts: ContactField[]) => void;
}) {
  const { clearUser, setUser, updateUser, user } = useAuth();
  const { t } = useI18n();
  const router = useRouter();

  const [profile, setProfile] = useState(initialProfile);
  const [displayName, setDisplayName] = useState(initialProfile.displayName);
  const [savedDisplayName, setSavedDisplayName] = useState(
    initialProfile.displayName
  );
  const [savedAccountEmail, setSavedAccountEmail] = useState(
    initialProfile.accountEmail
  );
  const [contacts, setContacts] = useState<ContactField[]>(() =>
    cloneContacts(initialProfile.contacts)
  );
  const [savedContacts, setSavedContacts] = useState<ContactField[]>(() =>
    cloneContacts(initialProfile.contacts)
  );
  const [saveState, setSaveState] = useState<SaveState>("idle");
  const [message, setMessage] = useState("");
  const [avatarSaveState, setAvatarSaveState] =
    useState<SaveState>("idle");
  const [avatarMediaMessage, setAvatarMediaMessage] = useState("");

  const [securitySaveState, setSecuritySaveState] =
    useState<SaveState>("idle");
  const [securityMessage, setSecurityMessage] = useState("");
  const [logoutError, setLogoutError] = useState("");
  const [emailDirty, setEmailDirty] = useState(false);
  const [emailBusy, setEmailBusy] = useState(false);
  const [emailSaveRequestId, setEmailSaveRequestId] = useState(0);
  const [emailCancelRequestId, setEmailCancelRequestId] = useState(0);
  const [isLoggingOut, startLogoutTransition] = useTransition();

  const contactValues = new Map(
    contacts.map((contact) => [contact.label, contact.value])
  );
  const hasChanges =
    displayName.trim() !== savedDisplayName.trim() ||
    !contactsEqual(contacts, savedContacts);
  const combinedHasChanges = hasChanges || emailDirty;
  const effectiveSaveState: SaveState = emailBusy ? "saving" : saveState;

  function emitPreview(
    nextProfile = profile,
    nextContacts = contacts
  ) {
    onPreview(nextProfile, nextContacts);
  }

  function updateContact(label: string, value: string) {
    const limits: Record<string, number> = {
      Почта: 254,
      Discord: 64,
      Регион: 40,
    };
    const normalizedValue = value.slice(0, limits[label] ?? 255);
    const next = contacts.map((contact) =>
      contact.label === label
        ? { ...contact, value: normalizedValue }
        : contact
    );
    setContacts(next);
    emitPreview({ ...profile, contacts: next }, next);
    setSaveState("idle");
    setMessage("");
  }

  function updateDisplayName(value: string) {
    const nextDisplayName = value.slice(0, 15);
    setDisplayName(nextDisplayName);
    emitPreview({ ...profile, displayName: nextDisplayName }, contacts);
    setSaveState("idle");
    setMessage("");
  }

  async function saveAccount(): Promise<boolean> {
    const byLabel = new Map(
      contacts.map((contact) => [contact.label, contact.value.trim()])
    );
    const normalizedDisplayName = displayName.trim();
    if (normalizedDisplayName.length < 2) {
      setSaveState("error");
      setMessage("Ник должен содержать от 2 до 15 символов.");
      return false;
    }

    const payload: AccountProfileUpdatePayload = {
      display_name: normalizedDisplayName,
      contact_email: (user?.email ?? savedAccountEmail).trim() || null,
      discord_account: byLabel.get("Discord") || null,
      region: byLabel.get("Регион") || null,
    };

    setSaveState("saving");
    setMessage("");
    try {
      const updated = await updateCurrentAccountProfile(payload);
      const updatedContacts = cloneContacts(updated.contacts);
      const nextProfile = {
        ...profile,
        displayName: updated.displayName,
        handle: updated.handle || profile.handle,
        avatarUrl: updated.avatarUrl,
        avatarMedia: updated.avatarMedia ?? profile.avatarMedia,
        contacts: updatedContacts,
      };
      setProfile(nextProfile);
      setDisplayName(updated.displayName);
      setSavedDisplayName(updated.displayName);
      setContacts(updatedContacts);
      setSavedContacts(cloneContacts(updatedContacts));
      setSaveState("saved");
      emitPreview(nextProfile, updatedContacts);
      if (user) {
        updateUser(user.id, {
          display_name: updated.displayName,
          avatar_url: updated.avatarUrl,
          avatar_media: updated.avatarMedia ?? null,
        });
      }
      return true;
    } catch (error) {
      setSaveState("error");
      setMessage(
        platformApiMessage(error, "Не удалось сохранить данные профиля.")
      );
      return false;
    }
  }

  function cancelAccountChanges() {
    const nextContacts = cloneContacts(savedContacts);
    const nextProfile = {
      ...profile,
      displayName: savedDisplayName,
      contacts: nextContacts,
    };
    setDisplayName(savedDisplayName);
    setContacts(nextContacts);
    setProfile(nextProfile);
    setSaveState("idle");
    setMessage("");
    setEmailCancelRequestId((current) => current + 1);
    emitPreview(nextProfile, nextContacts);
  }

  function applyLinkedEmail(updatedUser: PlatformUser) {
    const linkedEmail = updatedUser.email ?? "";
    if (!linkedEmail) {
      return;
    }
    const replaceEmail = (values: ContactField[]) => {
      const hasEmail = values.some((contact) => contact.label === "Почта");
      return hasEmail
        ? values.map((contact) =>
            contact.label === "Почта"
              ? { ...contact, value: linkedEmail }
              : contact
          )
        : [{ label: "Почта", value: linkedEmail }, ...values];
    };

    const nextContacts = replaceEmail(contacts);
    const nextSavedContacts = replaceEmail(savedContacts);
    const nextProfile = {
      ...profile,
      accountEmail: linkedEmail,
      contacts: nextContacts,
    };

    setUser(updatedUser);
    setSavedAccountEmail(linkedEmail);
    setProfile(nextProfile);
    setContacts(nextContacts);
    setSavedContacts(nextSavedContacts);
    setSaveState("idle");
    setMessage("");
    emitPreview(nextProfile, nextContacts);
  }

  async function uploadAvatar(file: File): Promise<boolean> {
    if (!mediaSourceTypes.has(file.type)) {
      setAvatarSaveState("error");
      setAvatarMediaMessage(t("profile.mediaUnsupported"));
      return false;
    }
    if (file.size > mediaSourceMaxBytes) {
      setAvatarSaveState("error");
      setAvatarMediaMessage(t("profile.mediaTooLarge"));
      return false;
    }

    setAvatarSaveState("saving");
    setAvatarMediaMessage("");
    try {
      const accepted = await uploadCurrentProfileAvatar(file);
      const descriptor = await waitForOwnedMedia(accepted.asset_id);
      const nextProfile = {
        ...profile,
        avatarMedia: descriptor,
      };
      setProfile(nextProfile);
      emitPreview(nextProfile, contacts);

      if (descriptor.status === "failed") {
        setAvatarSaveState("error");
        setAvatarMediaMessage(t("profile.avatarProcessingFailed"));
        return false;
      }

      setAvatarSaveState("saved");
      if (user) {
        updateUser(user.id, { avatar_media: descriptor });
      }
      return descriptor.status === "ready";
    } catch (error) {
      setAvatarSaveState("error");
      setAvatarMediaMessage(
        platformApiMessage(error, t("profile.avatarUploadFailed"))
      );
      return false;
    }
  }

  async function deleteAvatar(): Promise<boolean> {
    setAvatarSaveState("saving");
    setAvatarMediaMessage("");
    try {
      await deleteCurrentProfileAvatar();
      const nextProfile = {
        ...profile,
        avatarUrl: null,
        avatarMedia: null,
      };
      setProfile(nextProfile);
      emitPreview(nextProfile, contacts);
      setAvatarSaveState("saved");
      if (user) {
        updateUser(user.id, { avatar_url: null, avatar_media: null });
      }
      return true;
    } catch (error) {
      setAvatarSaveState("error");
      setAvatarMediaMessage(
        platformApiMessage(error, t("profile.avatarDeleteFailed"))
      );
      return false;
    }
  }

  async function saveAccountSecurity(
    currentPassword: string,
    newPassword: string
  ): Promise<string | null> {
    try {
      const updatedUser = await updateCurrentAccountSecurity({
        current_password: currentPassword,
        email: null,
        new_password: newPassword,
      });
      const nextProfile = {
        ...profile,
        accountEmail: updatedUser.email ?? "",
      };
      setSavedAccountEmail(updatedUser.email ?? "");
      setProfile(nextProfile);
      emitPreview(nextProfile, contacts);
      setUser(updatedUser);
      return null;
    } catch (error) {
      return platformApiMessage(
        error,
        "Не удалось обновить данные для входа."
      );
    }
  }

  function logout() {
    setLogoutError("");
    startLogoutTransition(async () => {
      try {
        await platformApiRequest<void>("/auth/logout", { method: "POST" });
        resetPlatformCsrfToken();
        clearUser();
        router.replace("/");
        router.refresh();
      } catch (error) {
        setLogoutError(
          platformApiMessage(error, t("profile.logoutFailed"))
        );
      }
    });
  }

  function requestProfileSave() {
    if (emailDirty) {
      setEmailSaveRequestId((current) => current + 1);
      return;
    }
    void saveAccount();
  }

  async function requestSecuritySave(form: HTMLFormElement) {
    if (securitySaveState === "saving") {
      return;
    }

    const formData = new FormData(form);
    const currentPassword = String(formData.get("current_password") ?? "");
    const newPassword = String(formData.get("new_password") ?? "");
    const confirmPassword = String(formData.get("confirm_password") ?? "");

    if (!currentPassword) {
      setSecuritySaveState("error");
      setSecurityMessage(t("profile.confirmCurrentPasswordRequired"));
      return;
    }
    if (newPassword.length < 10) {
      setSecuritySaveState("error");
      setSecurityMessage(
        "Новый пароль должен содержать не меньше 10 символов."
      );
      return;
    }
    if (newPassword !== confirmPassword) {
      setSecuritySaveState("error");
      setSecurityMessage("Новый пароль и подтверждение не совпадают.");
      return;
    }

    setSecuritySaveState("saving");
    setSecurityMessage("");
    const failureMessage = await saveAccountSecurity(
      currentPassword,
      newPassword
    );
    if (failureMessage) {
      setSecuritySaveState("error");
      setSecurityMessage(failureMessage);
      return;
    }

    form.reset();
    setSecurityMessage("");
    setSecuritySaveState("saved");
  }

  function resetSecurityFeedback() {
    if (securitySaveState !== "idle") {
      setSecuritySaveState("idle");
    }
    if (securityMessage) {
      setSecurityMessage("");
    }
  }

  return (
    <section>
      <ProfileBanner
        icon={<NotebookPen size={38} />}
        title="Данные профиля"
        text="Управляйте отображением профиля, контактами и безопасностью аккаунта."
      />
      <div className="account-settings-grid">
        <div className="panel account-settings-card">
          <div className="account-avatar-editor">
            <div
              className={
                hasPreparedOrLegacyMedia(
                  profile.avatarMedia,
                  profile.avatarUrl
                )
                  ? "avatar account-avatar-preview has-image"
                  : "avatar account-avatar-preview profile-avatar-empty"
              }
            >
              {hasPreparedOrLegacyMedia(
                profile.avatarMedia,
                profile.avatarUrl
              ) ? (
                <PreparedMedia
                  alt=""
                  descriptor={profile.avatarMedia}
                  fallbackUrl={profile.avatarUrl}
                  height={112}
                  sizes="112px"
                  width={112}
                />
              ) : (
                <UserRound aria-hidden="true" />
              )}
            </div>
            <div className="account-avatar-copy">
              <strong>{t("profile.avatarTitle")}</strong>
              <span>{t("profile.avatarHint")}</span>
              <label
                className={
                  avatarSaveState === "saving"
                    ? "secondary-button disabled"
                    : "secondary-button"
                }
              >
                <Camera size={17} />
                {avatarSaveState === "saving"
                  ? t("profile.mediaProcessing")
                  : avatarSaveState === "error"
                    ? t("common.retry")
                    : t("profile.changeAvatar")}
                <input
                  accept="image/jpeg,image/png,image/webp"
                  className="sr-only"
                  data-testid="profile-avatar-input"
                  disabled={avatarSaveState === "saving"}
                  type="file"
                  onChange={(event) => {
                    const file = event.target.files?.[0];
                    if (file) {
                      void uploadAvatar(file);
                    }
                    event.target.value = "";
                  }}
                />
              </label>
              {hasPreparedOrLegacyMedia(
                profile.avatarMedia,
                profile.avatarUrl
              ) ? (
                <button
                  className="secondary-button media-delete-button"
                  disabled={avatarSaveState === "saving"}
                  onClick={() => void deleteAvatar()}
                  type="button"
                >
                  {t("profile.deleteAvatar")}
                </button>
              ) : null}
            </div>
          </div>

          {avatarMediaMessage && avatarSaveState === "error" ? (
            <p
              aria-live="polite"
              className="media-upload-status error"
              role="status"
            >
              {avatarMediaMessage}
            </p>
          ) : null}

          <div className="account-form-grid">
            <AccountField
              autoComplete="nickname"
              icon={<UserRound size={18} />}
              label="Ник"
              maxLength={15}
              value={displayName}
              onChange={updateDisplayName}
            />
            <AccountEmailIdentity
              cancelRequestId={emailCancelRequestId}
              email={user?.email ?? null}
              hasPassword={user?.has_password === true}
              onBeforeSave={saveAccount}
              onBusyChange={setEmailBusy}
              onDirtyChange={setEmailDirty}
              onLinked={applyLinkedEmail}
              saveRequestId={emailSaveRequestId}
            />
            <AccountField
              icon={<DiscordIcon aria-hidden="true" size={18} />}
              label="Discord"
              maxLength={64}
              value={contactValues.get("Discord") ?? ""}
              onChange={(value) => updateContact("Discord", value)}
            />
            <AccountSteamIdentity
              steamAuthStatus={steamAuthStatus}
              steamId={user?.steam_id ?? null}
              steamLinked={user?.steam_linked === true}
            />
            <AccountField
              icon={<MapPin size={18} />}
              label="Регион"
              maxLength={40}
              value={contactValues.get("Регион") ?? ""}
              onChange={(value) => updateContact("Регион", value)}
            />
          </div>

          {message && saveState === "error" ? (
            <ValidationMessage
              message={message}
              testId="profile-account-validation"
            />
          ) : null}

          <div className="profile-footer-actions account-card-actions">
            <ProfileActionButtons
              cancelTestId="profile-cancel-account-button"
              hasChanges={combinedHasChanges}
              onCancel={cancelAccountChanges}
              onSave={requestProfileSave}
              saveState={effectiveSaveState}
              saveTestId="profile-save-account-button"
            />
          </div>
        </div>

        <div className="account-settings-side">
          {user?.has_password === false ? (
            <div className="panel account-settings-card account-security-card account-passwordless-card">
              <div>
                <h3 className="panel-title">
                  {t("profile.accountLoginTitle")}
                </h3>
                <p className="description-text">
                  {t("profile.passwordlessCopy")}
                </p>
                {user.email ? (
                  <p className="description-text">
                    {t("profile.passwordlessEmailCopy")}
                  </p>
                ) : null}
              </div>
              {user.email ? (
                <Link
                  className="secondary-button"
                  href="/reset-password?returnTo=%2Fprofile%2Fme%3Ftab%3Daccount"
                >
                  {t("profile.setPassword")}
                </Link>
              ) : null}
            </div>
          ) : (
            <form
              autoComplete="on"
              className="panel account-settings-card account-security-card"
              id="account-password-change-form"
              method="post"
              name="account-password-change-form"
              onSubmit={(event) => {
                event.preventDefault();
                void requestSecuritySave(event.currentTarget);
              }}
            >
              <input
                autoComplete="username"
                className="sr-only"
                defaultValue={user?.email ?? profile.handle}
                id="account-username"
                name="username"
                tabIndex={-1}
                type="text"
              />
              <div className="account-security-fields">
                <label className="field account-field" htmlFor="current-password">
                  <span className="label">{t("profile.currentPassword")}</span>
                  <span className="account-input-wrap">
                    <KeyRound aria-hidden="true" size={18} />
                    <input
                      autoComplete="current-password"
                      className="input account-input"
                      id="current-password"
                      maxLength={128}
                      name="current_password"
                      onInput={resetSecurityFeedback}
                      required
                      type="password"
                    />
                  </span>
                </label>
                <label className="field account-field" htmlFor="new-password">
                  <span className="label">{t("auth.newPassword")}</span>
                  <span className="account-input-wrap">
                    <KeyRound aria-hidden="true" size={18} />
                    <input
                      autoComplete="new-password"
                      className="input account-input"
                      id="new-password"
                      maxLength={128}
                      minLength={10}
                      name="new_password"
                      onInput={resetSecurityFeedback}
                      placeholder={t("profile.passwordMinimum")}
                      required
                      type="password"
                    />
                  </span>
                </label>
                <label className="field account-field" htmlFor="confirm-password">
                  <span className="label">{t("auth.confirmNewPassword")}</span>
                  <span className="account-input-wrap">
                    <KeyRound aria-hidden="true" size={18} />
                    <input
                      autoComplete="new-password"
                      className="input account-input"
                      id="confirm-password"
                      maxLength={128}
                      minLength={10}
                      name="confirm_password"
                      onInput={resetSecurityFeedback}
                      required
                      type="password"
                    />
                  </span>
                </label>
              </div>
              {securityMessage && securitySaveState === "error" ? (
                <ValidationMessage
                  message={securityMessage}
                  testId="profile-security-validation"
                />
              ) : null}
              {securitySaveState === "saved" ? (
                <div
                  aria-live="polite"
                  className="account-help"
                  data-testid="profile-password-change-success"
                  role="status"
                >
                  Пароль изменён.
                </div>
              ) : null}
              <div className="profile-footer-actions account-card-actions">
                <button
                  className="primary-button"
                  data-testid="profile-save-security-button"
                  disabled={securitySaveState === "saving"}
                  type="submit"
                >
                  <Save size={18} />
                  {securitySaveState === "saving"
                    ? t("common.saving")
                    : securitySaveState === "saved"
                      ? "Пароль изменён"
                      : t("auth.resetPassword")}
                </button>
              </div>
            </form>
          )}

          <div className="panel account-settings-card account-signoff-card">
            <div className="account-signoff-copy">
              <span
                className="account-signoff-icon"
                aria-hidden="true"
              >
                <HeartHandshake size={24} />
              </span>
              <div>
                <h3>{t("profile.communityTitle")}</h3>
                <p>{t("profile.communityThanks")}</p>
                <p className="account-support-message">
                  <span>{t("profile.communitySupportQuestion")}</span>
                  <span>{t("profile.communitySupportAction")}</span>
                </p>
                <a
                  className="account-support-link"
                  href="/info#support"
                >
                  <Mail aria-hidden="true" size={15} />
                  {t("profile.communitySupportLink")}
                </a>
              </div>
            </div>
            <div className="account-signoff-actions">
              <button
                className="secondary-button account-signoff-logout"
                data-testid="profile-logout-button"
                disabled={isLoggingOut}
                onClick={logout}
                type="button"
              >
                <LogOut size={17} aria-hidden="true" />
                {isLoggingOut
                  ? t("profile.loggingOut")
                  : t("profile.logout")}
              </button>
              {logoutError ? (
                <span
                  className="account-signoff-error"
                  role="alert"
                >
                  {logoutError}
                </span>
              ) : null}
            </div>
          </div>
        </div>
      </div>
    </section>
  );
}
