import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// reader-api (services/reader-api) has no CORS middleware, so the dev server proxies
// /api -> it instead. This is the only backend the browser talks to directly; askai is
// bearer-token gated and only reachable server-side, via reader-api's own /ask proxy.
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": {
        target: "http://localhost:8081",
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ""),
      },
    },
  },
});
