"use client";

import {
  createContext,
  ReactNode,
  useContext,
} from "react";

import {
  enumLabel,
  translate,
} from "../lib/i18n";

type I18nContextValue = {
  enumLabel: (value: string | null | undefined) => string;
  formatDate: (value: string) => string;
  t: (key: string, params?: Record<string, string | number | null | undefined>) => string;
};

const I18nContext = createContext<I18nContextValue | null>(null);
const i18nValue: I18nContextValue = {
  enumLabel,
  formatDate: (rawValue) => new Intl.DateTimeFormat("ru-RU", {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(rawValue)),
  t: translate,
};

export function I18nProvider({ children }: { children: ReactNode }) {
  return <I18nContext.Provider value={i18nValue}>{children}</I18nContext.Provider>;
}

export function useI18n(): I18nContextValue {
  const context = useContext(I18nContext);
  if (!context) {
    throw new Error("useI18n must be used inside I18nProvider");
  }
  return context;
}
