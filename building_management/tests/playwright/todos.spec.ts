import { test, expect } from '@playwright/test';
import { credentials } from './utils/env';

const creds = credentials();

async function loginIfPossible(page) {
  if (!creds) {
    return false;
  }
  await page.goto('/login/');
  await page.getByLabel(/username/i).fill(creds.username);
  await page.getByLabel(/password/i).fill(creds.password);
  await page.getByRole('button', { name: /log in/i }).click();
  await page.waitForURL((url) => !url.pathname.includes('/login'), { timeout: 10000 });
  return true;
}

test.describe('To-Do page', () => {
  test('redirects anonymous users to login', async ({ page }) => {
    await page.goto('/todos/');
    await expect(page).toHaveURL(/login/);
  });

  test('shows planner shell after login when creds provided', async ({ page }) => {
    test.skip(!creds, 'Set E2E_TODO_USERNAME/PASSWORD to run authenticated coverage.');
    await loginIfPossible(page);
    await page.goto('/todos/');
    await expect(page.getByRole('heading', { name: /planner/i })).toBeVisible();
    await expect(page.getByRole('button', { name: /quick add/i })).toBeVisible();
  });
});
