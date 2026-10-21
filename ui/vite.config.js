import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// The console calls the API with relative paths (/api/...). In `npm run dev`
// the Vite server forwards them to the bot's web server on 7327.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: { '/api': 'http://127.0.0.1:7327' },
  },
  build: { outDir: 'dist', chunkSizeWarningLimit: 900 },
})
