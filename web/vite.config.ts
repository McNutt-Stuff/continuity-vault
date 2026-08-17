import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The dev server proxies /api to the cloud control plane so the SPA and API
// share an origin (matches the production Caddy reverse-proxy layout).
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: process.env.CV_API_TARGET || "http://localhost:8000",
        changeOrigin: true,
      },
    },
  },
  build: { outDir: "dist" },
});
