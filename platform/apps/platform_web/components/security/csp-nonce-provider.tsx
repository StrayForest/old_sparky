"use client";

import { createContext, useContext, useState, type ReactNode } from "react";

const CspNonceContext = createContext<string | null>(null);

export function CspNonceProvider({
  children,
  nonce,
}: {
  children: ReactNode;
  nonce: string | null;
}) {
  // Root layouts survive App Router soft navigation. Retaining the initial
  // value ensures client-rendered styles always use this document's nonce.
  const [documentNonce] = useState(nonce);
  return (
    <CspNonceContext.Provider value={documentNonce}>
      {children}
    </CspNonceContext.Provider>
  );
}

export function useCspNonce(): string | null {
  return useContext(CspNonceContext);
}
