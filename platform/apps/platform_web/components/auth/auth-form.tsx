"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { FormEvent, useEffect, useState, useTransition } from "react";
import { LogIn, UserPlus } from "lucide-react";
import { useAuth } from "@/components/auth/auth-provider";
import { useI18n } from "@/components/i18n-provider";
import { SteamAuthAction } from "@/components/auth/steam-auth-action";
import { TurnstileWidget, type TurnstileAction } from "@/components/auth/turnstile-widget";
import { useAuthSecurityConfig } from "@/components/auth/use-auth-security-config";
import { useResendCooldown } from "@/components/auth/use-resend-cooldown";
import { safeAuthReturnPath } from "@/lib/auth-navigation";
import {
  PlatformApiError,
  platformApiRequest,
  resetPlatformCsrfToken
} from "@/lib/platform-api";
import type {
  PlatformAuthRegistrationResponse,
  PlatformAuthSessionResponse
} from "@/lib/platform-types";

type AuthFormProps = {
  mode: "login" | "register";
  returnTo?: string;
  steamAuthError?: boolean;
};

type AcceptedResponse = { accepted: boolean; retry_after_seconds?: number };

export function AuthForm({ mode, returnTo, steamAuthError = false }: AuthFormProps) {
  const { clearUser, setUser } = useAuth();
  const { t } = useI18n();
  const router = useRouter();
  const [verificationEmail, setVerificationEmail] = useState("");
  const [registrationStep, setRegistrationStep] = useState<"details" | "code">("details");
  const [verificationCode, setVerificationCode] = useState("");
  const [errorMessage, setErrorMessage] = useState("");
  const securityConfigState = useAuthSecurityConfig();
  const [turnstileToken, setTurnstileToken] = useState<string | null>(null);
  const [turnstileResetSignal, setTurnstileResetSignal] = useState(0);
  const [isPending, startTransition] = useTransition();
  const [isResending, setIsResending] = useState(false);
  const resendCooldown = useResendCooldown();
  const isRegister = mode === "register";
  const securityConfig = securityConfigState.config;
  const turnstileSiteKey = securityConfig?.turnstile_mode !== "off"
    ? securityConfig?.turnstile_site_key ?? null
    : null;
  const turnstileMode = securityConfig?.turnstile_mode ?? "off";
  const securityConfigReady = securityConfigState.status === "ready" || securityConfigState.status === "fallback";
  const registrationEnabled = !isRegister || securityConfig?.public_registration_enabled === true;
  const enteringCode = isRegister && registrationStep === "code";
  const turnstileAction: TurnstileAction = mode;
  const [turnstileChallengeRequired, setTurnstileChallengeRequired] = useState(false);
  const turnstileRequired = turnstileChallengeRequired;
  const canSubmit = (
    (enteringCode || (
      securityConfigReady
      && registrationEnabled
      && (!turnstileSiteKey || !turnstileRequired || Boolean(turnstileToken))
    ))
    && !isPending
  );

  useEffect(() => {
    setTurnstileToken(null);
    setTurnstileChallengeRequired(false);
  }, [turnstileMode, turnstileSiteKey]);

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setErrorMessage("");
    if (!canSubmit) {
      return;
    }

    const formData = new FormData(event.currentTarget);
    const submittedEmail = String(formData.get("username") ?? "").trim();
    const submittedPassword = String(
      formData.get(isRegister ? "new_password" : "current_password") ?? ""
    );
    const submittedDisplayName = String(formData.get("display_name") ?? "").trim();

    startTransition(async () => {
      try {
        if (enteringCode) {
          const authResponse = await platformApiRequest<PlatformAuthSessionResponse>("/auth/email-verification/confirm", {
            method: "POST",
            csrfPolicy: "origin-only",
            body: JSON.stringify({
              email: verificationEmail,
              code: verificationCode
            })
          });
          setUser(authResponse.user);
          router.replace("/profile/me");
          router.refresh();
          return;
        }

        const authResponse = await platformApiRequest<PlatformAuthSessionResponse | PlatformAuthRegistrationResponse>(isRegister ? "/auth/register" : "/auth/login", {
          method: "POST",
          csrfPolicy: "origin-only",
          body: JSON.stringify(isRegister
            ? {
                display_name: submittedDisplayName,
                email: submittedEmail,
                password: submittedPassword,
                turnstile_token: turnstileToken ?? undefined
              }
            : {
                email: submittedEmail,
                password: submittedPassword,
                turnstile_token: turnstileToken ?? undefined
              })
        });
        if (isRegister && "verification_required" in authResponse && authResponse.verification_required) {
          clearUser();
          resetPlatformCsrfToken();
          setVerificationEmail(submittedEmail.toLowerCase());
          setVerificationCode("");
          setTurnstileToken(null);
          setRegistrationStep("code");
          resendCooldown.start(authResponse.retry_after_seconds ?? 60);
          return;
        }
        setUser(authResponse.user);
        router.push(isRegister ? "/profile/me" : safeAuthReturnPath(returnTo));
        router.refresh();
      } catch (error) {
        setErrorMessage(authErrorMessage(error, enteringCode, isRegister, t));
        if (turnstileSiteKey && !enteringCode) {
          if (isHumanVerificationRequired(error)) {
            setTurnstileChallengeRequired(true);
          }
          setTurnstileToken(null);
          setTurnstileResetSignal((current) => current + 1);
        }
      }
    });
  }

  async function resendVerificationCode() {
    if (!enteringCode || isResending || resendCooldown.isCoolingDown) {
      return;
    }
    setErrorMessage("");
    setIsResending(true);
    try {
      const response = await platformApiRequest<AcceptedResponse>("/auth/email-verification/resend", {
        method: "POST",
        csrfPolicy: "origin-only",
        body: JSON.stringify({ email: verificationEmail })
      });
      setVerificationCode("");
      resendCooldown.start(response.retry_after_seconds ?? 60);
    } catch (error) {
      if (error instanceof PlatformApiError && error.status === 429 && error.retryAfterSeconds) {
        resendCooldown.start(error.retryAfterSeconds);
      } else {
        setErrorMessage(t("auth.verificationResendFailed"));
      }
    } finally {
      setIsResending(false);
    }
  }

  const formIdentity = enteringCode
    ? "registration-code-form"
    : isRegister
      ? "registration-form"
      : "login-form";
  const usernameId = isRegister ? "register-username" : "login-username";
  const passwordId = isRegister ? "new-password" : "current-password";
  const passwordName = isRegister ? "new_password" : "current_password";

  return (
    <main className="main auth-layout">
      <section className="panel panel-pad auth-panel" aria-label={t(isRegister ? "auth.accountCreation" : "auth.signIn")}>
        <div className="panel-title-row">
          <h2 className="panel-title">
            {isRegister ? <UserPlus size={18} aria-hidden="true" /> : <LogIn size={18} aria-hidden="true" />}
            {t(isRegister ? "auth.createAccount" : "auth.login")}
          </h2>
        </div>
        <p className="description-text">
          {t(enteringCode ? "auth.registrationCodeCopy" : isRegister ? "auth.registerCopy" : "auth.loginCopy")}
        </p>

        {steamAuthError ? (
          <div className="auth-error" role="alert">{t("auth.steamCallbackFailed")}</div>
        ) : null}
        {errorMessage ? <div className="auth-error" role="alert">{errorMessage}</div> : null}

        <form
          autoComplete="on"
          className="auth-form"
          id={formIdentity}
          key={formIdentity}
          method="post"
          name={formIdentity}
          onSubmit={handleSubmit}
        >
          {isRegister && !enteringCode ? (
            <label className="field" htmlFor="register-display-name">
              <span className="label">{t("auth.displayName")}</span>
              <input
                autoComplete="nickname"
                className="input"
                disabled={isPending}
                id="register-display-name"
                maxLength={15}
                minLength={2}
                name="display_name"
                required
              />
            </label>
          ) : null}

          {!enteringCode ? (
            <label className="field" htmlFor={usernameId}>
              <span className="label">{t("auth.email")}</span>
              <input
                autoCapitalize="none"
                autoComplete="username"
                className="input"
                id={usernameId}
                maxLength={254}
                name="username"
                required
                spellCheck={false}
                type="email"
              />
            </label>
          ) : null}

          {!enteringCode ? (
            <label className="field" htmlFor={passwordId}>
              <span className="label">{t("auth.password")}</span>
              <input
                autoComplete={isRegister ? "new-password" : "current-password"}
                className="input"
                id={passwordId}
                maxLength={128}
                minLength={isRegister ? 10 : undefined}
                name={passwordName}
                required
                type="password"
              />
            </label>
          ) : null}

          {enteringCode ? (
            <label className="field" htmlFor="registration-one-time-code">
              <span className="label">{t("auth.verificationCode")}</span>
              <input
                autoComplete="one-time-code"
                className="input auth-code-input"
                disabled={isPending || isResending}
                id="registration-one-time-code"
                inputMode="numeric"
                maxLength={6}
                minLength={6}
                name="one_time_code"
                onChange={(event) => setVerificationCode(event.target.value.replace(/\D/gu, "").slice(0, 6))}
                pattern="[0-9]{6}"
                required
                value={verificationCode}
              />
            </label>
          ) : null}

          {securityConfigState.status === "error" ? (
            <div className="auth-security-status auth-security-status-error" role="alert">
              <span>{t("auth.securityConfigError")}</span>
              <button
                className="auth-turnstile-retry"
                onClick={securityConfigState.retry}
                type="button"
              >
                {t("auth.securityConfigRetry")}
              </button>
            </div>
          ) : null}

          {isRegister && !enteringCode && securityConfigReady && !registrationEnabled ? (
            <p className="auth-security-status auth-security-status-error" role="alert">
              {t("auth.registrationClosed")}
            </p>
          ) : null}

          {!enteringCode && turnstileSiteKey && turnstileRequired && !turnstileToken ? (
            <TurnstileWidget
              action={turnstileAction}
              onTokenChange={setTurnstileToken}
              resetSignal={turnstileResetSignal}
              siteKey={turnstileSiteKey}
            />
          ) : null}

          <div className={enteringCode ? "auth-actions auth-code-actions" : "auth-actions"}>
            <button className="primary-button" disabled={!canSubmit} type="submit">
              {isPending
                ? t(enteringCode ? "common.processing" : isRegister ? "auth.creating" : "auth.signingIn")
                : t(enteringCode ? "auth.confirmCode" : isRegister ? "auth.createAccount" : "auth.login")}
            </button>
            {enteringCode ? (
              <button
                className="secondary-button"
                disabled={isPending || isResending || resendCooldown.isCoolingDown}
                onClick={() => void resendVerificationCode()}
                type="button"
              >
                {isResending
                  ? t("auth.verificationResending")
                  : resendCooldown.isCoolingDown
                    ? t("auth.verificationResendCountdown", { seconds: resendCooldown.secondsRemaining })
                    : t("auth.verificationSendAgain")}
              </button>
            ) : (
              <Link
                className="secondary-button"
                href={`${isRegister ? "/auth/login" : "/auth/register"}${returnTo ? `?returnTo=${encodeURIComponent(returnTo)}` : ""}`}
                prefetch={false}
              >
                {t(isRegister ? "auth.haveAccount" : "auth.createAccount")}
              </Link>
            )}
          </div>
          {!isRegister ? (
            <div className="auth-help-links">
              <Link href="/reset-password">{t("auth.forgotPassword")}</Link>
            </div>
          ) : null}
        </form>
        {securityConfig?.steam_login_enabled !== false ? (
          <SteamAuthAction
            label={t(isRegister ? "auth.steamCreate" : "auth.steamLogin")}
            returnTo={isRegister ? "/profile/me" : safeAuthReturnPath(returnTo)}
            security={securityConfigState}
          />
        ) : null}
      </section>
    </main>
  );
}

function isHumanVerificationRequired(error: unknown): boolean {
  return error instanceof PlatformApiError && error.message === "Human verification is required.";
}

function authErrorMessage(
  error: unknown,
  enteringCode: boolean,
  isRegister: boolean,
  t: (key: string) => string
): string {
  if (error instanceof PlatformApiError) {
    if (enteringCode && error.status === 400) {
      return t("auth.verificationCodeInvalid");
    }
    if (
      isRegister
      && !enteringCode
      && error.status === 429
    ) {
      return t("auth.registrationCooldown");
    }
    if (error.message === "Human verification is required." || error.message === "Human verification failed.") {
      return t("auth.turnstileRejected");
    }
    if (error.message === "Human verification is temporarily unavailable.") {
      return t("auth.turnstileUnavailable");
    }
  }
  return t(isRegister ? "auth.registrationFailed" : "auth.loginFailed");
}
