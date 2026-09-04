"use client";

import { useEffect, useRef } from "react";

declare global {
  interface Window {
    adsbygoogle?: unknown[];
  }
}

export function DiagnosticAd() {
  const hasPushedRef = useRef(false);

  useEffect(() => {
    if (hasPushedRef.current) {
      return;
    }
    hasPushedRef.current = true;
    (window.adsbygoogle = window.adsbygoogle || []).push({});
  }, []);

  return (
    <ins
      className="adsbygoogle diagnostic-ad"
      data-ad-client="ca-pub-7185165276065459"
      data-ad-format="auto"
      data-ad-slot="4365553701"
      data-full-width-responsive="true"
    />
  );
}
