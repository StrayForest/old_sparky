import type { Metadata } from "next";
import { HomePageContent } from "@/components/home/home-page-content";

export const metadata: Metadata = {
  title: "Главная",
  description: "Турниры, обновления Deadlock, Драфты и новые видео Old Sparky."
};

export default function HomePage() {
  return <HomePageContent />;
}
