import { defineConfig } from '@playwright/test'
import base from './playwright.config'

if (process.env.CS_REAL_MODELS !== '1' || process.env.CS_VERIFICATION_TARGET !== 'test') {
  throw new Error('Real model browser verification requires explicit opt-in and fixed test database')
}

export default defineConfig({
  ...base,
  testDir: './real-model-e2e',
  timeout: 90000,
  reporter: [['list'], ['json', { outputFile: '../.runtime/real-model-browser.json' }]],
  outputDir: '../.runtime/playwright-output-real-model',
  use: { ...base.use, channel: 'chrome' },
})
