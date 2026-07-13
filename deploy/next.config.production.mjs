const buildId = process.env.NEXT_BUILD_ID;

if (!buildId) {
  throw new Error("NEXT_BUILD_ID is required for a reproducible build");
}

const nextConfig = {
  output: "standalone",
  serverExternalPackages: ["better-sqlite3"],
  eslint: {
    ignoreDuringBuilds: true,
  },
  generateBuildId: async () => buildId,
};

export default nextConfig;

