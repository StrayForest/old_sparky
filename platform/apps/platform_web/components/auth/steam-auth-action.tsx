"use client";

import { useCallback, useEffect, useState } from "react";
import { SteamIcon } from "@/components/icons/brand-icons";
import { TurnstileWidget } from "@/components/auth/turnstile-widget";
import type { useAuthSecurityConfig } from "@/components/auth/use-auth-security-config";
import { useI18n } from "@/components/i18n-provider";
import { steamCompletionPath } from "@/lib/auth-navigation";
import { platformApiRequest } from "@/lib/platform-api";

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
  const turnstileSiteKey = security.config?.turnstile_mode !== "off"
    ? security.config?.turnstile_site_key ?? null
    : null;
  const securityReady = security.status === "ready" || security.status === "fallback";

  const startSteamAuth = useCallback(async (token: string | null) => {
    if (isStarting) {
      return;
    }
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
    } catch {
      setErrorMessage(t("auth.steamStartFailed"));
      setTurnstileToken(null);
      setIsStarting(false);
      if (turnstileSiteKey) {
        setTurnstileResetSignal((current) => current + 1);
      }
    }
  }, [isStarting, returnTo, t, turnstileSiteKey]);

  useEffect(() => {
    if (expanded && turnstileToken && !isStarting) {
      void startSteamAuth(turnstileToken);
    }
  }, [expanded, isStarting, startSteamAuth, turnstileToken]);

  function begin() {
    if (!securityReady || isStarting) {
      return;
    }
    if (!turnstileSiteKey) {
      void startSteamAuth(null);
      return;
    }
    setExpanded(true);
  }

  return (
    <div className="steam-auth-action">
      <div className="auth-divider" aria-hidden="true"><span>{t("auth.or")}</span></div>
      <button
        className="secondary-button steam-auth-button"
        disabled={!securityReady || isStarting}
        onClick={begin}
        type="button"
      >
        <SteamIcon aria-hidden="true" size={19} />
        {isStarting ? t("auth.steamStarting") : label}
      </button>
      {expanded && turnstileSiteKey ? (
        <TurnstileWidget
          action="steam_login"
          onTokenChange={setTurnstileToken}
          resetSignal={turnstileResetSignal}
          siteKey={turnstileSiteKey}
        />
      ) : null}
      {errorMessage ? <div className="auth-error" role="alert">{errorMessage}</div> : null}
    </div>
  );
}
