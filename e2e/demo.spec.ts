import { test, expect } from '@playwright/test';

test('ide shell loads with resizable panels', async ({ page }) => {
  await page.goto('http://localhost:5174');
  await expect(page.getByText('FixHub').first()).toBeVisible({ timeout: 30000 });
  await expect(page.getByText('AUTONOMOUS AI ENGINEER')).toBeVisible();
  // VS Code shell: activity bar, side bar, center workspace, right AI panel, bottom panel.
  await expect(page.getByRole('navigation', { name: 'Activity bar' })).toBeVisible();
  await expect(page.getByRole('separator', { name: 'Resize side bar' })).toBeVisible();
  await expect(page.getByRole('separator', { name: 'Resize AI panel' })).toBeVisible();
  await expect(page.getByRole('separator', { name: 'Resize bottom panel' })).toBeVisible();
  // Compact pipeline strip binds to the selected run (empty state when idle).
  await expect(page.getByText('FLOW').first()).toBeVisible();
  // Right-side IDE tabs: chat lives on the right, not in the center.
  const rightTabs = page.getByRole('tablist', { name: 'Right panel' });
  await expect(rightTabs.getByRole('tab', { name: 'AI Engineer' })).toBeVisible();
  await expect(rightTabs.getByRole('tab', { name: 'Chat' })).toBeVisible();
  await expect(rightTabs.getByRole('tab', { name: 'Terminal' })).toBeVisible();
  await expect(rightTabs.getByRole('tab', { name: 'Session' })).toBeVisible();
  // Bottom developer tabs.
  const bottomTabs = page.getByRole('tablist', { name: 'Bottom panel' });
  await expect(bottomTabs.getByRole('tab', { name: /Verification/ })).toBeVisible();
  await expect(bottomTabs.getByRole('tab', { name: /Proof of Fix/ })).toBeVisible();
});

test('chat on the right shows the real empty state', async ({ page }) => {
  await page.goto('http://localhost:5174');
  await expect(page.getByText('FixHub').first()).toBeVisible({ timeout: 30000 });
  await page.getByRole('tab', { name: 'Chat' }).click();
  await expect(page.getByText(/AI Engineer chat/).first()).toBeVisible();
});
