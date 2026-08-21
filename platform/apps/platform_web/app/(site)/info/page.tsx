import type { Metadata } from "next";
import { InfoPageContent } from "@/components/info/info-page-content";

export const metadata: Metadata = {
  title: "Инфо",
  description: "Инструкции, правила, ответы на вопросы и поддержка Old Sparky Arena."
};

export default function InfoPage() {
  return <InfoPageContent />;
}
