import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // No X-Powered-By header; the API hides its server header the same way.
  poweredByHeader: false,
  // Don't let `next dev` write an AGENTS.md here. The root AGENTS.md already points
  // agents to the docs bundled with this Next.js version.
  agentRules: false,
  turbopack: {
    rules: {
      // Tailwind CSS v4's native Turbopack loader, so no PostCSS config is needed.
      "*.css": { loaders: ["@tailwindcss/turbopack"], as: "*.css" },
    },
  },
};

export default nextConfig;
