import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The public marketing site (Public Web Node). Built to /dist and served
// statically by Caddy, same as the customer portal.
export default defineConfig({
  plugins: [react()],
  server: { port: 5174 },
  build: { outDir: "dist", sourcemap: false },
});
