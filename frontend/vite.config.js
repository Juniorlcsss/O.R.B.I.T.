import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import cesium from "vite-plugin-cesium";

export default defineConfig({
  plugins: [react(), cesium()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: process.env.ORBIT_BACKEND_URL || "http://localhost:8080",
        changeOrigin: true,
      },
    },
  },
  build: {
    chunkSizeWarningLimit: 4000,
  },
});
