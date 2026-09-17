import { test, expect } from '@playwright/test';

test('debugger chatbot shell loads with connection status', async ({ page }) => {
  await page.goto('http://localhost:5174');
  await expect(page.getByText('Fixhub debugger')).toBeVisible({ timeout: 30000 });
  // chatbot is the default tab with repo-scoped prompt
  await expect(page.getByPlaceholder(/Select a repo, then chat/)).toBeVisible();
});

test('demo autonomous fix shows real verification', async ({ page }) => {
  test.setTimeout(600000);
  await page.goto('http://localhost:5174');
  await expect(page.getByText('Fixhub debugger')).toBeVisible({ timeout: 30000 });
  await page.getByRole('button', { name: 'Start Autonomous Fix (demo)' }).first().click();
  // task transitions appear in the header badge once the backend responds
  await expect(page.getByText(/Task #\d+/).first()).toBeVisible({ timeout: 120000 });
  // real autonomous loop: investigate + fix + Docker verification (~3-9 min).
  // Proof tab renders PASS/FAIL rows from real verification records.
  await page.getByRole('button', { name: 'Proof' }).click();
  await expect(page.getByText(/PROOF OF FIX|PASS|FAIL/).first()).toBeVisible({ timeout: 540000 });
});
