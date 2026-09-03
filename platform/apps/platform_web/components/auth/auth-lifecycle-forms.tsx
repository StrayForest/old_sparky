"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { FormEvent, useEffect, useState, useTransition } from "react";
import { CheckCircle2 } from "lucide-react";
import { useAuth } from "@/components/auth/auth-provider";
import { TurnstileWidget } from "@/components/auth/turnstile-widget";
import { useAuthSecurityConfig } from "@/components/auth/use-auth-security-config";
import { useResendCooldown } from "@/components/auth/use-resend-cooldown";
import { useI18n } from "@/components/i18n-provider";
import { safeAuthReturnPath } from "@/lib/auth-navigation";
import {
  PlatformApiError,
  platformApiRequest
} from "@/lib/platform-api";
import type { PlatformAuthSessionResponse } from "@/lib/platform-types";

type AcceptedResponse = { accepted: boolean; retry_after_seconds?: number };
type PasswordResetMode = "request" | "code" | "password" | "verified";

const resetFormIdentity: Record<Exclude<PasswordResetMode, "verified">, string> = {
  request: "password-reset-request-form",
  code: "password-reset-code-form",
  password: "password-reset-password-form"
};

export function PasswordResetForm({
  returnTo,
  variant = "reset"
}: {
  returnTo?: string;
  variant?: "reset" | "verification";
} = {}) {
  const { setUser } = useAuth();
  const { t } = useI18n();
  const router = useRouter();
  const [mode, setMode] = useState<PasswordResetMode>("request");
  const security = useAuthSecurityConfig();
  const [email, setEmail] = useState("");
  const [submittedEmail, setSubmittedEmail] = useState("");
  const [code, setCode] = useState("");
  const [errorMessage, setErrorMessage] = useState("");
  const [turnstileToken, setTurnstileToken] = useState<string | null>(null);
  const [turnstileResetSignal, setTurnstileResetSignal] = useState(0);
  const [isPending, startTransition] = useTransition();
  const [isResending, setIsResending] = useState(false);
  const resendCooldown = useResendCooldown();
  const turnstileSiteKey = security.config?.turnstile_mode !== "off"
    ? security.config?.turnstile_site_key ?? null
    : null;
  const turnstileMode = security.config?.turnstile_mode ?? "off";
  const [turnstileChallengeRequired, setTurnstileChallengeRequired] = useState(false);
  const turnstileRequired = turnstileChallengeRequired;
  const securityReady = security.status === "ready" || security.status === "fallback";

  useEffect(() => {
    setTurnstileToken(null);
    setTurnstileChallengeRequired(false);
  }, [turnstileMode, turnstileSiteKey]);

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setErrorMessage("");
    if (mode === "request" && (!securityReady || (turnstileSiteKey && turnstileRequired && !turnstileToken))) {
      return;
    }

    const formData = new FormData(event.currentTarget);
    const submittedNewPassword = mode === "password"
      ? String(formData.get("new_password") ?? "")
      : "";
    const submittedConfirmPassword = mode === "password"
      ? String(formData.get("confirm_password") ?? "")
      : "";

    if (mode === "password" && submittedNewPassword !== submittedConfirmPassword) {
      setErrorMessage(t("auth.passwordsDoNotMatch"));
      return;
    }

    startTransition(async () => {
      try {
        if (mode === "password") {
          const authResponse = await platformApiRequest<PlatformAuthSessionResponse>("/auth/password-reset/confirm", {
            method: "POST",
            csrfPolicy: "origin-only",
            body: JSON.stringify({
              email: submittedEmail,
              code,
              new_password: submittedNewPassword
            })
          });
          setUser(authResponse.user);
          if (variant === "verification") {
            setMode("verified");
          } else {
            router.replace(safeAuthReturnPath(returnTo) === "/" ? "/profile/me" : safeAuthReturnPath(returnTo));
            router.refresh();
          }
        } else if (mode === "code") {
          await platformApiRequest<AcceptedResponse>("/auth/password-reset/verify-code", {
            method: "POST",
            csrfPolicy: "origin-only",
            body: JSON.stringify({
              email: submittedEmail,
              code
            })
          });
          setMode("password");
        } else {
          const normalizedEmail = email.trim().toLowerCase();
          const response = await platformApiRequest<AcceptedResponse>("/auth/password-reset/request", {
            method: "POST",
            csrfPolicy: "origin-only",
            body: JSON.stringify({
              email: normalizedEmail,
              turnstile_token: turnstileToken ?? undefined
            })
          });
          setCode("");
          setSubmittedEmail(normalizedEmail);
          setTurnstileToken(null);
          setMode("code");
          resendCooldown.start(response.retry_after_seconds ?? 60);
        }
      } catch (error) {
        setErrorMessage(lifecycleErrorMessage(
          error,
          mode === "verified" ? "request" : mode,
          t
        ));
        if (turnstileSiteKey && mode === "request") {
          if (isHumanVerificationRequired(error)) {
            setTurnstileChallengeRequired(true);
          }
          setTurnstileToken(null);
          setTurnstileResetSignal((current) => current + 1);
        }
      }
    });
  }

  async function resendResetCode() {
    if (mode !== "code" || isResending || resendCooldown.isCoolingDown) {
      return;
    }
    setErrorMessage("");
    setIsResending(true);
    try {
      const response = await platformApiRequest<AcceptedResponse>("/auth/password-reset/request", {
        method: "POST",
        csrfPolicy: "origin-only",
        body: JSON.stringify({ email: submittedEmail })
      });
      setCode("");
      resendCooldown.start(response.retry_after_seconds ?? 60);
    } catch (error) {
      if (error instanceof PlatformApiError && error.status === 429 && error.retryAfterSeconds) {
        resendCooldown.start(error.retryAfterSeconds);
      } else {
        setErrorMessage(lifecycleErrorMessage(error, "request", t));
      }
    } finally {
      setIsResending(false);
    }
  }

  if (mode === "verified") {
    return (
      <LifecycleSuccess
        actionLabel={t("auth.openProfile")}
        href="/profile/me"
        text={t("auth.verificationCompleteCopy")}
        title={t("auth.verificationCompleteTitle")}
      />
    );
  }

  const canSubmit = (mode !== "request" || (
    securityReady && (!turnstileSiteKey || !turnstileRequired || Boolean(turnstileToken))
  ))
    && !isPending;
  const formIdentity = resetFormIdentity[mode];

  return (
    <form
      autoComplete="on"
      className="auth-form"
      id={formIdentity}
      key={mode}
      method="post"
      name={formIdentity}
      onSubmit={submit}
    >
      <p className="description-text">
        {t(mode === "request"
          ? variant === "verification" ? "auth.verificationPendingCopy" : "auth.resetRequestCopy"
          : mode === "code"
            ? variant === "verification" ? "auth.verificationCodeCopy" : "auth.resetCodeCopy"
            : variant === "verification" ? "auth.pendingRecoveryPasswordCopy" : "auth.resetConfirmCopy")}
      </p>
      {errorMessage ? <div className="auth-error" role="alert">{errorMessage}</div> : null}
      {mode === "request" ? (
        <label className="field" htmlFor="password-reset-email">
          <span className="label">{t("auth.email")}</span>
          <input
            autoComplete="email"
            className="input"
            disabled={isPending}
            id="password-reset-email"
            maxLength={254}
            name="email"
            onChange={(event) => setEmail(event.target.value)}
            required
            type="email"
            value={email}
          />
        </label>
      ) : mode === "code" ? (
        <VerificationCodeField
          code={code}
          disabled={isPending || isResending}
          id="password-reset-code"
          name="code"
          onChange={setCode}
          t={t}
        />
      ) : (
        <>
          <input
            aria-label={t("auth.email")}
            autoComplete="username"
            className="sr-only"
            defaultValue={submittedEmail}
            id="username"
            name="username"
            tabIndex={-1}
            type="email"
          />
          <label className="field" htmlFor="new-password">
            <span className="label">{t("auth.newPassword")}</span>
            <input
              autoComplete="new-password"
              className="input"
              id="new-password"
              maxLength={128}
              minLength={10}
              name="new_password"
              onInput={() => setErrorMessage("")}
              required
              type="password"
            />
          </label>
          <label className="field" htmlFor="confirm-password">
            <span className="label">{t("auth.confirmNewPassword")}</span>
            <input
              autoComplete="new-password"
              className="input"
              id="confirm-password"
              maxLength={128}
              minLength={10}
              name="confirm_password"
              onInput={() => setErrorMessage("")}
              required
              type="password"
            />
          </label>
        </>
      )}
      {mode === "request" ? <SecurityConfigFeedback security={security} /> : null}
      {mode === "request" && turnstileSiteKey && turnstileRequired ? (
        <TurnstileWidget
          action="reset_request"
          onTokenChange={setTurnstileToken}
          resetSignal={turnstileResetSignal}
          siteKey={turnstileSiteKey}
        />
      ) : null}
      <div className={mode === "code" ? "auth-actions auth-code-actions" : "auth-actions"}>
        <button className="primary-button" disabled={!canSubmit} type="submit">
          {isPending
            ? t("common.processing")
            : t(mode === "request" ? "auth.sendResetCode" : mode === "code" ? "common.ok" : "auth.resetPassword")}
        </button>
        {mode === "code" ? (
          <button
            className="secondary-button"
            disabled={isPending || isResending || resendCooldown.isCoolingDown}
            onClick={() => void resendResetCode()}
            type="button"
          >
            {isResending
              ? t("auth.verificationResending")
              : resendCooldown.isCoolingDown
                ? t("auth.verificationResendCountdown", { seconds: resendCooldown.secondsRemaining })
                : t("auth.resetSendAgain")}
          </button>
        ) : (
          <Link className="secondary-button" href="/auth/login" prefetch={false}>{t("common.backToLogin")}</Link>
        )}
      </div>
    </form>
  );
}

function isHumanVerificationRequired(error: unknown): boolean {
  return error instanceof PlatformApiError && error.message === "Human verification is required.";
}

export function EmailVerificationForm() {
  return <PasswordResetForm variant="verification" />;
}

function VerificationCodeField({
  code,
  disabled = false,
  id,
  name,
  onChange,
  t
}: {
  code: string;
  disabled?: boolean;
  id?: string;
  name?: string;
  onChange: (value: string) => void;
  t: (key: string) => string;
}) {
  return (
    <label className="field" htmlFor={id}>
      <span className="label">{t("auth.verificationCode")}</span>
      <input
        autoComplete="one-time-code"
        className="input auth-code-input"
        disabled={disabled}
        id={id}
        inputMode="numeric"
        maxLength={6}
        minLength={6}
        name={name}
        onChange={(event) => onChange(event.target.value.replace(/\D/gu, "").slice(0, 6))}
        pattern="[0-9]{6}"
        required
        value={code}
      />
    </label>
  );
}

function SecurityConfigFeedback({
  security
}: {
  security: ReturnType<typeof useAuthSecurityConfig>;
}) {
  const { t } = useI18n();
  if (security.status === "loading") {
    return <p aria-live="polite" className="auth-security-status" role="status">{t("auth.securityConfigLoading")}</p>;
  }
  if (security.status === "error") {
    return (
      <div className="auth-security-status auth-security-status-error" role="alert">
        <span>{t("auth.securityConfigError")}</span>
        <button className="auth-turnstile-retry" onClick={security.retry} type="button">
          {t("auth.securityConfigRetry")}
        </button>
      </div>
    );
  }
  return null;
}

function LifecycleSuccess({
  actionLabel,
  href,
  onAction,
  text,
  title
}: {
  actionLabel: string;
  href?: string;
  onAction?: () => void;
  text: string;
  title: string;
}) {
  return (
    <div className="auth-lifecycle-state" role="status">
      <span className="auth-lifecycle-icon success"><CheckCircle2 aria-hidden="true" /></span>
      <h3>{title}</h3>
      <p>{text}</p>
      {href ? (
        <Link className="primary-button" href={href}>{actionLabel}</Link>
      ) : (
        <button className="secondary-button" onClick={onAction} type="button">{actionLabel}</button>
      )}
    </div>
  );
}

function lifecycleErrorMessage(
  error: unknown,
  operation: "request" | "code" | "password" | "resend" | "verification",
  t: (key: string) => string
): string {
  if (error instanceof PlatformApiError) {
    if (error.message === "Human verification is required." || error.message === "Human verification failed.") {
      return t("auth.turnstileRejected");
    }
    if (error.message === "Human verification is temporarily unavailable.") {
      return t("auth.turnstileUnavailable");
    }
    if ((operation === "code" || operation === "verification") && error.status === 400) {
      return t("auth.verificationCodeInvalid");
    }
    if (operation === "password" && (error.status === 400 || error.status === 409)) {
      return t(error.status === 409 ? "auth.resetPasswordUnchanged" : "auth.verificationCodeInvalid");
    }
  }
  if (operation === "password" || operation === "code") {
    return t("auth.resetConfirmFailed");
  }
  return t(operation === "resend" || operation === "verification" ? "auth.verificationResendFailed" : "auth.resetRequestFailed");
}
