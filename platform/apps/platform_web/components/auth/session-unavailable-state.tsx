"use client";

import { useRouter } from "next/navigation";
import { useAuth } from "@/components/auth/auth-provider";
import { useI18n } from "@/components/i18n-provider";

export function SessionUnavailableState() {
  const { refreshUser } = useAuth();
  const { t } = useI18n();
  const router = useRouter();

  return (
    <section className="panel panel-pad auth-panel">
      <h2 className="panel-title">{t("auth.sessionUnavailableTitle")}</h2>
      <p className="description-text">{t("auth.sessionUnavailableCopy")}</p>
      <div className="auth-actions">
        <button
          className="primary-button"
          onClick={() => {
            void refreshUser().catch(() => undefined).finally(() => router.refresh());
          }}
          type="button"
        >
          {t("common.tryAgain")}
        </button>
      </div>
    </section>
  );
}
