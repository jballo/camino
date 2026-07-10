import type { NextConfig } from "next";
import path from "node:path";

const nextConfig: NextConfig = {
  // Pin the workspace root to this folder so Next.js/Turbopack doesn't infer it
  // from a stray lockfile in a parent directory.
  turbopack: {
    root: path.resolve(__dirname),
  },
};

export default nextConfig;
