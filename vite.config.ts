import { fileURLToPath, URL } from "node:url";

import vue from "@vitejs/plugin-vue";
import { defineConfig } from "vite";

export default defineConfig({
  root: "frontend",
  plugins: [vue()],
  build: {
    emptyOutDir: true,
    manifest: true,
    outDir: "../src/pdf2html/web/dist",
    rollupOptions: {
      input: fileURLToPath(new URL("./frontend/index.html", import.meta.url)),
    },
  },
  test: {
    environment: "jsdom",
    include: ["**/*.test.ts"],
  },
});
