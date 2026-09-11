import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import path from 'path'

// Built output is COMMITTED to the repo (web/dist) so production needs no Node at all:
// the deploy stays `git reset --hard` + restart, exactly as it was before this app grew
// a build step. CI re-runs the build and fails if the committed dist has drifted.
export default defineConfig({
  plugins: [react()],
  resolve: { alias: { '@': path.resolve(__dirname, './src') } },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    // Vite's default target assumes a browser from the last couple of years. Telegram on
    // iOS runs whatever WKWebView the phone's iOS version ships, and a parse error there
    // is a silent white screen rather than an error anybody can see. Older syntax costs a
    // few KB and buys back every one of those phones.
    target: ['es2017', 'safari12', 'chrome64', 'firefox60'],
    // One JS and one CSS file with stable names. Flask serves them straight from disk
    // and nginx caches by content hash in the query string, so hashed filenames would
    // only make the Flask route harder for nothing.
    rollupOptions: {
      output: {
        entryFileNames: 'assets/app.js',
        chunkFileNames: 'assets/[name].js',
        assetFileNames: 'assets/app.[ext]',
      },
    },
  },
})
