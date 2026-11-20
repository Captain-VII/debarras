import { defineConfig } from "vite";

// Port imposé par tauri.conf.json ; le code Python et Rust n'est pas surveillé.
export default defineConfig({
  clearScreen: false,
  server: { port: 1420, strictPort: true, watch: { ignored: ["**/src-tauri/**", "**/target/**", "**/.venv/**"] } },
  build: { outDir: "ui-dist", emptyOutDir: true, target: "es2022" },
});
