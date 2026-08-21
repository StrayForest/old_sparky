import type { Metadata } from "next";
import Link from "next/link";
import {
  AndroidAutofillDiagnostic,
  type AndroidAutofillDiagnosticMode,
} from "@/components/auth/android-autofill-diagnostic";
import { Hero } from "@/components/layout/hero";

export const metadata: Metadata = {
  title: "Android Autofill Test",
  referrer: "no-referrer",
  robots: { index: false, follow: false },
};

export default async function AndroidAutofillTestPage({
  searchParams,
}: {
  searchParams: Promise<{ mode?: string }>;
}) {
  const params = await searchParams;
  const mode: AndroidAutofillDiagnosticMode =
    params.mode === "change" ? "change" : "signup";

  return (
    <>
      <div className="page-noise" aria-hidden="true" />
      <Hero
        title="Android Autofill Test"
        subtitle="Проверка реального Google Password Manager / Android Autofill без API, Turnstile и React-controlled password inputs."
      />
      <main className="main auth-layout">
        <section
          aria-label="Android Autofill Test"
          className="panel panel-pad auth-panel"
        >
          <div className="auth-actions">
            <Link
              aria-current={mode === "signup" ? "page" : undefined}
              className={mode === "signup" ? "primary-button" : "secondary-button"}
              href="/android-autofill-test?mode=signup"
            >
              Создание пароля
            </Link>
            <Link
              aria-current={mode === "change" ? "page" : undefined}
              className={mode === "change" ? "primary-button" : "secondary-button"}
              href="/android-autofill-test?mode=change"
            >
              Смена пароля
            </Link>
          </div>
          <AndroidAutofillDiagnostic mode={mode} />
        </section>
      </main>
    </>
  );
}
