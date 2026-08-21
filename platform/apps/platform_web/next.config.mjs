const apiProxyBaseUrl = (process.env.PLATFORM_API_BASE_URL ?? "http://127.0.0.1:8010/api/v1").replace(/\/$/, "");

/** @type {import('next').NextConfig} */
const nextConfig = {
  output: "standalone",
  poweredByHeader: false,
  reactStrictMode: true,
  allowedDevOrigins: ["127.0.0.1", "localhost"],
  async headers() {
    return [
      {
        source: "/assets/ranks/:rank.webp",
        headers: [
          {
            key: "Cache-Control",
            value: "public, max-age=31536000, immutable"
          }
        ]
      }
    ];
  },
  async rewrites() {
    return [
      {
        source: "/api/v1/:path*",
        destination: `${apiProxyBaseUrl}/:path*`
      }
    ];
  }
};

export default nextConfig;
