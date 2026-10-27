import { defineConfig } from '@playwright/test'
const evidenceScope =
  process.env.CS_EVIDENCE_SCOPE === 'V1-edge'
    ? 'v1-edge'
    : process.env.CS_EVIDENCE_SCOPE === 'V1-business'
      ? 'v1-business'
      : process.env.CS_EVIDENCE_SCOPE === 'B2'
        ? 'b2'
        : process.env.CS_EVIDENCE_SCOPE === 'debian-db'
          ? 'debian-db'
          : 'b1'
export default defineConfig({
  testDir: './e2e',
  workers: 1,
  timeout: 60000,
  reporter: [['list'], ['json', { outputFile: `../.runtime/${evidenceScope}-playwright.json` }]],
  outputDir: `../.runtime/playwright-output-${evidenceScope}`,
  use: {
    ...(process.env.CS_BROWSER_CHANNEL === 'chrome'
      ? { channel: 'chrome' }
      : process.env.CS_BROWSER_CHANNEL === 'edge'
        ? { channel: 'msedge' }
        : {}),
    baseURL: 'http://127.0.0.1:5173',
    viewport: { width: 1440, height: 1000 },
    trace: 'off',
    screenshot: 'off',
    video: 'off',
  },
  webServer: [
    {
      command: '"../backend/.venv/Scripts/python.exe" ../scripts/serve_local.py',
      url: 'http://127.0.0.1:8000/health/ready',
      reuseExistingServer: false,
      timeout: 30000,
    },
    {
      command: 'node node_modules/vite/bin/vite.js --host 127.0.0.1 --port 5173',
      url: 'http://127.0.0.1:5173',
      reuseExistingServer: false,
      timeout: 30000,
    },
  ],
})
