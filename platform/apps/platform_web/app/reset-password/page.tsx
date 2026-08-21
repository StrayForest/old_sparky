import type { Metadata } from "next";
import { PasswordResetForm } from "@/components/auth/auth-lifecycle-forms";
import { Hero } from "@/components/layout/hero";
import { translate } from "@/lib/i18n";

export const metadata: Metadata = {
  title: translate("auth.resetPageTitle"),
  referrer: "no-referrer",
  robots: { index: false, follow: false }
};

export default async function ResetPasswordPage({
  searchParams
}: {
  searchParams: Promise<{ returnTo?: string }>;
}) {
  const { returnTo } = await searchParams;
  return (
    <>
      <div className="page-noise" aria-hidden="true" />
      <Hero
        title={translate("auth.resetPageTitle")}
        subtitle={translate("auth.resetPageSubtitle")}
      />
      <main className="main auth-layout">
        <section className="panel panel-pad auth-panel" aria-label={translate("auth.resetPageTitle")}>
          <PasswordResetForm returnTo={returnTo} />
        </section>
      </main>
    </>
  );
}
