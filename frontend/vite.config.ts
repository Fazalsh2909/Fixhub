import react from "@vitejs/plugin-react";
import { defineConfig, loadEnv } from "vite";

export default defineConfig(({ mode }) => {
  // Keep the default backend port for normal local dev; allow a one-off
  // override (VITE_API_TARGET) when another checkout already owns port 8001.
  const apiTarget = loadEnv(mode, ".", "").VITE_API_TARGET || "http://localhost:8001";

  return {
    plugins: [react()],
    server: {
      port: 5173,
      proxy: {
        "/api": apiTarget,
        "/webhooks": apiTarget,
        "/health": apiTarget,
      },
    },
    build: { outDir: "dist", sourcemap: false },
  };
});
