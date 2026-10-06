// Run with: node --test tests/test_homepage_metrics.cjs.
const { test, after } = require('node:test');
const assert = require('node:assert/strict');
const { spawn } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'homepage-metrics-test-'));
// Extract this literal block to test the exact deployed code without dependencies.
const manifest = fs.readFileSync('apps/config/homepage/metrics-configmap.yaml', 'utf8');
const block = manifest.split('  server.cjs: |\n')[1].split('  actual-worker.cjs: |\n')[0];
const script = block.split('\n').map(line => line.slice(4)).join('\n');
const scriptPath = path.join(temp, 'server.cjs');
fs.writeFileSync(scriptPath, script);
const { monthInManila, budgetSummary, blockySummary } = require(scriptPath);
after(() => fs.rmSync(temp, { recursive: true, force: true }));

test('month switches at midnight Manila, independent of host timezone', () => {
  assert.equal(monthInManila(new Date('2026-09-30T15:59:59Z')), '2026-09');
  assert.equal(monthInManila(new Date('2026-09-30T16:00:00Z')), '2026-10');
});
test('available and overspent stay separate, including rollover and hidden deficits', () => {
  const summary = budgetSummary({ month: '2026-10', categoryGroups: [
    { is_income: true, categories: [{ balance: 900000 }] },
    { categories: [{ balance: 15000, budgeted: 10000, carryover: true }, { balance: -2500 }, { balance: 0 }] },
    { hidden: true, categories: [{ hidden: true, balance: -1000 }] },
  ] });
  assert.equal(summary.available, '₱150.00');
  assert.equal(summary.overspent, '₱35.00');
  assert.deepEqual(summary.metrics.map(m => m.label), ['Month', 'Available', 'Overspent']);
});
test('invalid balances fail instead of silently showing zero', () => {
  assert.throws(() => budgetSummary({ categoryGroups: [{ categories: [{}] }] }));
});
test('Blocky sums all nodes and uses weighted percentages, not averages', () => {
  const result = blockySummary([
    { status: { enabled: true }, stats: { summary: { queries: 90, blocked: 9, cached: 45, errors: 1 } } },
    { status: { enabled: false }, stats: { summary: { queries: 10, blocked: 5, cached: 5, errors: 2 } } },
  ]);
  assert.equal(result.blocking, 'Mixed');
  assert.equal(result.queries, 100);
  assert.equal(result.blockedPercent, '14.0');
  assert.equal(result.cacheHitPercent, '50.0');
  assert.equal(result.errors, 3);
});
test('empty or incomplete statistics fail and zero queries do not divide by zero', () => {
  assert.throws(() => blockySummary([]));
  assert.throws(() => blockySummary([{ status: { enabled: true }, stats: {} }]));
  const summary = blockySummary([{ status: { enabled: false }, stats: { summary: { queries: 0, blocked: 0, cached: 0, errors: 0 } } }]);
  assert.equal(summary.blocking, 'Off');
  assert.equal(summary.blockedPercent, '0.0');
});
test('HTTP exposes only read endpoints and reports missing credentials honestly', async () => {
  const env = { ...process.env, BLOCKY_DNS: 'does-not-exist.invalid' };
  delete env.ACTUAL_SERVER_PASSWORD; delete env.ACTUAL_SYNC_ID;
  const child = spawn(process.execPath, [scriptPath], { env, stdio: 'ignore' });
  try {
    let ready = false;
    for (let i = 0; i < 50; i++) {
      try { ready = (await fetch('http://127.0.0.1:8080/healthz')).ok; } catch {}
      if (ready) break;
      await new Promise(r => setTimeout(r, 20));
    }
    assert.ok(ready);
    assert.equal((await fetch('http://127.0.0.1:8080/actual')).status, 503);
    assert.equal((await fetch('http://127.0.0.1:8080/blocky')).status, 503);
    assert.equal((await fetch('http://127.0.0.1:8080/actual', { method: 'POST' })).status, 405);
    assert.equal((await fetch('http://127.0.0.1:8080/transactions')).status, 404);
  } finally {
    child.kill();
    await new Promise(resolve => child.once('exit', resolve));
  }
});
