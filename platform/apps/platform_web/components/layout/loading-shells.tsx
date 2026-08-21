import { Hero } from "@/components/layout/hero";

type LoadingShellVariant =
  | "tournaments"
  | "tournament-detail"
  | "bracket"
  | "organizer"
  | "dashboard"
  | "operations";

const loadingShellCopy: Record<LoadingShellVariant, { title: string; subtitle: string; eyebrow?: string }> = {
  tournaments: {
    title: "Deadlock-турниры",
    subtitle: "Загружаем список турниров."
  },
  "tournament-detail": {
    eyebrow: "Турниры",
    title: "Загрузка турнира",
    subtitle: "Подгружаем параметры, регистрацию и текущий этап."
  },
  bracket: {
    title: "Загрузка сетки",
    subtitle: "Подгружаем актуальные матчи и результаты."
  },
  organizer: {
    eyebrow: "Турниры / Создать турнир",
    title: "Создать турнир",
    subtitle: "Готовим форму организатора."
  },
  dashboard: {
    title: "Профиль",
    subtitle: "Загружаем ваш турнирный профиль."
  },
  operations: {
    title: "Operations",
    subtitle: "Проверяем доступ и загружаем панель управления."
  }
};

export function RouteLoadingShell({ variant }: { variant: LoadingShellVariant }) {
  const copy = loadingShellCopy[variant];
  return (
    <>
      <div className="page-noise" aria-hidden="true" />
      <Hero eyebrow={copy.eyebrow} title={copy.title} subtitle={copy.subtitle} />
      <main className="main">
        <section className={`panel panel-pad route-loading-shell route-loading-${variant}`} aria-busy="true">
          <div className="route-loading-head">
            <span className="loading-dot" aria-hidden="true" />
            <div>
              <h2 className="panel-title">Загрузка</h2>
              <p>Подождите, страница загружается</p>
            </div>
          </div>
          <div className="route-loading-grid" aria-hidden="true">
            <span className="skeleton-line skeleton-line-wide" />
            <span className="skeleton-line" />
            <span className="skeleton-line skeleton-line-short" />
            <span className="skeleton-card" />
            <span className="skeleton-card" />
            <span className="skeleton-card skeleton-card-wide" />
          </div>
        </section>
      </main>
    </>
  );
}
