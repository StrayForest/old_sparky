import type { MetadataRoute } from "next";

const SITE_ORIGIN = "https://old-sparky.com";

export default function sitemap(): MetadataRoute.Sitemap {
  const generatedAt = new Date();
  return [
    { url: `${SITE_ORIGIN}/`, lastModified: generatedAt, changeFrequency: "daily", priority: 1 },
    { url: `${SITE_ORIGIN}/tournaments`, lastModified: generatedAt, changeFrequency: "hourly", priority: 0.9 },
    { url: `${SITE_ORIGIN}/info`, lastModified: generatedAt, changeFrequency: "monthly", priority: 0.6 },
    { url: `${SITE_ORIGIN}/privacy`, lastModified: generatedAt, changeFrequency: "yearly", priority: 0.3 },
    { url: `${SITE_ORIGIN}/terms`, lastModified: generatedAt, changeFrequency: "yearly", priority: 0.3 },
    { url: `${SITE_ORIGIN}/stats`, lastModified: generatedAt, changeFrequency: "daily", priority: 0.5 }
  ];
}
