const { test, expect } = require('@playwright/test');
const { loginIfNeeded } = require('./utils/auth');

const buildingListPath = '/buildings/';

async function ensureBuildingId(page) {
  await page.goto(buildingListPath);
  await page.waitForLoadState('domcontentloaded');
  const buildingLink = page.locator('a[href^="/buildings/"]').first();
  if ((await buildingLink.count()) === 0) {
    await page.goto('/buildings/new/');
    await page.fill('input[name="name"]', `Playwright HQ ${Date.now()}`);
    await page.fill('input[name="address"]', '123 Playwright Ave');
    await page.click('button:has-text("Save")');
    await page.waitForURL(/\/buildings\/\d+\//);
    const match = page.url().match(/\/buildings\/(\d+)\//);
    if (!match) {
      throw new Error('Unable to determine building id after creation');
    }
    return match[1];
  }
  const href = await buildingLink.getAttribute('href');
  const match = href && href.match(/\/buildings\/(\d+)\//);
  if (!match) {
    throw new Error('Unable to parse building id from list');
  }
  return match[1];
}

test.describe('Responsive smoke', () => {
  test('buildings list renders primary components', async ({ page }) => {
    await loginIfNeeded(page, { destination: buildingListPath });
    await page.goto(buildingListPath);
    await expect(page.getByRole('heading', { name: 'Buildings' })).toBeVisible();
    await expect(page.getByRole('button', { name: /add new building/i })).toBeVisible();
    await expect(page.getByLabel(/search/i)).toBeVisible();
    await page.screenshot({ path: `./tests/playwright/artifacts/buildings-${test.info().project.name}.png`, fullPage: true });
  });

  test('building detail cards render when reachable', async ({ page }) => {
    await loginIfNeeded(page, { destination: buildingListPath });
    await page.goto(buildingListPath);
    const firstRow = page.locator('a:has-text("Building")').first();
    if (await firstRow.count()) {
      await firstRow.click();
      await expect(page.getByText(/units/i)).toBeVisible();
      await page.screenshot({ path: `./tests/playwright/artifacts/building-detail-${test.info().project.name}.png`, fullPage: true });
    }
  });

  test('user can snooze a deadline notification', async ({ page }) => {
    await loginIfNeeded(page, { destination: buildingListPath });

    const buildingId = await ensureBuildingId(page);

    await page.goto(`/work-orders/new/?building=${buildingId}`);
    await page.waitForSelector('form');
    const tomorrow = new Date();
    tomorrow.setDate(tomorrow.getDate() + 1);
    const deadlineStr = tomorrow.toISOString().split('T')[0];

    const uniqueTitle = `Playwright Deadline ${Date.now()}`;
    await page.fill('input[name="title"]', uniqueTitle);
    const descriptionField = page.locator('textarea[name="description"]');
    if (await descriptionField.count()) {
      await descriptionField.fill('Playwright generated work order for deadline notification smoke test.');
    }
    await page.fill('input[name="deadline"]', deadlineStr);
    await page.click('button:has-text("Save")');
    await page.waitForURL(new RegExp(`/buildings/${buildingId}/`));

    await page.goto(buildingListPath);
    const notificationLocator = page.locator('[data-notification-key^="wo-deadline-"]');
    await expect(notificationLocator.first()).toBeVisible();
    const noteKey = await notificationLocator.first().getAttribute('data-notification-key');
    const specificLocator = page.locator(`[data-notification-key="${noteKey}"]`);
    await expect(specificLocator.getByText(/new/i)).toBeVisible();
    await specificLocator.getByRole('button', { name: /dismiss/i }).click();
    await page.waitForSelector(`[data-notification-key="${noteKey}"]`, { state: 'detached' });
  });
});
