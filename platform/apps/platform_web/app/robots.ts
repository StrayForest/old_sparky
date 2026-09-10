import type { MetadataRoute } from "next";

const SITE_ORIGIN = "https://old-sparky.com";

export default function robots(): MetadataRoute.Robots {
  return {
    rules: {
      userAgent: "*",
      allow: "/"
    },
    sitemap: `${SITE_ORIGIN}/sitemap.xml`
  };
}
