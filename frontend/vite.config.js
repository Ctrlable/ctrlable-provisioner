import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  // Relative asset URLs so the built app works both at the server root (direct)
  // and under a dynamic prefix — HA ingress serves it at /api/hassio_ingress/<token>/
  // and the proxy injects a matching <base href>. Absolute "/assets/..." would
  // escape that prefix.
  base: './',
  plugins: [react()],
  server: {
    proxy: {
      '/api': 'http://localhost:8000',
      '/health': 'http://localhost:8000',
    },
  },
})
