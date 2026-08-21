import type { Metadata } from "next";
import { Activity, BarChart3, CheckCircle2, Swords, Trophy, Users } from "lucide-react";
import { Hero } from "@/components/layout/hero";
import { getPlatformStatsOverview } from "@/lib/platform-api";

export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: "Статистика"
};

const numberFormatter = new Intl.NumberFormat("ru-RU");

export default async function StatsPage() {
  const stats = await getPlatformStatsOverview();
  const maxRankCount = Math.max(1, ...stats.deadlock_rank_distribution.map((bucket) => bucket.count));
  const cards = [
    {
      icon: Trophy,
      label: "Турниры",
      value: stats.total_tournaments,
      meta: `${numberFormatter.format(stats.completed_tournaments)} завершено`
    },
    {
      icon: Activity,
      label: "Активные и будущие",
      value: stats.active_upcoming_tournaments,
      meta: "registration и in progress"
    },
    {
      icon: Users,
      label: "Активные регистрации",
      value: stats.registered_participants,
      meta: `${numberFormatter.format(stats.registered_participants_with_deadlock_profile)} с Deadlock-профилем`
    },
    {
      icon: Swords,
      label: "Завершённые матчи",
      value: stats.completed_matches,
      meta: "по завершенным сеткам"
    },
    {
      icon: CheckCircle2,
      label: "Покрытие профилями",
      value: `${stats.deadlock_profile_coverage_percent}%`,
      meta: `${numberFormatter.format(stats.deadlock_profiles_total)} Deadlock-профилей всего`
    },
    {
      icon: BarChart3,
      label: "Ранги в статистике",
      value: stats.deadlock_rank_distribution.length,
      meta: "live platformdb"
    }
  ];

  return (
    <>
      <div className="page-noise" aria-hidden="true" />
      <Hero
        title="Статистика платформы"
        subtitle="Публичные агрегаты по турнирам, регистрациям, матчам и заполненности Deadlock-профилей."
      />
      <main className="main stats-page">
        <section className="panel stats-toolbar">
          <div>
            <h2 className="panel-title">Phase 7 stats MVP</h2>
            <p className="section-copy">
              Данные считаются из `platformdb` и не обращаются к legacy Telegram bot runtime.
            </p>
          </div>
          <span className="status-chip status-chip-green">Live</span>
        </section>

        <section className="stats-card-grid" aria-label="Ключевые показатели">
          {cards.map((card) => {
            const Icon = card.icon;
            return (
              <article className="stats-card" key={card.label}>
                <span className="stats-card-icon"><Icon size={22} aria-hidden="true" /></span>
                <span className="stats-card-label">{card.label}</span>
                <strong className="stats-card-value">
                  {typeof card.value === "number" ? numberFormatter.format(card.value) : card.value}
                </strong>
                <span className="stats-card-meta">{card.meta}</span>
              </article>
            );
          })}
        </section>

        <section className="panel panel-pad stats-rank-panel">
          <div className="stats-section-header">
            <div>
              <h2 className="panel-title">Распределение рангов</h2>
              <p className="section-copy">Срез по сохранённым Deadlock-профилям игроков.</p>
            </div>
          </div>
          {stats.deadlock_rank_distribution.length ? (
            <div className="stats-rank-list">
              {stats.deadlock_rank_distribution.map((bucket) => (
                <div className="stats-rank-row" key={bucket.rank}>
                  <span className="stats-rank-name">{bucket.rank}</span>
                  <progress
                    aria-label={`Доля ранга ${bucket.rank}`}
                    className="stats-rank-meter"
                    max={100}
                    value={Math.max(4, (bucket.count / maxRankCount) * 100)}
                  />
                  <strong>{numberFormatter.format(bucket.count)}</strong>
                </div>
              ))}
            </div>
          ) : (
            <div className="empty-state">Ранги появятся после заполнения первых Deadlock-профилей.</div>
          )}
        </section>
      </main>
    </>
  );
}
