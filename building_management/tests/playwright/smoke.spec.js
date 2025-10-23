const { test, expect } = require('@playwright/test');

const buildingListPath = '/';

test.describe('Responsive smoke', () => {
  test('buildings list renders primary components', async ({ page }) => {
    await page.goto(buildingListPath);
    await expect(page.getByRole('heading', { name: 'Buildings' })).toBeVisible();
    await expect(page.getByRole('button', { name: /add new building/i })).toBeVisible();
    await expect(page.getByLabel(/search/i)).toBeVisible();
    await page.screenshot({ path: `./tests/playwright/artifacts/buildings-${test.info().project.name}.png`, fullPage: true });
  });

  test('building detail cards render when reachable', async ({ page }) => {
    await page.goto(buildingListPath);
    const firstRow = page.locator('a:has-text("Building")').first();
    if (await firstRow.count()) {
      await firstRow.click();
      await expect(page.getByText(/units/i)).toBeVisible();
      await page.screenshot({ path: `./tests/playwright/artifacts/building-detail-${test.info().project.name}.png`, fullPage: true });
    }
  });
});
