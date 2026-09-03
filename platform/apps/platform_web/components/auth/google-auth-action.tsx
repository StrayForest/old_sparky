"use client";

import { useCallback, useRef, useState } from "react";
import { GoogleIcon } from "@/components/icons/brand-icons";
import type { useAuthSecurityConfig } from "@/components/auth/use-auth-security-config";
import { useI18n } from "@/components/i18n-provider";
import { googleCompletionPath } from "@/lib/auth-navigation";
import { platformApiRequest } from "@/lib/platform-api";

type GoogleAuthStartResponse = {
  authorization_url: string;
  expires_at: string;
};

export function GoogleAuthAction({
  label,
  returnTo,
  security
}: {
  label: string;
  returnTo: string;
  security: ReturnType<typeof useAuthSecurityConfig>;
}) {
  const { t } = useI18n();
  const [isStarting, setIsStarting] = useState(false);
  const [errorMessage, setErrorMessage] = useState("");
  const isStartingRef = useRef(false);
  const securityReady = security.status === "ready" || security.status === "fallback";

  const startGoogleAuth = useCallback(async () => {
    if (isStartingRef.current) {
      return;
    }
    isStartingRef.current = true;
    setIsStarting(true);
    setErrorMessage("");
    try {
      const response = await platformApiRequest<GoogleAuthStartResponse>("/auth/google/login/start", {
        method: "POST",
        csrfPolicy: "origin-only",
        body: JSON.stringify({ return_to: googleCompletionPath(returnTo) })
      });
      window.location.assign(response.authorization_url);
    } catch {
      isStartingRef.current = false;
      setIsStarting(false);
      setErrorMessage(t("auth.googleStartFailed"));
    }
  }, [returnTo, t]);

  return (
    <div className="google-auth-action">
      <button
        className="secondary-button google-auth-button"
        disabled={!securityReady || isStarting}
        onClick={() => void startGoogleAuth()}
        type="button"
      >
        <GoogleIcon aria-hidden="true" size={19} />
        {isStarting ? t("auth.googleStarting") : label}
      </button>
      {errorMessage ? <div className="auth-error" role="alert">{errorMessage}</div> : null}
    </div>
  );
}
