// playwright.config.js
// Lightweight smoke configuration targeting key breakpoints for responsive verification.

const { defineConfig, devices } = require('@playwright/test');

module.exports = defineConfig({
  testDir: './tests/playwright',
  fullyParallel: true,
  timeout: 60 * 1000,
  expect: {
    timeout: 10 * 1000,
  },
  reporter: [['list']],
  globalSetup: require.resolve('./tests/playwright/global-setup.js'),
  use: {
    baseURL: process.env.BASE_URL || 'http://localhost:8000',
    screenshot: 'only-on-failure',
    trace: 'on-first-retry',
    video: 'retain-on-failure',
  },
  projects: [
    {
      name: 'mobile',
      use: { ...devices['iPhone 13 Mini'] },
    },
    {
      name: 'desktop',
      use: { ...devices['Desktop Chrome'] },
    },
  ],
});
