import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// In dev (`npm run dev`) /api is proxied to the API on :8000, mirroring nginx in Docker.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 3000,
    proxy: {
      "/api": {
        target: process.env.API_URL || "http://localhost:8000",
        rewrite: (path) => path.replace(/^\/api/, ""),
      },
    },
  },
});
