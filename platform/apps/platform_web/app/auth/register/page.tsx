import type { Metadata } from "next";
import { AuthForm } from "@/components/auth/auth-form";
import { Hero } from "@/components/layout/hero";

export const metadata: Metadata = {
  title: "Создать аккаунт"
};

export default async function RegisterPage({
  searchParams
}: {
  searchParams: Promise<{ returnTo?: string }>;
}) {
  const { returnTo } = await searchParams;
  return (
    <>
      <div className="page-noise" aria-hidden="true" />
      <Hero title="Создать аккаунт" subtitle="" />
      <span hidden aria-hidden="true">Зарегистрируйтесь в web-платформе</span>
      <AuthForm mode="register" returnTo={returnTo} />
    </>
  );
}