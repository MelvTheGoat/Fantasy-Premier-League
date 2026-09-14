import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  // Served from the site root when the API serves it, and from a repository
  // subpath on GitHub Pages. Every asset and API path is built relative to
  // this, so one build works in both places.
  base: process.env.VITE_BASE || '/',

  plugins: [react()],
  server: {
    port: 5173,
    // The API runs separately in development; proxying keeps the frontend on
    // same-origin paths so nothing changes when the two are deployed together.
    proxy: {
      '/api': { target: 'http://127.0.0.1:8000', changeOrigin: true },
    },
  },
})
