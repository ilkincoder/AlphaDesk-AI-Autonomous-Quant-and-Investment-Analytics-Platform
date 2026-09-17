import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vitest/config'

export default defineConfig({
  plugins: [react(), tailwindcss()],

  server: {
    port: 5173,
    // Fail loudly instead of quietly moving to 5174, which would leave the Compose
    // port mapping pointing at nothing.
    strictPort: true,

    proxy: {
      // The browser only ever calls /api/... on this same origin, so there is no CORS
      // and no backend hostname in client code. Vite forwards those requests from
      // inside the container, where "backend" is the Compose service name.
      '/api': {
        target: 'http://backend:8000',
        changeOrigin: true,
        // FastAPI serves /portfolio/valuation; the browser asked for
        // /api/portfolio/valuation. The prefix exists only to mark the boundary.
        rewrite: (path) => path.replace(/^\/api/, ''),
      },
    },
  },

  test: {
    environment: 'jsdom',
    // @testing-library/react registers its automatic cleanup through the global
    // afterEach, which only exists when this is on.
    globals: true,
  },
})
