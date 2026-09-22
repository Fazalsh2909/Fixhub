import { test, expect } from '@playwright/test';

// Covers the question: does the UI agent behave like the auto-trigger agent?
// Asserts the engineering workspace loads, the Session tab exists, and the
// interactive session loop (POST /api/agent/sessions + message) works.
const REPO = 'demo/files-7df4ee2a';

test('engineering shell loads with session tab', async ({ page }) => {
  await page.goto('http://localhost:5174');
  await expect(page.getByText('FixHub').first()).toBeVisible({ timeout: 30000 });
  await expect(
    page.getByRole('button', { name: 'Start Autonomous Fix' })
  ).toBeVisible();
  // Right-side tabs: Session is the interactive coding agent under test.
  await expect(page.getByRole('tab', { name: 'SESSION' })).toBeVisible();
  await expect(page.getByRole('tab', { name: 'ENGINEER' })).toBeVisible();
  // Bottom evidence tabs replace the old Tasks tab.
  await expect(page.getByRole('tab', { name: /Activity/ })).toBeVisible();
});

test('ui agent session runs a tool turn (opencode-style)', async ({ request }) => {
  test.setTimeout(180000);
  const created = await request.post('http://localhost:8001/api/agent/sessions', {
    data: { repo: REPO },
  });
  expect(created.ok()).toBeTruthy();
  const session = await created.json();
  expect(session.id).toBeGreaterThan(0);

  const turn = await request.post(
    `http://localhost:8001/api/agent/sessions/${session.id}/message`,
    { data: { content: 'List the files in this repo using list_files.', max_turns: 1 } }
  );
  expect(turn.ok()).toBeTruthy();
  const body = await turn.json();
  // Bounded loop: one user turn -> tool call -> paused (frontend auto-continues).
  expect(['done', 'paused']).toContain(body.status);
  expect(body.error).toBeNull();
  const tools = body.messages.filter((m: { role: string }) => m.role === 'tool');
  expect(tools.length).toBeGreaterThan(0);
  expect(tools.every((t: { ok: boolean }) => t.ok)).toBeTruthy();
  // Opencode-style timeline fields ride on every row.
  expect(tools.every((t: { duration_ms: number }) => typeof t.duration_ms === 'number')).toBeTruthy();
  const assistants = body.messages.filter((m: { role: string }) => m.role === 'assistant');
  expect(assistants.every((m: { thinking: string }) => typeof m.thinking === 'string')).toBeTruthy();
  expect('plan' in body).toBeTruthy();
});

test('ui agent streams rows live over sse', async ({ request }) => {
  test.setTimeout(180000);
  const created = await request.post('http://localhost:8001/api/agent/sessions', {
    data: { repo: REPO },
  });
  const session = await created.json();
  const res = await request.get(
    `http://localhost:8001/api/agent/sessions/${session.id}/stream?content=${encodeURIComponent('List the files using list_files.')}&max_turns=1`,
  );
  expect(res.ok()).toBeTruthy();
  expect(res.headers()['content-type']).toContain('text/event-stream');
  const text = await res.text();
  expect(text).toContain('event: message');
  expect(text).toContain('event: done');
  expect(text).toContain('"role": "user"');
  expect(text).toContain('"role": "tool"');
});

test('auto-trigger path creates a Task, not a session', async ({ request }) => {
  const res = await request.post('http://localhost:8001/api/tasks', {
    data: { repo: REPO, title: 'playwright probe: list files' },
  });
  expect(res.ok()).toBeTruthy();
  const body = await res.json();
  expect(body.task_id).toBeGreaterThan(0);
  // Distinct systems: tasks advance TaskEvent state machine; sessions use AgentMessage.
  const detail = await request.get(`http://localhost:8001/api/tasks/${body.task_id}`);
  expect(detail.ok()).toBeTruthy();
  const task = await detail.json();
  expect(task.id).toBe(body.task_id);
  expect(typeof task.state).toBe('string');
});
