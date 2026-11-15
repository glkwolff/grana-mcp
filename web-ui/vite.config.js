import { defineConfig } from "vite";
import preact from "@preact/preset-vite";

// Build straight into the Python package; the files are committed so that
// `pip install grana-mcp` needs no Node. Dev: `npm run dev` proxies the API.
export default defineConfig({
  plugins: [preact()],
  build: {
    outDir: "../src/grana_mcp/web/static",
    emptyOutDir: true,
    assetsDir: "assets",
    sourcemap: false,
    modulePreload: { polyfill: false },
  },
  server: { proxy: { "/api": "http://127.0.0.1:8765" } },
});
