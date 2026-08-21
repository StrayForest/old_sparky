import type { Metadata } from "next";
import { PatchDetailPage } from "@/components/home/patch-detail-page";

export const metadata: Metadata = {
  title: "Патч Deadlock",
  description: "Изменения последнего патча Deadlock по героям и способностям."
};

export default async function PatchPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return <PatchDetailPage patchId={id} />;
}
