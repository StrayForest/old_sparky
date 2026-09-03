"use client";

import Script from "next/script";
import { useEffect, useRef, useState } from "react";
import { ShieldCheck } from "lucide-react";
import { useI18n } from "@/components/i18n-provider";

export type TurnstileAction =
  | "login"
  | "register"
  | "reset_request"
  | "verification_resend";
type TurnstileState = "loading" | "checking" | "verified" | "expired" | "error";

type TurnstileApi = {
  render: (
    container: HTMLElement,
    options: {
      sitekey: string;
      action: TurnstileAction;
      appearance: "interaction-only";
      language: "ru";
      size: "flexible";
      theme: "dark";
      callback: (token: string) => void;
      "error-callback": () => void;
      "expired-callback": () => void;
      "timeout-callback": () => void;
    }
  ) => string;
  remove: (widgetId: string) => void;
  reset: (widgetId: string) => void;
};

declare global {
  interface Window {
    turnstile?: TurnstileApi;
  }
}

type TurnstileWidgetProps = {
  action: TurnstileAction;
  onTokenChange: (token: string | null) => void;
  resetSignal: number;
  siteKey: string;
};

export function TurnstileWidget({
  action,
  onTokenChange,
  resetSignal,
  siteKey
}: TurnstileWidgetProps) {
  const { t } = useI18n();
  const containerRef = useRef<HTMLDivElement>(null);
  const widgetIdRef = useRef<string | null>(null);
  const previousResetSignalRef = useRef(resetSignal);
  const [scriptReady, setScriptReady] = useState(false);
  const [state, setState] = useState<TurnstileState>("loading");

  useEffect(() => {
    if (!scriptReady || !window.turnstile || !containerRef.current) {
      return;
    }

    const turnstile = window.turnstile;
    let active = true;
    setState("checking");
    try {
      const widgetId = turnstile.render(containerRef.current, {
        sitekey: siteKey,
        action,
        appearance: "interaction-only",
        language: "ru",
        size: "flexible",
        theme: "dark",
        callback: (token) => {
          if (!active) {
            return;
          }
          onTokenChange(token);
          setState("verified");
        },
        "error-callback": () => {
          if (!active) {
            return;
          }
          onTokenChange(null);
          setState("error");
        },
        "expired-callback": () => {
          if (!active) {
            return;
          }
          onTokenChange(null);
          setState("expired");
        },
        "timeout-callback": () => {
          if (!active) {
            return;
          }
          onTokenChange(null);
          setState("expired");
        }
      });
      widgetIdRef.current = widgetId;
      return () => {
        active = false;
        turnstile.remove(widgetId);
        if (widgetIdRef.current === widgetId) {
          widgetIdRef.current = null;
        }
      };
    } catch {
      onTokenChange(null);
      setState("error");
    }
  }, [action, onTokenChange, scriptReady, siteKey]);

  useEffect(() => {
    if (previousResetSignalRef.current === resetSignal) {
      return;
    }
    previousResetSignalRef.current = resetSignal;
    resetWidget();
  }, [resetSignal]);

  function resetWidget() {
    onTokenChange(null);
    const widgetId = widgetIdRef.current;
    if (widgetId && window.turnstile) {
      window.turnstile.reset(widgetId);
      setState("checking");
      return;
    }
    setState(scriptReady ? "error" : "loading");
  }

  const statusKey = state === "expired" ? "auth.turnstileExpired" : "auth.turnstileError";

  return (
    <div
      aria-busy={state === "loading" || state === "checking"}
      aria-label={t("auth.turnstileLabel")}
      className="auth-turnstile"
      data-state={state}
    >
      <Script
        id="cloudflare-turnstile"
        onError={() => {
          onTokenChange(null);
          setState("error");
        }}
        onReady={() => setScriptReady(true)}
        src="https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit"
        strategy="afterInteractive"
      />
      {state === "expired" || state === "error" ? (
        <div className="auth-turnstile-heading" aria-hidden="true">
          <span><ShieldCheck size={18} /></span>
          <strong>{t("auth.turnstileTitle")}</strong>
        </div>
      ) : null}
      <div className="auth-turnstile-frame" ref={containerRef} />
      {state === "expired" || state === "error" ? (
        <div aria-live="polite" className="auth-turnstile-feedback" data-state={state} role="status">
          <span>{t(statusKey)}</span>
          <button className="auth-turnstile-retry" onClick={resetWidget} type="button">
            {t("auth.turnstileRetry")}
          </button>
        </div>
      ) : null}
    </div>
  );
}
