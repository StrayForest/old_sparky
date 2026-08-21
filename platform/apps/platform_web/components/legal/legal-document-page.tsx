import { Hero } from "@/components/layout/hero";

export type LegalSection = {
  title: string;
  paragraphs: readonly string[];
  items?: readonly string[];
};

type LegalDocumentPageProps = {
  title: string;
  subtitle: string;
  sections: readonly LegalSection[];
};

export function LegalDocumentPage({ title, subtitle, sections }: LegalDocumentPageProps) {
  return (
    <>
      <div className="page-noise" aria-hidden="true" />
      <Hero title={title} subtitle={subtitle} />
      <main className="main legal-main">
        <article className="legal-document">
          <p className="legal-updated">Последнее обновление: 13 августа 2026 года</p>
          {sections.map((section) => (
            <section key={section.title}>
              <h2>{section.title}</h2>
              {section.paragraphs.map((paragraph) => <p key={paragraph}>{paragraph}</p>)}
              {section.items ? (
                <ul>
                  {section.items.map((item) => <li key={item}>{item}</li>)}
                </ul>
              ) : null}
            </section>
          ))}
          <p className="legal-contact">
            Вопросы по этим условиям и данным отправляйте через форму на странице{" "}
            <a href="/info#support">«Инфо и поддержка»</a>.
          </p>
        </article>
      </main>
    </>
  );
}
