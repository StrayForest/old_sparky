import Link from "next/link";
import { SiteHeader } from "@/components/layout/site-header";

export default function NotFound() {
  return (
    <>
      <div aria-hidden="true" className="page-noise" />
      <SiteHeader />
      <main className="not-found-main">
        <section aria-labelledby="not-found-title" className="panel not-found-panel">
          <h1 className="not-found-code" id="not-found-title">404</h1>
          <h2>Страница не найдена</h2>
          <p>
            Такой страницы нет или ссылка устарела. Вернитесь на главную или
            откройте список турниров.
          </p>
          <nav aria-label="Переход со страницы 404" className="not-found-actions">
            <Link className="primary-action" href="/">На главную</Link>
            <Link className="outline-button" href="/tournaments">К турнирам</Link>
          </nav>
        </section>
      </main>
    </>
  );
}
