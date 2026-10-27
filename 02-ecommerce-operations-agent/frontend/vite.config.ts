import { defineConfig } from 'vitest/config'
import vue from '@vitejs/plugin-vue'

export default defineConfig(() => ({
  base: '/operations/',
  plugins: [vue()],
  server: { proxy: { '/api/ops/v1': { target: 'http://127.0.0.1:8120', changeOrigin: false }, '/api/v1': { target: 'http://127.0.0.1:8000', changeOrigin: false }, '/login': { target: 'http://127.0.0.1:8000', changeOrigin: false } } },
  test: { environment: 'happy-dom', include: ['src/**/*.test.ts'], restoreMocks: true },
}))
