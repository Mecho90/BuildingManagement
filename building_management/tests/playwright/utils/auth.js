const { expect } = require('@playwright/test');
const { ensurePlaywrightEnv } = require('./env');

const USER_ENV = 'PLAYWRIGHT_USERNAME';
const PASS_ENV = 'PLAYWRIGHT_PASSWORD';
const DEFAULT_USER = 'mdonev';
const DEFAULT_PASS = 'qwerty123';

async function loginIfNeeded(page, { destination = '/' } = {}) {
  ensurePlaywrightEnv();

  await page.goto(destination);
  await page.waitForLoadState('domcontentloaded');

  const loginHeading = page.getByRole('heading', { name: /sign in/i });
  if ((await loginHeading.count()) === 0 && !page.url().includes('/login')) {
    return true;
  }

  const username = process.env[USER_ENV] || DEFAULT_USER;
  const password = process.env[PASS_ENV] || DEFAULT_PASS;

  await page.fill('input[name="username"]', username);
  await page.fill('input[name="password"]', password);
  await page.click('button[type="submit"]');
  await page.waitForLoadState('domcontentloaded');

  // Sanity check we are no longer on the login page
  await expect(page).not.toHaveURL(/\/login\/?$/);
  return true;
}

module.exports = {
  loginIfNeeded,
};
