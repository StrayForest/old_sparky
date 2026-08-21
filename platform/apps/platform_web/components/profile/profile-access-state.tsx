"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useTransition } from "react";
import { useAuth } from "@/components/auth/auth-provider";
import { useI18n } from "@/components/i18n-provider";

export function ProfileAccessState({
  state
}: {
  state: "anonymous" | "unavailable";
}) {
  const { clearUser, refreshUser, status } = useAuth();
  const { t } = useI18n();
  const router = useRouter();
  const [isRetrying, startRetryTransition] = useTransition();

  useEffect(() => {
    if (state === "anonymous") {
      clearUser();
    }
  }, [clearUser, state]);

  if (state === "anonymous") {
    return (
      <section className="panel panel-pad auth-panel">
        <h2 className="panel-title">{t("profile.signInRequiredTitle")}</h2>
        <p className="description-text">{t("profile.signInRequiredCopy")}</p>
        <div className="auth-actions">
          <Link className="primary-button" href="/auth/login?returnTo=%2Fprofile%2Fme" prefetch={false}>
            {t("auth.login")}
          </Link>
          <Link className="secondary-button" href="/auth/register?returnTo=%2Fprofile%2Fme" prefetch={false}>
            {t("auth.createAccount")}
          </Link>
        </div>
      </section>
    );
  }

  return (
    <section className="panel panel-pad auth-panel">
      <h2 className="panel-title">{t("profile.unavailableTitle")}</h2>
      <p className="description-text">{t("profile.unavailableCopy")}</p>
      <div className="auth-actions">
        <button
          className="primary-button"
          disabled={isRetrying}
          onClick={() => {
            startRetryTransition(async () => {
              if (status === "unavailable") {
                await refreshUser().catch(() => undefined);
              }
              router.refresh();
            });
          }}
          type="button"
        >
          {isRetrying ? t("profile.retrying") : t("common.retry")}
        </button>
      </div>
    </section>
  );
}
