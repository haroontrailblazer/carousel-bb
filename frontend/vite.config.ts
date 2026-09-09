import path from "node:path"
import { defineConfig } from "vite"
import react from "@vitejs/plugin-react"
import tailwindcss from "@tailwindcss/vite"

// The console is served from "/" by FastAPI in production (the ADK dev UI was
// moved to /dev precisely so this could own the root), so no base prefix is
// needed and the router needs no basename.
//
// Tailwind is wired through its Vite plugin rather than PostCSS: there is no
// framework here that owns the PostCSS pipeline, and the plugin is the native
// v4 path.

/**
 * Third-party code that changes on ITS schedule, not ours.
 *
 * The console shipped as a single 830 KB file, so every deploy - a copy
 * tweak, a colour - invalidated React, Radix and the Supabase SDK along with
 * it, and every user downloaded all of it again. Splitting the dependencies
 * that only move when they are upgraded means a normal deploy re-downloads
 * the app code and nothing else.
 *
 * Order matters: the first matching prefix wins, so `react-router` must be
 * tested before `react`.
 */
// Measured, not assumed. Pulling Radix into its own chunk makes the very
// first load ~3 KB larger - some of it belongs to screens the visitor has not
// opened - and makes every load AFTER a deploy ~25 KB smaller, because the app
// chunk is the only thing that changed. With hashed filenames served immutable
// (see web_api/spa.py) the second case is the common one.
const VENDOR_CHUNKS: [test: string, chunk: string][] = [
  ["@supabase/", "vendor-supabase"],
  ["@tanstack/", "vendor-query"],
  ["@radix-ui/", "vendor-radix"],
  ["react-router", "vendor-react"],
  ["react-dom", "vendor-react"],
  ["scheduler", "vendor-react"],
  ["react", "vendor-react"],
]

function vendorChunk(id: string): string | undefined {
  // Normalise Windows separators so the same rule matches on both platforms.
  const normalized = id.replace(/\\/g, "/")
  const marker = "/node_modules/"
  const at = normalized.lastIndexOf(marker)
  if (at === -1) return undefined
  const specifier = normalized.slice(at + marker.length)
  for (const [test, chunk] of VENDOR_CHUNKS) {
    if (specifier.startsWith(test)) return chunk
  }
  // Everything else is left to Rollup.
  //
  // A catch-all "vendor" bucket was the obvious next line and produced a
  // circular chunk: react-router's own dependencies (cookie, turbo-stream)
  // fell into it, so vendor-react imported vendor, while lucide and sonner
  // import react, so vendor imported vendor-react. Two chunks that each need
  // the other to have finished evaluating is how a bundle throws a
  // cannot-access-before-initialization error at a user and nowhere else.
  //
  // Rollup's own placement has no such problem: it puts a shared module in a
  // chunk that provably loads before every chunk that needs it.
  return undefined
}

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: { "@": path.resolve(__dirname, "./src") },
  },
  build: {
    // web_app.py serves this directory; see SPA_DIST there.
    outDir: "dist",
    // "hidden" rather than true: the map is still written, so a stack trace
    // from production can still be read, but no sourceMappingURL comment
    // points at it - so nothing offers a 3.9 MB download to anyone who opens
    // devtools on the live site.
    sourcemap: "hidden",
    target: "es2022",
    rollupOptions: {
      output: {
        manualChunks: vendorChunk,
      },
    },
  },
  server: {
    // Listen on the LAN so a 4:5 Instagram slide can be checked on a phone -
    // judging a portrait carousel on a desktop monitor is not the same thing.
    host: true,
    port: 5173,
    proxy: {
      // Same-origin in dev as in production, so the API client needs no base
      // URL logic and cookies behave identically.
      //
      // Note: no compression is configured anywhere in this proxy. Gzip
      // buffers, and buffering turns the live run trace into a single burst
      // when the run finishes - the same class of bug the backend avoids by
      // keeping its auth layer raw ASGI.
      "/api": { target: "http://127.0.0.1:8000", changeOrigin: true },
      "/review-api": { target: "http://127.0.0.1:8000", changeOrigin: true },
      "/dev": { target: "http://127.0.0.1:8000", changeOrigin: true },
    },
  },
})
