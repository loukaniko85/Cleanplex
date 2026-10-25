import { defineConfig, devices } from '@playwright/test'

// End-to-end tests run the real FastAPI app + built React bundle on a throwaway
// SQLite database seeded by e2e/seed.py. This is the layer that catches what
// unit tests cannot: the process booting, the UI rendering that data, and the
// browser flows a user actually performs.
export default defineConfig({
  testDir: './tests',
  fullyParallel: false,
  workers: 1,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [['list'], ['html', { open: 'never' }]] : 'list',
  timeout: 30_000,
  expect: { timeout: 10_000 },
  use: {
    baseURL: 'http://127.0.0.1:7985',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  projects: [
    { name: 'chromium', use: { ...devices['Desktop Chrome'] } },
  ],
  webServer: {
    // Playwright runs this with cwd = e2e/; the script itself cd's to the repo root.
    command: 'bash start-server.sh',
    url: 'http://127.0.0.1:7985/api/health',
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
    stdout: 'pipe',
    stderr: 'pipe',
  },
})
