import path from "node:path";

import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Build a self-contained server (server.js plus only the files it needs) for the
  // container image. Trace from the repository root, where pnpm keeps the packages:
  // the server then lands in .next/standalone/apps/web/server.js.
  output: "standalone",
  outputFileTracingRoot: path.join(import.meta.dirname, "../.."),
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
