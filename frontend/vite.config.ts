import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The API the dev and preview servers proxy to. tools/ci.py (stage ui-smoke) points it at a
// throwaway backend on a free port.
const apiTarget = process.env.JOBFINDER_API_PROXY ?? "http://localhost:8000";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: { "/api": apiTarget },
  },
  preview: {
    proxy: { "/api": apiTarget },
  },
});
