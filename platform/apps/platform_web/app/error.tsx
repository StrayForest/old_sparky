"use client";

import { useI18n } from "@/components/i18n-provider";

type AppErrorProps = {
  error: Error & { digest?: string };
  reset: () => void;
};

export default function AppError({ reset }: AppErrorProps) {
  const { t } = useI18n();

  return (
    <main className="main">
      <section className="panel panel-pad auth-panel" role="alert">
        <h1 className="panel-title">{t("common.unexpectedErrorTitle")}</h1>
        <p className="description-text">{t("common.unexpectedErrorCopy")}</p>
        <div className="auth-actions">
          <button className="primary-button" onClick={reset} type="button">
            {t("common.tryAgain")}
          </button>
        </div>
      </section>
    </main>
  );
}
