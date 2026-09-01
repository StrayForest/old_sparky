"use client";

import { useCallback, useEffect, useState } from "react";
import { platformApiRequest } from "@/lib/platform-api";
import type { PlatformAuthSecurityConfig } from "@/lib/platform-types";

export type AuthSecurityConfigState = {
  config: PlatformAuthSecurityConfig | null;
  status: "loading" | "ready" | "fallback" | "error";
};

const fallbackTurnstileSiteKey = process.env.NEXT_PUBLIC_PLATFORM_TURNSTILE_SITE_KEY?.trim() || null;
const securityConfigCacheTtlMs = 5 * 60 * 1000;

let cachedSecurityConfig: {
  config: PlatformAuthSecurityConfig;
  expiresAt: number;
} | null = null;
let securityConfigRequest: Promise<PlatformAuthSecurityConfig> | null = null;

function readCachedSecurityConfig(): PlatformAuthSecurityConfig | null {
  if (!cachedSecurityConfig) {
    return null;
  }
  if (cachedSecurityConfig.expiresAt <= Date.now()) {
    cachedSecurityConfig = null;
    return null;
  }
  return cachedSecurityConfig.config;
}

function loadSecurityConfig(force = false): Promise<PlatformAuthSecurityConfig> {
  if (force) {
    cachedSecurityConfig = null;
  }
  const cached = readCachedSecurityConfig();
  if (cached) {
    return Promise.resolve(cached);
  }
  if (securityConfigRequest) {
    return securityConfigRequest;
  }

  const request = platformApiRequest<PlatformAuthSecurityConfig>("/auth/security-config")
    .then((payload) => {
      const config = validateSecurityConfig(payload);
      cachedSecurityConfig = {
        config,
        expiresAt: Date.now() + securityConfigCacheTtlMs,
      };
      return config;
    });
  securityConfigRequest = request;
  request.then(
    () => {
      if (securityConfigRequest === request) {
        securityConfigRequest = null;
      }
    },
    () => {
      if (securityConfigRequest === request) {
        securityConfigRequest = null;
      }
    }
  );
  return request;
}

export function useAuthSecurityConfig(enabled = true) {
  const [attempt, setAttempt] = useState(0);
  const [state, setState] = useState<AuthSecurityConfigState>({
    config: fallbackSecurityConfig(),
    status: fallbackTurnstileSiteKey ? "fallback" : "loading"
  });

  useEffect(() => {
    if (!enabled) {
      return;
    }
    let active = true;
    const fallback = fallbackSecurityConfig();
    const cached = readCachedSecurityConfig();
    setState({
      config: cached ?? fallback,
      status: cached ? "ready" : fallback ? "fallback" : "loading"
    });

    void loadSecurityConfig(attempt > 0)
      .then((payload) => {
        if (active) {
          setState({ config: validateSecurityConfig(payload), status: "ready" });
        }
      })
      .catch(() => {
        if (active) {
          setState({ config: fallback, status: fallback ? "fallback" : "error" });
        }
      });

    return () => {
      active = false;
    };
  }, [attempt, enabled]);

  const retry = useCallback(() => setAttempt((current) => current + 1), []);
  return { ...state, retry };
}

function fallbackSecurityConfig(): PlatformAuthSecurityConfig | null {
  if (!fallbackTurnstileSiteKey) {
    return null;
  }
  return {
    // A build-time Turnstile key can keep password login usable while the
    // runtime config endpoint is unavailable, but it is not authority to open
    // registration or relax verification policy. Those capabilities fail
    // closed until the backend contract is available again.
    public_registration_enabled: false,
    email_verification_required: true,
    turnstile_mode: "always",
    turnstile_site_key: fallbackTurnstileSiteKey,
    steam_login_enabled: false
  };
}

function validateSecurityConfig(payload: PlatformAuthSecurityConfig): PlatformAuthSecurityConfig {
  if (!["off", "always", "adaptive"].includes(payload.turnstile_mode)) {
    throw new Error("Unsupported Turnstile mode.");
  }
  const siteKey = payload.turnstile_site_key?.trim() || null;
  if (payload.turnstile_mode !== "off" && !siteKey) {
    throw new Error("Turnstile site key is missing.");
  }
  if (
    typeof payload.public_registration_enabled !== "boolean"
    || typeof payload.email_verification_required !== "boolean"
  ) {
    throw new Error("Authentication security state is incomplete.");
  }
  return {
    public_registration_enabled: payload.public_registration_enabled,
    email_verification_required: payload.email_verification_required,
    turnstile_mode: payload.turnstile_mode,
    turnstile_site_key: siteKey,
    steam_login_enabled: payload.steam_login_enabled === true
  };
}
