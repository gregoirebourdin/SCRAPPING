import type { NextConfig } from "next";

// `NEXT_DIST_DIR` lets a second dev server run next to `pnpm dev` (the browser E2E stack uses `.next-e2e`): two
// `next dev` cannot share a dist dir (`<distDir>/dev/lock`). That server reads an extending tsconfig, so Next never
// rewrites tsconfig.json with the other dist dir's type globs.
const distDir = process.env.NEXT_DIST_DIR || ".next";

const nextConfig: NextConfig = {
  distDir,
  ...(distDir === ".next" ? {} : { typescript: { tsconfigPath: "tsconfig.e2e.json" } }),
  reactStrictMode: true,
  transpilePackages: ["@scout/design-system", "@scout/schemas", "@scout/shared"],
  serverExternalPackages: ["pg"],
  poweredByHeader: false,
  async headers() {
    return [
      {
        source: "/:path*",
        headers: [
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
          { key: "X-Frame-Options", value: "DENY" },
          { key: "Permissions-Policy", value: "camera=(), microphone=(), geolocation=()" },
        ],
      },
    ];
  },
};

export default nextConfig;
