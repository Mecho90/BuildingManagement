import { test, expect } from '@playwright/test';
import { Buffer } from 'buffer';

const { loginIfNeeded } = require('./utils/auth');

const buildingListPath = '/buildings/';

async function createBuilding(page) {
  const unique = Date.now();
  await page.goto('/buildings/new/');
  await page.waitForSelector('form');
  await page.fill('input[name="name"]', `Playwright Attachments ${unique}`);
  await page.fill('input[name="address"]', `123 Attachment Blvd ${unique}`);
  await page.click('button:has-text("Save")');
  await page.waitForURL(/\/buildings\/\d+\//);
  const match = page.url().match(/\/buildings\/(\d+)\//);
  if (!match) {
    throw new Error('Unable to determine building id after creation');
  }
  return match[1];
}

function createDeadlineString(offsetDays = 2) {
  const date = new Date();
  date.setDate(date.getDate() + offsetDays);
  return date.toISOString().split('T')[0];
}

async function createWorkOrder(page, buildingId) {
  const title = `Playwright Attachment Work Order ${Date.now()}`;
  const createUrl = `/work-orders/new/?building=${buildingId}`;
  await page.goto(createUrl);
  await page.waitForSelector('form');

  await page.fill('input[name="title"]', title);
  await page.fill('input[name="deadline"]', createDeadlineString());
  const descriptionField = page.locator('textarea[name="description"]');
  if (await descriptionField.count()) {
    await descriptionField.fill('Automated attachments smoke test.');
  }

  await page.click('button:has-text("Save")');
  await page.waitForURL(new RegExp(`/buildings/${buildingId}/`));

  const editLink = page.getByRole('link', { name: title }).first();
  await expect(editLink).toBeVisible();
  const href = await editLink.getAttribute('href');
  const match = href && href.match(/work-orders\/(\d+)\/edit/);
  if (!match) {
    throw new Error('Unable to determine work order id after creation');
  }
  return { id: match[1], title };
}

function imageBuffer() {
  const base64 =
    'iVBORw0KGgoAAAANSUhEUgAAAA8AAAAQCAYAAADJViUEAAAACXBIWXMAAAsSAAALEgHS3X78AAAAGUlEQVQ4y2NgGAV0A8b///+vGAgjBiYqAwDKzQdKt8Ct7wAAAABJRU5ErkJggg==';
  return Buffer.from(base64, 'base64');
}

test.describe('Work order attachments', () => {
  test('user can upload, zoom, and delete attachments inline', async ({ page }) => {
    await loginIfNeeded(page, { destination: buildingListPath });

    const buildingId = await createBuilding(page);
    const { id: workOrderId } = await createWorkOrder(page, buildingId);

    await page.goto(`/work-orders/${workOrderId}/`);
    await expect(page.getByRole('heading', { name: /attachments/i })).toBeVisible();

    const attachmentsWrapper = page.locator('[data-attachments-wrapper]');
    const attachmentsGrid = attachmentsWrapper.locator('[data-attachments]');
    const emptyState = attachmentsWrapper.locator('[data-attachments-empty]');
    await expect(emptyState).toBeVisible();

    const uploadRoot = page.locator('[data-attachment-upload]');
    const fileInput = uploadRoot.locator('[data-attachment-upload-input]');
    const queueItems = uploadRoot.locator('.attachment-upload__item');

    const fileName = `playwright-${Date.now()}.png`;
    await fileInput.setInputFiles({
      name: fileName,
      mimeType: 'image/png',
      buffer: imageBuffer(),
    });

    const activeQueueItem = queueItems.first();
    await expect(activeQueueItem).toBeVisible();
    const card = attachmentsGrid.locator('.attachments-grid__item').first();
    await expect(card).toBeVisible();
    await expect(card).toContainText(fileName);

    const cardId = await card.getAttribute('data-attachment-id');
    await expect(cardId).not.toBeNull();

    const zoomTrigger = card.locator('[data-attachment-viewer]').first();
    await zoomTrigger.click();
    const overlay = page.locator('.attachment-lightbox');
    await expect(overlay).toBeVisible();
    await overlay.locator('[data-action="close"]').click();
    await expect(overlay).toBeHidden();

    await activeQueueItem.waitFor({ state: 'detached' });

    page.once('dialog', (dialog) => dialog.accept());
    await card.locator('[data-attachment-delete]').click();
    await expect(
      attachmentsGrid.locator(`[data-attachment-id="${cardId}"]`)
    ).toHaveCount(0);
    await expect(attachmentsGrid.locator('.attachments-grid__item')).toHaveCount(0);
    await expect(emptyState).toBeVisible();
  });
});
