import { defineConfig } from 'vitest/config'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  server: {
    strictPort: true,
    proxy: {
      '/api/ops/v1': { target: 'http://127.0.0.1:8120' },
      '/operations': { target: 'http://127.0.0.1:5174', ws: true },
      '/api': { target: 'http://127.0.0.1:8000', ws: true },
      '/health': 'http://127.0.0.1:8000',
    },
  },
  test: { environment: 'jsdom', include: ['tests/**/*.spec.ts'] },
})
