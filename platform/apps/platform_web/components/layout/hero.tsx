import type { ReactNode } from "react";

type HeroProps = {
  title: string;
  subtitle?: string;
  eyebrow?: string;
  variant?: "default" | "home";
  actions?: ReactNode;
};

export function Hero({ actions, title, subtitle, eyebrow, variant = "default" }: HeroProps) {
  const titleWords = title.trim().split(/\s+/u);
  const homeAccent = variant === "home" && titleWords.length > 1
    ? titleWords.pop()
    : null;

  return (
    <section className={variant === "home" ? "hero-wrap hero-wrap-home" : "hero-wrap"}>
      <div className="hero-inner">
        {eyebrow ? <div className="breadcrumbs">{eyebrow}</div> : null}
        <h1 className="hero-title" aria-label={title}>
          {homeAccent ? (
            <>
              <span aria-hidden="true">{titleWords.join(" ")}</span>
              <span aria-hidden="true" className="hero-title-accent">{homeAccent}</span>
            </>
          ) : title}
        </h1>
        {subtitle ? <p className="hero-subtitle">{subtitle}</p> : null}
        {actions ? <div className="hero-actions">{actions}</div> : null}
      </div>
    </section>
  );
}
