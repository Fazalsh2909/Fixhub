import { defineConfig } from '@playwright/test';
export default defineConfig({
  testDir: './e2e',
  timeout: 90000,
  // Use system Edge (Chromium) — avoids a ~160MB browser download on slow networks.
  use: { baseURL: 'http://localhost:5174', channel: 'msedge', headless: true },
});
