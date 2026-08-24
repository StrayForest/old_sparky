"use client";

import type { MouseEvent, ReactNode } from "react";
import { useRouter } from "next/navigation";

type HistoryBackLinkProps = {
  children: ReactNode;
  className?: string;
  fallbackHref: string;
};

export function HistoryBackLink({ children, className, fallbackHref }: HistoryBackLinkProps) {
  const router = useRouter();

  function handleClick(event: MouseEvent<HTMLAnchorElement>) {
    if (
      event.defaultPrevented
      || event.button !== 0
      || event.metaKey
      || event.ctrlKey
      || event.shiftKey
      || event.altKey
    ) {
      return;
    }

    event.preventDefault();
    if (window.history.length > 1) {
      router.back();
      return;
    }
    router.replace(fallbackHref);
  }

  return (
    <a className={className} href={fallbackHref} onClick={handleClick}>
      {children}
    </a>
  );
}
