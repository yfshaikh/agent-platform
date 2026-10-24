import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// In development, /api goes to the platform API through `kubectl port-forward svc/platform-api 8080:80 -n platform`.
export default defineConfig({
  plugins: [react()],
  server: { port: 5173, proxy: { "/api": "http://localhost:8080" } },
});
