import type { Metadata } from "next";
import { cookies } from "next/headers";
import Link from "next/link";
import { Hero } from "@/components/layout/hero";
import { CreateTournamentForm } from "@/components/tournaments/create-tournament-form";
import { getServerCurrentUser, platformSessionCookieName } from "@/lib/server-auth";

export const metadata: Metadata = {
  title: "Создать турнир",
  description: "Создание турнира Deadlock в Old Sparky Arena."
};

export default async function NewTournamentPage() {
  const requestCookies = await cookies();
  const cookieHeader = requestCookies.toString();
  const authSnapshot = requestCookies.has(platformSessionCookieName())
    ? await getServerCurrentUser(cookieHeader)
    : null;
  const currentUser = authSnapshot?.user ?? null;

  return (
    <>
      <div className="page-noise" aria-hidden="true" />
      <Hero
        eyebrow="Турниры / Создать турнир"
        title="Создать турнир"
        subtitle="Настройте турнир за несколько минут."
      />
      <main className="main">
        {currentUser ? (
          <CreateTournamentForm currentUser={currentUser} serverNowIso={new Date().toISOString()} />
        ) : (
          <div className="auth-layout">
            <section className="panel panel-pad auth-panel create-auth-panel">
              <h2 className="panel-title">Войдите или зарегистрируйтесь</h2>
              <p className="description-text">Создавать турниры могут только авторизованные пользователи.</p>
              <div className="auth-actions">
                <Link className="primary-button" href="/auth/login?returnTo=%2Ftournaments%2Fnew" prefetch={false}>Войти</Link>
                <Link className="secondary-button" href="/auth/register?returnTo=%2Ftournaments%2Fnew" prefetch={false}>Зарегистрироваться</Link>
              </div>
            </section>
          </div>
        )}
      </main>
    </>
  );
}
