import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  base: "/admin/",
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/admin/api": {
        target: "http://127.0.0.1:18792",
        changeOrigin: true,
      },
      "/healthz": {
        target: "http://127.0.0.1:18792",
        changeOrigin: true,
      },
      "/v1": {
        target: "http://127.0.0.1:18792",
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: "dist",
    assetsDir: "assets",
    sourcemap: false,
  },
});
