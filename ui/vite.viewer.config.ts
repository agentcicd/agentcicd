import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  // The loopback server serves one SPA at the origin root. Run/project URLs are
  // client routes, so relative asset URLs would incorrectly resolve below them.
  base: "/",
  build: {
    outDir: "../src/agentcicd/ui_static",
    emptyOutDir: true,
  },
});
