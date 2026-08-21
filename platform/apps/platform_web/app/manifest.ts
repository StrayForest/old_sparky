import type { MetadataRoute } from "next";

export default function manifest(): MetadataRoute.Manifest {
  return {
    name: "Old Sparky Arena",
    short_name: "Old Sparky",
    description: "Турниры и игровое сообщество Deadlock.",
    start_url: "/",
    display: "standalone",
    background_color: "#071014",
    theme_color: "#d99031",
    lang: "ru",
    icons: [
      { src: "/icon.png", sizes: "512x512", type: "image/png" },
      { src: "/apple-icon.png", sizes: "180x180", type: "image/png" }
    ]
  };
}
