"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { SteamIcon } from "@/components/icons/brand-icons";
import { TurnstileWidget } from "@/components/auth/turnstile-widget";
import type { useAuthSecurityConfig } from "@/components/auth/use-auth-security-config";
import { useI18n } from "@/components/i18n-provider";
import { steamCompletionPath } from "@/lib/auth-navigation";
import { PlatformApiError, platformApiRequest } from "@/lib/platform-api";

type SteamAuthStartResponse = {
  authorization_url: string;
  expires_at: string;
};

export function SteamAuthAction({
  label,
  returnTo,
  security
}: {
  label: string;
  returnTo: string;
  security: ReturnType<typeof useAuthSecurityConfig>;
}) {
  const { t } = useI18n();
  const [expanded, setExpanded] = useState(false);
  const [turnstileToken, setTurnstileToken] = useState<string | null>(null);
  const [turnstileResetSignal, setTurnstileResetSignal] = useState(0);
  const [isStarting, setIsStarting] = useState(false);
  const [errorMessage, setErrorMessage] = useState("");
  const isStartingRef = useRef(false);
  const turnstileSiteKey = security.config?.turnstile_mode !== "off"
    ? security.config?.turnstile_site_key ?? null
    : null;
  const securityReady = security.status === "ready" || security.status === "fallback";

  const startSteamAuth = useCallback(async (token: string | null) => {
    if (isStartingRef.current) {
      return;
    }
    isStartingRef.current = true;
    setIsStarting(true);
    setErrorMessage("");
    try {
      const response = await platformApiRequest<SteamAuthStartResponse>("/auth/steam/login/start", {
        method: "POST",
        csrfPolicy: "origin-only",
        body: JSON.stringify({
          return_to: steamCompletionPath(returnTo),
          turnstile_token: token ?? undefined
        })
      });
      window.location.assign(response.authorization_url);
    } catch (error) {
      const needsHumanVerification = error instanceof PlatformApiError
        && error.message === "Human verification is required.";
      const turnstileFailed = error instanceof PlatformApiError
        && error.message === "Human verification failed.";
      const turnstileUnavailable = error instanceof PlatformApiError
        && error.message === "Human verification is temporarily unavailable.";
      setErrorMessage(
        needsHumanVerification
          ? ""
          : turnstileFailed
            ? t("auth.turnstileRejected")
            : turnstileUnavailable
              ? t("auth.turnstileUnavailable")
              : t("auth.steamStartFailed")
      );
      setTurnstileToken(null);
      isStartingRef.current = false;
      setIsStarting(false);
      if ((needsHumanVerification || turnstileFailed) && turnstileSiteKey) {
        setExpanded(true);
      }
      if (turnstileSiteKey) {
        setTurnstileResetSignal((current) => current + 1);
      }
    }
  }, [returnTo, t, turnstileSiteKey]);

  useEffect(() => {
    if (turnstileToken && !isStarting) {
      void startSteamAuth(turnstileToken);
    }
  }, [isStarting, startSteamAuth, turnstileToken]);

  const handleTurnstileToken = useCallback((token: string | null) => {
    setTurnstileToken(token);
    if (token) {
      setExpanded(false);
    }
  }, []);

  function begin() {
    if (!securityReady || isStarting || isStartingRef.current || expanded) {
      return;
    }
    void startSteamAuth(null);
  }

  return (
    <div className="steam-auth-action">
      <div className="auth-divider" aria-hidden="true"><span>{t("auth.or")}</span></div>
      <button
        className="secondary-button steam-auth-button"
        disabled={!securityReady || isStarting || expanded}
        onClick={begin}
        type="button"
      >
        <SteamIcon aria-hidden="true" size={19} />
        {isStarting ? t("auth.steamStarting") : label}
      </button>
      {expanded && turnstileSiteKey ? (
        <TurnstileWidget
          action="steam_login"
          onTokenChange={handleTurnstileToken}
          resetSignal={turnstileResetSignal}
          siteKey={turnstileSiteKey}
        />
      ) : null}
      {errorMessage ? <div className="auth-error" role="alert">{errorMessage}</div> : null}
    </div>
  );
}
