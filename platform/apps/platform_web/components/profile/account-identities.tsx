"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { KeyRound, Mail, X } from "lucide-react";
import { useAuth } from "@/components/auth/auth-provider";
import { SteamIcon } from "@/components/icons/brand-icons";
import { useResendCooldown } from "@/components/auth/use-resend-cooldown";
import { useI18n } from "@/components/i18n-provider";
import { steamCompletionPath } from "@/lib/auth-navigation";
import {
  PlatformApiError,
  platformApiMessage,
  platformApiRequest
} from "@/lib/platform-api";
import type { PlatformUser } from "@/lib/platform-types";

type AcceptedResponse = {
  accepted: boolean;
  retry_after_seconds?: number;
};

type SteamAuthStartResponse = {
  authorization_url: string;
  expires_at: string;
};

type EmailFlowMode = "link" | "change";
type EmailStep = "idle" | "password" | "code";

function normalizeEmail(value: string) {
  return value.trim().toLowerCase();
}

function isSimpleValidEmail(value: string) {
  return value.length <= 254 && /^[^\s@]+@[^\s@]+\.[^\s@]+$/u.test(value);
}

export function AccountEmailIdentity({
  cancelRequestId,
  email,
  hasPassword,
  onBeforeSave,
  onBusyChange,
  onDirtyChange,
  onLinked,
  saveRequestId
}: {
  cancelRequestId: number;
  email: string | null;
  hasPassword: boolean;
  onBeforeSave: () => Promise<boolean>;
  onBusyChange: (busy: boolean) => void;
  onDirtyChange: (dirty: boolean) => void;
  onLinked: (user: PlatformUser) => void;
  saveRequestId: number;
}) {
  const { t } = useI18n();
  const [emailStep, setEmailStep] = useState<EmailStep>("idle");
  const [candidateEmail, setCandidateEmail] = useState(email ?? "");
  const [code, setCode] = useState("");
  const [message, setMessage] = useState("");
  const [emailTouched, setEmailTouched] = useState(false);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [isResending, setIsResending] = useState(false);
  const lastSaveRequestRef = useRef(saveRequestId);
  const lastCancelRequestRef = useRef(cancelRequestId);
  const resendCooldown = useResendCooldown();

  const currentEmail = normalizeEmail(email ?? "");
  const normalizedCandidateEmail = normalizeEmail(candidateEmail);
  const emailMode: EmailFlowMode = currentEmail ? "change" : "link";
  const emailChanged = normalizedCandidateEmail !== currentEmail;
  const emailIsValid = isSimpleValidEmail(normalizedCandidateEmail);
  const validationMessage = emailTouched && emailChanged && !emailIsValid
    ? "Введите корректную почту."
    : "";

  const resetEmailDraft = useCallback(() => {
    setCandidateEmail(email ?? "");
    setEmailStep("idle");
    setCode("");
    setMessage("");
    setEmailTouched(false);
  }, [email]);

  useEffect(() => {
    resetEmailDraft();
  }, [resetEmailDraft]);

  useEffect(() => {
    onDirtyChange(emailChanged);
  }, [emailChanged, onDirtyChange]);

  useEffect(() => {
    onBusyChange(isSubmitting || isResending);
  }, [isResending, isSubmitting, onBusyChange]);

  useEffect(() => {
    if (cancelRequestId === lastCancelRequestRef.current) {
      return;
    }
    lastCancelRequestRef.current = cancelRequestId;
    resetEmailDraft();
  }, [cancelRequestId, resetEmailDraft]);

  function closeEmailDialog() {
    setEmailStep("idle");
    setCode("");
    setMessage("");
  }

  const requestEmailCode = useCallback(async (submittedCurrentPassword = "") => {
    if (!emailIsValid || isSubmitting) {
      setEmailTouched(true);
      setMessage("Введите корректную почту.");
      return;
    }
    if (emailMode === "change" && !submittedCurrentPassword) {
      setMessage("Введите текущий пароль.");
      return;
    }

    setIsSubmitting(true);
    setMessage("");
    const profileSaved = await onBeforeSave();
    if (!profileSaved) {
      setMessage("Не удалось сохранить данные профиля.");
      setIsSubmitting(false);
      return;
    }

    const endpoint = emailMode === "change" ? "/auth/email-change/request" : "/auth/email-link/request";
    try {
      const response = await platformApiRequest<AcceptedResponse>(endpoint, {
        method: "POST",
        body: JSON.stringify(
          emailMode === "change"
            ? { email: normalizedCandidateEmail, current_password: submittedCurrentPassword }
            : { email: normalizedCandidateEmail }
        )
      });
      setCandidateEmail(normalizedCandidateEmail);
      setCode("");
      setEmailStep("code");
      resendCooldown.start(response.retry_after_seconds ?? 60);
    } catch (error) {
      setMessage(platformApiMessage(error, "Не удалось отправить код на почту."));
    } finally {
      setIsSubmitting(false);
    }
  }, [emailIsValid, emailMode, isSubmitting, normalizedCandidateEmail, onBeforeSave, resendCooldown]);

  const beginEmailSave = useCallback(async () => {
    if (!emailChanged || isSubmitting) {
      return;
    }
    setEmailTouched(true);
    if (!emailIsValid) {
      setMessage("Введите корректную почту.");
      return;
    }
    if (emailMode === "change") {
      if (!hasPassword) {
        setMessage("Чтобы изменить почту, сначала установите пароль.");
        return;
      }
      setCode("");
      setMessage("");
      setEmailStep("password");
      return;
    }
    await requestEmailCode();
  }, [emailChanged, emailIsValid, emailMode, hasPassword, isSubmitting, requestEmailCode]);

  useEffect(() => {
    if (saveRequestId === lastSaveRequestRef.current) {
      return;
    }
    lastSaveRequestRef.current = saveRequestId;
    void beginEmailSave();
  }, [beginEmailSave, saveRequestId]);

  async function resendEmailCode() {
    if (isResending || resendCooldown.isCoolingDown) {
      return;
    }
    setIsResending(true);
    setMessage("");
    const endpoint = emailMode === "change" ? "/auth/email-change/resend" : "/auth/email-link/resend";
    try {
      const response = await platformApiRequest<AcceptedResponse>(endpoint, {
        method: "POST",
        body: JSON.stringify({ email: normalizedCandidateEmail })
      });
      setCode("");
      resendCooldown.start(response.retry_after_seconds ?? 60);
    } catch (error) {
      if (error instanceof PlatformApiError && error.status === 429 && error.retryAfterSeconds) {
        resendCooldown.start(error.retryAfterSeconds);
      } else {
        setMessage(platformApiMessage(error, "Не удалось повторно отправить код."));
      }
    } finally {
      setIsResending(false);
    }
  }

  async function confirmEmailCode() {
    if (!/^\d{6}$/u.test(code) || isSubmitting) {
      setMessage("Введите шестизначный код.");
      return;
    }
    setIsSubmitting(true);
    setMessage("");
    const endpoint = emailMode === "change" ? "/auth/email-change/confirm" : "/auth/email-link/confirm";
    try {
      const updatedUser = await platformApiRequest<PlatformUser>(endpoint, {
        method: "POST",
        body: JSON.stringify({ email: normalizedCandidateEmail, code })
      });
      setCandidateEmail(updatedUser.email ?? normalizedCandidateEmail);
      setEmailTouched(false);
      onLinked(updatedUser);
      setEmailStep("idle");
      setCode("");
      setMessage("");
    } catch (error) {
      setMessage(platformApiMessage(error, "Код недействителен или устарел."));
    } finally {
      setIsSubmitting(false);
    }
  }

  return (
    <>
      <label className="field account-field account-email-identity">
        <span className="label">{t("profile.email")}</span>
        <span className="account-input-wrap">
          <Mail aria-hidden="true" size={18} />
          <input
            aria-invalid={emailTouched && emailChanged && !emailIsValid}
            aria-label={t("profile.email")}
            autoComplete="email"
            className="input account-input"
            data-testid="profile-account-email"
            maxLength={254}
            onBlur={() => setEmailTouched(true)}
            onChange={(event) => {
              setCandidateEmail(event.target.value);
              setMessage("");
            }}
            placeholder="Не заполнено"
            type="email"
            value={candidateEmail}
          />
        </span>
        <span aria-hidden="true" className="account-identity-button sr-only">{candidateEmail}</span>
        {emailStep === "idle" && (message || validationMessage) ? (
          <span className="account-identity-message" role="alert">{message || validationMessage}</span>
        ) : null}
        <IdentityStyles />
      </label>

      {emailStep === "password" ? (
        <div className="account-confirm-backdrop" role="presentation">
          <form
            aria-labelledby="email-password-confirm-title"
            aria-modal="true"
            autoComplete="on"
            className="panel account-confirm-dialog"
            id="account-email-change-password-form"
            method="post"
            name="account-email-change-password-form"
            role="dialog"
            onSubmit={(event) => {
              event.preventDefault();
              const formData = new FormData(event.currentTarget);
              void requestEmailCode(
                String(formData.get("current_password") ?? "")
              );
            }}
          >
            <input
              autoComplete="username"
              className="sr-only"
              defaultValue={currentEmail}
              id="email-change-username"
              name="username"
              tabIndex={-1}
              type="email"
            />
            <div>
              <h3 className="panel-title" id="email-password-confirm-title">Подтвердите смену почты</h3>
              <p className="description-text">Введите текущий пароль аккаунта.</p>
            </div>
            <label className="field account-field" htmlFor="email-change-current-password">
              <span className="label">Текущий пароль</span>
              <span className="account-input-wrap">
                <KeyRound aria-hidden="true" size={18} />
                <input
                  autoComplete="current-password"
                  autoFocus
                  className="input account-input"
                  id="email-change-current-password"
                  maxLength={128}
                  name="current_password"
                  onInput={() => setMessage("")}
                  required
                  type="password"
                />
              </span>
            </label>
            {message ? <span className="account-identity-message" role="alert">{message}</span> : null}
            <div className="account-confirm-actions">
              <button className="ghost-button" disabled={isSubmitting} onClick={closeEmailDialog} type="button">Отмена</button>
              <button className="primary-button" disabled={isSubmitting} type="submit">
                {isSubmitting ? "Проверяем..." : "Продолжить"}
              </button>
            </div>
          </form>
        </div>
      ) : null}

      {emailStep === "code" ? (
        <div className="account-confirm-backdrop" role="presentation">
          <form
            aria-labelledby="email-code-confirm-title"
            aria-modal="true"
            className="panel account-confirm-dialog"
            role="dialog"
            onSubmit={(event) => {
              event.preventDefault();
              void confirmEmailCode();
            }}
          >
            <div>
              <h3 className="panel-title" id="email-code-confirm-title">Подтвердите новую почту</h3>
              <p className="description-text">Код отправлен на {normalizedCandidateEmail}</p>
            </div>
            <label className="field account-field">
              <span className="label">{t("auth.verificationCode")}</span>
              <span className="account-input-wrap">
                <Mail aria-hidden="true" size={18} />
                <input
                  aria-label={t("auth.verificationCode")}
                  autoComplete="one-time-code"
                  autoFocus
                  className="input account-input"
                  disabled={isSubmitting || isResending}
                  inputMode="numeric"
                  maxLength={6}
                  onChange={(event) => {
                    setCode(event.target.value.replace(/\D/gu, "").slice(0, 6));
                    setMessage("");
                  }}
                  pattern="[0-9]{6}"
                  placeholder="000000"
                  value={code}
                />
              </span>
            </label>
            {message ? <span className="account-identity-message" role="alert">{message}</span> : null}
            <div className="account-confirm-actions email-code-actions">
              <button className="ghost-button" disabled={isSubmitting || isResending} onClick={closeEmailDialog} type="button">Отмена</button>
              <button
                className="secondary-button"
                disabled={isSubmitting || isResending || resendCooldown.isCoolingDown}
                onClick={() => void resendEmailCode()}
                type="button"
              >
                {isResending
                  ? t("auth.verificationResending")
                  : resendCooldown.isCoolingDown
                    ? t("auth.verificationResendCountdown", { seconds: resendCooldown.secondsRemaining })
                    : t("auth.verificationSendAgain")}
              </button>
              <button className="primary-button" disabled={code.length !== 6 || isSubmitting} type="submit">
                {isSubmitting ? "Проверяем..." : "Подтвердить"}
              </button>
            </div>
          </form>
        </div>
      ) : null}
    </>
  );
}

export function AccountSteamIdentity({
  steamAuthStatus,
  steamId,
  steamLinked
}: {
  steamAuthStatus?: "error" | "success";
  steamId: string | null;
  steamLinked: boolean;
}) {
  const { t } = useI18n();
  const { user, setUser } = useAuth();
  const [isStarting, setIsStarting] = useState(false);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [message, setMessage] = useState(
    steamAuthStatus === "error"
      ? t("profile.steamLinkCallbackFailed")
      : ""
  );
  const [confirmUnlink, setConfirmUnlink] = useState(false);

  const currentSteamLinked = user?.steam_linked ?? steamLinked;
  const currentSteamId = user?.steam_id ?? steamId;

  async function startLink() {
    if (isStarting || currentSteamLinked) {
      return;
    }
    setIsStarting(true);
    setMessage("");
    try {
      const response = await platformApiRequest<SteamAuthStartResponse>("/auth/steam/link/start", {
        method: "POST",
        body: JSON.stringify({ return_to: steamCompletionPath("/profile/me?tab=account", "link") })
      });
      window.location.assign(response.authorization_url);
    } catch (error) {
      setMessage(platformApiMessage(error, t("profile.steamLinkStartFailed")));
      setIsStarting(false);
    }
  }

  async function unlinkSteam() {
    if (isSubmitting) {
      return;
    }
    setIsSubmitting(true);
    setMessage("");
    try {
      const updatedUser = await platformApiRequest<PlatformUser>("/auth/identities/steam", { method: "DELETE" });
      setUser(updatedUser);
      setConfirmUnlink(false);
    } catch (error) {
      setMessage(platformApiMessage(error, "Не удалось отвязать Steam."));
      setConfirmUnlink(false);
    } finally {
      setIsSubmitting(false);
    }
  }

  return (
    <>
      <div className="field account-field account-steam-identity">
        <span className="label">Steam</span>
        {currentSteamLinked ? (
          <div className="account-identity-linked-row">
            <SteamIcon aria-hidden="true" size={20} />
            <span className="account-identity-value">{currentSteamId ?? "Steam привязан"}</span>
            <button
              aria-label="Отвязать Steam"
              className="account-identity-unlink"
              onClick={() => setConfirmUnlink(true)}
              type="button"
            >
              <X aria-hidden="true" size={20} />
            </button>
          </div>
        ) : (
          <button
            className="account-steam-link-button"
            disabled={isStarting}
            onClick={() => void startLink()}
            type="button"
          >
            <SteamIcon aria-hidden="true" size={20} />
            <span>{isStarting ? t("auth.steamStarting") : "Привязать Steam"}</span>
          </button>
        )}
        {steamAuthStatus === "success" && currentSteamLinked ? (
          <span className="sr-only" role="status">{t("profile.steamLinkSuccess")}</span>
        ) : null}
        {message ? <span className="account-identity-message" role="alert">{message}</span> : null}
        <IdentityStyles />
      </div>

      {confirmUnlink ? (
        <div className="account-confirm-backdrop" role="presentation">
          <div aria-modal="true" className="panel account-confirm-dialog" role="dialog">
            <div>
              <h3 className="panel-title">Отвязать Steam?</h3>
              <p className="description-text">
                После отвязки вход через Steam перестанет работать для этого аккаунта.
              </p>
            </div>
            <div className="account-confirm-actions">
              <button className="ghost-button" disabled={isSubmitting} onClick={() => setConfirmUnlink(false)} type="button">Нет</button>
              <button className="primary-button" disabled={isSubmitting} onClick={() => void unlinkSteam()} type="button">
                {isSubmitting ? "Сохранение..." : "Да"}
              </button>
            </div>
          </div>
        </div>
      ) : null}
    </>
  );
}

function IdentityStyles() {
  return (
    <style jsx global>{`
      .account-email-identity,
      .account-steam-identity {
        min-width: 0;
      }

      .account-email-identity .account-input-wrap {
        width: 100%;
        box-sizing: border-box;
      }

      .account-email-identity .account-input {
        color: var(--ui-text, var(--text-main));
        background: transparent;
      }

      .account-steam-link-button,
      .account-identity-linked-row {
        width: 100%;
        min-height: 48px;
        box-sizing: border-box;
        display: flex;
        align-items: center;
        gap: 11px;
        padding: 0 14px;
        color: var(--ui-text-soft, var(--text-soft));
        background: var(--bg-input);
        border: 1px solid var(--border-main);
        border-radius: var(--ui-radius-md, 12px);
      }

      .account-steam-link-button {
        justify-content: flex-start;
        font: inherit;
        text-align: left;
        cursor: pointer;
        transition: border-color .16s ease, background .16s ease, color .16s ease, box-shadow .16s ease;
      }

      .account-steam-link-button:hover,
      .account-steam-link-button:focus-visible {
        color: var(--ui-text, var(--text-main));
        background: var(--ui-surface-hover, var(--bg-card-soft));
        border-color: rgba(167, 139, 250, .52);
        box-shadow: 0 0 0 3px rgba(139, 92, 246, .08);
        outline: none;
      }

      .account-steam-link-button:disabled {
        cursor: wait;
        opacity: .7;
      }

      .account-steam-link-button svg,
      .account-identity-linked-row > svg {
        flex: 0 0 auto;
      }

      .account-identity-linked-row {
        padding-right: 6px;
      }

      .account-identity-value {
        min-width: 0;
        flex: 1 1 auto;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
        color: var(--ui-text, var(--text-main));
        font-variant-numeric: tabular-nums;
      }

      .account-identity-unlink {
        width: 38px;
        height: 38px;
        flex: 0 0 38px;
        display: grid;
        place-items: center;
        padding: 0;
        color: var(--ui-danger, var(--red));
        background: transparent;
        border: 0;
        border-radius: 9px;
        cursor: pointer;
        transition: color .16s ease, background .16s ease;
      }

      .account-identity-unlink:hover,
      .account-identity-unlink:focus-visible {
        color: #fff;
        background: rgba(251, 113, 133, .16);
        outline: none;
      }

      .account-identity-message {
        display: block;
        margin-top: 8px;
        color: var(--red);
        font-size: 12px;
        line-height: 1.4;
      }

      .email-code-actions {
        flex-wrap: wrap;
      }

      @media (max-width: 720px) {
        .email-code-actions {
          align-items: stretch;
          flex-direction: column;
        }
      }
    `}</style>
  );
}
