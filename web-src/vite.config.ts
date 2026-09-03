import react from "@vitejs/plugin-react-swc";
import { defineConfig } from "vite";

// Builds straight into ../web — that's what app/main.py's StaticFiles/SPA
// fallback serves. No Node/npm on the robot (checked) — only the built
// output ever gets deployed there, same as humanoid-robot's own dashboard.
export default defineConfig({
  plugins: [react()],
  build: {
    outDir: "../web",
    emptyOutDir: true,
  },
  server: {
    port: 5173,
    proxy: {
      "/api": { target: "http://127.0.0.1:8000", changeOrigin: true },
      "/ws": { target: "http://127.0.0.1:8000", ws: true },
    },
  },
});
