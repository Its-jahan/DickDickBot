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
