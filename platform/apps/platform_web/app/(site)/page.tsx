import type { Metadata } from "next";
import { HomePageContent } from "@/components/home/home-page-content";
import { getServerHomeContent } from "@/lib/server-home-content";

export const metadata: Metadata = {
  title: "Главная",
  description: "Турниры сообщества, обновления Deadlock и новые видео Old Sparky."
};

export default async function HomePage() {
  const initialContent = await getServerHomeContent();
  return <HomePageContent initialContent={initialContent} />;
}
