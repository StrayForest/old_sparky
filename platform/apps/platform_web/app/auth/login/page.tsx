import type { Metadata } from "next";
import { AuthForm } from "@/components/auth/auth-form";
import { Hero } from "@/components/layout/hero";

export const metadata: Metadata = {
  title: "Вход"
};

export default async function LoginPage({
  searchParams
}: {
  searchParams: Promise<{ returnTo?: string; steam_auth?: string; google_auth?: string }>;
}) {
  const { returnTo, steam_auth: steamAuth, google_auth: googleAuth } = await searchParams;
  return (
    <>
      <div className="page-noise" aria-hidden="true" />
      <Hero
        title="Вход"
        subtitle="Профиль, регистрации и управление турнирами."
      />
      <AuthForm
        googleAuthError={googleAuth === "error"}
        mode="login"
        returnTo={returnTo}
        steamAuthError={steamAuth === "error"}
      />
    </>
  );
}
