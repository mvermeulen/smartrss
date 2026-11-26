import crypto from "node:crypto";
import path from "node:path";
import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig, type Plugin } from "vite";

/**
 * Emits exact build-time precache URL list & deterministic build hash into dist/sw.js (1c §7.1).
 */
function smartrssSwManifestPlugin(): Plugin {
  return {
    name: "smartrss-sw-manifest",
    apply: "build",
    generateBundle(_options, bundle) {
      const precache = new Set<string>([
        "/",
        "/index.html",
        "/manifest.webmanifest",
        "/icons/icon.svg",
      ]);
      const hash = crypto.createHash("sha256");

      for (const [fileName, item] of Object.entries(bundle)) {
        if (fileName === "sw.js" || fileName.endsWith(".map")) {
          continue;
        }
        precache.add("/" + fileName);
        if (item.type === "chunk") {
          hash.update(fileName + ":" + item.code);
        } else if (item.type === "asset") {
          hash.update(fileName + ":" + String(item.source));
        }
      }

      const buildHash = hash.digest("hex").slice(0, 12);
      const manifestJson = JSON.stringify(Array.from(precache).sort());

      const swChunk = bundle["sw.js"];
      if (swChunk && swChunk.type === "chunk") {
        swChunk.code = swChunk.code
          .replace("__SMARTRSS_PRECACHE_MANIFEST__", manifestJson)
          .replace("__SMARTRSS_BUILD_HASH__", JSON.stringify(buildHash));
      }
    },
  };
}

export default defineConfig({
  plugins: [react(), tailwindcss(), smartrssSwManifestPlugin()],
  server: {
    port: 5173,
    strictPort: true,
  },
  preview: {
    port: 4173,
    strictPort: true,
  },
  build: {
    target: "es2022",
    sourcemap: false,
    rollupOptions: {
      input: {
        main: path.resolve(__dirname, "index.html"),
        sw: path.resolve(__dirname, "src/sw/sw.ts"),
      },
      output: {
        entryFileNames(chunkInfo) {
          if (chunkInfo.name === "sw") {
            return "sw.js";
          }
          return "assets/[name]-[hash].js";
        },
        chunkFileNames: "assets/[name]-[hash].js",
        assetFileNames: "assets/[name]-[hash][extname]",
      },
    },
  },
});
