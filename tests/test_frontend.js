/* Offline P2 regression tests. All HTTP, DOM, and SSE effects are in-memory. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = path.resolve(process.env.VOC_TEST_SRC || path.join(__dirname, '..'));
const code = fs.readFileSync(path.join(source, 'web/app.js'), 'utf8');
function section(start, end) {
  const from = code.indexOf(start);
  const to = code.indexOf(end, from);
  assert.ok(from >= 0 && to > from, `Missing source section ${start}`);
  return code.slice(from, to);
}
const functions = [
  section('async function openAftersaleOverview(', 'function syncGroupFromPlatform()'),
  section('function applyPlatform(', 'function applyPlatformTexts('),
  section('function watchJob(', 'function bindResultEvents()'),
  section('async function openDataset(', 'function renderResult()'),
].join('\n');
const initialState = section('const state = {', '/* ============================ 登录与权限');

function gate() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
const platforms = [
  { id: 'AmazonCs', kind: 'aftersale', group: '售后VOC' },
  { id: 'WayfairCs', kind: 'aftersale', group: '售后VOC' },
  { id: 'Amazon', kind: 'ecommerce', group: '产品VOC' },
];
function context() {
  const state = vm.runInNewContext(initialState + '\nstate;');
  Object.assign(state, { platforms, platform: 'AmazonCs', datasets: [
    { id: 'AMAZON_REPORT', platform: 'AmazonCs', status: 'done', created_at: '2026-11-10' },
    { id: 'WAYFAIR_REPORT', platform: 'WayfairCs', status: 'done', created_at: '2026-11-10' },
  ], sources: [], current: null, result: null, reportMd: '', files: ['pending.csv'] });
  const effects = [], events = [], elements = new Map();
  const routes = new Map();
  const c = { state, effects, events, Date, console, routes,
    loadDatasets: async () => {},
    api: async (p, options) => {
      effects.push({ path: p, method: options?.method || 'GET' });
      if (routes.has(p)) return routes.get(p)(options);
      if (p === '/api/feishu/sources') return { json: async () => ({ sources: state.sources }) };
      const [, id, suffix] = p.match(/^\/api\/datasets\/([^/]+)(?:\/(result|report))?$/) || [];
      const rec = state.datasets.find(row => row.id === id);
      assert.ok(rec, `Unexpected mock path ${p}`);
      if (suffix === 'result') return { json: async () => ({ report_type: 'aftersale', platform: rec.platform }) };
      if (suffix === 'report') return { text: async () => `Report ${id}` };
      return { json: async () => rec };
    },
    $: selector => {
      if (!elements.has(selector)) elements.set(selector, { style: {}, hidden: true, textContent: '', className: '' });
      return elements.get(selector);
    },
    $$: () => [],
    showView: view => { state.view = view; effects.push({ view }); },
    renderAftersale: () => effects.push({ rendered: state.current.id, resultPlatform: state.result.platform }),
    renderResult: () => effects.push({ rendered: state.current.id, resultPlatform: state.result.platform }),
    EventSource: class {
      constructor(url) { this.url = url; this.closed = false; events.push(this); }
      close() { this.closed = true; }
    },
  };
  for (const fn of ['syncGroupFromPlatform', 'applyPlatformTexts', 'updateFeishuUI',
    'renderDemos', 'renderDatasets', 'resizeAllCharts', 'renderFileList',
    'updateStartState', 'showError', 'toast']) c[fn] = () => {};
  vm.createContext(c);
  vm.runInContext(functions, c);
  return c;
}
async function tick() { for (let i = 0; i < 20; i++) await Promise.resolve(); }
function renders(c) { return c.effects.filter(e => e.rendered).map(e => e.rendered); }
function assertWayfair(c) {
  assert.equal(c.state.platform, 'WayfairCs');
  assert.equal(c.state.current?.id, 'WAYFAIR_REPORT');
  assert.equal(c.state.result?.platform, 'WayfairCs');
  assert.equal(c.state.view, 'result');
  assert.deepEqual(renders(c), ['WAYFAIR_REPORT']);
}
const tests = [];
function test(name, body) { tests.push({ name, body }); }

test('Wayfair empty state never synchronizes another platform source', async () => {
  const c = context();
  c.state.platform = 'WayfairCs'; c.state.datasets = [];
  c.state.sources = [{ id: 'AMAZON_SOURCE', platform: 'AmazonCs', auto_sync: true, last_sync_at: 0 }];
  c.routes.set('/api/feishu/sources/AMAZON_SOURCE/sync', async () => ({}));
  await c.openAftersaleOverview('WayfairCs');
  assert.equal(c.effects.filter(e => e.method === 'POST').length, 0);
  assert.equal(c.state.view, 'setup');
});

test('A due source on the selected platform still synchronizes', async () => {
  const c = context();
  c.state.platform = 'WayfairCs';
  c.state.sources = [
    { id: 'AMAZON_SOURCE', platform: 'AmazonCs', auto_sync: true, last_sync_at: 0 },
    { id: 'WAYFAIR_SOURCE', platform: 'WayfairCs', auto_sync: true, last_sync_at: 0 },
  ];
  c.routes.set('/api/feishu/sources/WAYFAIR_SOURCE/sync', async () => ({}));
  await c.openAftersaleOverview('WayfairCs');
  assert.deepEqual(c.effects.filter(e => e.method === 'POST').map(e => e.path),
    ['/api/feishu/sources/WAYFAIR_SOURCE/sync']);
  assertWayfair(c);
});

test('A slow dataset list cannot reopen the previously selected platform', async () => {
  const c = context(), pending = gate(); let calls = 0;
  c.loadDatasets = async () => { if (++calls === 1) await pending.promise; };
  const old = c.openAftersaleOverview('AmazonCs');
  c.applyPlatform('WayfairCs');
  await c.openAftersaleOverview('WayfairCs');
  pending.resolve(); await old;
  assertWayfair(c);
});

for (const waitAt of ['metadata', 'result', 'report']) {
  test(`A slow ${waitAt} response inside openDataset cannot replace a newer selection`, async () => {
    const c = context(), pending = gate(), entered = gate();
    const endpoint = '/api/datasets/AMAZON_REPORT' + (waitAt === 'metadata' ? '' : '/' + waitAt);
    c.routes.set(endpoint, async () => {
      entered.resolve(); await pending.promise;
      if (waitAt === 'metadata') return { json: async () => c.state.datasets[0] };
      if (waitAt === 'result') return { json: async () => ({ report_type: 'aftersale', platform: 'AmazonCs' }) };
      return { text: async () => 'Stale Amazon report' };
    });
    const old = c.openAftersaleOverview('AmazonCs');
    await entered.promise;
    c.applyPlatform('WayfairCs');
    await c.openAftersaleOverview('WayfairCs');
    pending.resolve(); await old;
    assertWayfair(c);
    assert.equal(c.state.reportMd, 'Report WAYFAIR_REPORT');
  });
}

test('An old request failure cannot replace a newer result with the empty state', async () => {
  const c = context(), pending = gate(); let calls = 0;
  c.loadDatasets = async () => { if (++calls === 1) await pending.promise; };
  const old = c.openAftersaleOverview('AmazonCs');
  c.applyPlatform('WayfairCs'); await c.openAftersaleOverview('WayfairCs');
  pending.reject(new Error('Synthetic late failure')); await old;
  assertWayfair(c);
});

test('Switching to product VOC invalidates an outstanding aftersale request', async () => {
  const c = context(), pending = gate();
  c.loadDatasets = async () => pending.promise;
  const old = c.openAftersaleOverview('AmazonCs');
  c.applyPlatform('Amazon');
  pending.resolve(); await old;
  assert.equal(c.state.platform, 'Amazon');
  assert.equal(c.state.view, 'setup');
  assert.equal(c.state.current, null);
  assert.deepEqual(renders(c), []);
});

test('A stale source response cannot start a synchronization after platform navigation', async () => {
  const c = context(), pending = gate(), entered = gate();
  c.routes.set('/api/feishu/sources', async () => {
    entered.resolve(); await pending.promise;
    return { json: async () => ({ sources: [{ id: 'AMAZON_SOURCE', platform: 'AmazonCs', auto_sync: true }] }) };
  });
  c.routes.set('/api/feishu/sources/AMAZON_SOURCE/sync', async () => ({}));
  const old = c.openAftersaleOverview('AmazonCs'); await entered.promise;
  c.applyPlatform('Amazon');
  pending.resolve(); await old;
  assert.equal(c.effects.filter(e => e.method === 'POST').length, 0);
  assert.equal(c.state.platform, 'Amazon');
});

test('Refreshing the same platform invalidates an older overview generation', async () => {
  const c = context(), pending = gate(), entered = gate(); let calls = 0;
  c.routes.set('/api/feishu/sources', async () => {
    if (++calls === 1) { entered.resolve(); await pending.promise; return { json: async () => ({ sources: [], marker: 'old' }) }; }
    return { json: async () => ({ sources: [], marker: 'new' }) };
  });
  const old = c.openAftersaleOverview('AmazonCs'); await entered.promise;
  await c.openAftersaleOverview('AmazonCs');
  pending.resolve(); await old;
  assert.equal(c.state.feishuCred.marker, 'new');
  assert.deepEqual(renders(c), ['AMAZON_REPORT']);
});

test('An explicit newer openDataset call supersedes an outstanding report request', async () => {
  const c = context(), pending = gate(), entered = gate();
  c.routes.set('/api/datasets/AMAZON_REPORT', async () => {
    entered.resolve(); await pending.promise;
    return { json: async () => c.state.datasets[0] };
  });
  const old = c.openDataset('AMAZON_REPORT'); await entered.promise;
  await c.openDataset('WAYFAIR_REPORT');
  pending.resolve(); await old;
  assertWayfair(c);
});

test('A late job completion cannot reopen a report after platform navigation', async () => {
  const c = context();
  c.state.datasets[0].status = 'running';
  c.state.sources = [{ platform: 'AmazonCs', auto_sync: false, dataset_id: 'AMAZON_REPORT' }];
  await c.openAftersaleOverview('AmazonCs');
  const event = c.events[0]; assert.ok(event);
  c.applyPlatform('WayfairCs'); await c.openAftersaleOverview('WayfairCs');
  event.onmessage({ data: JSON.stringify({ status: 'done', progress: 100 }) });
  await tick();
  assertWayfair(c);
  assert.equal(event.closed, true);
  assert.deepEqual(c.state.files, ['pending.csv']);
});

test('Navigation during the completion refresh invalidates the old SSE continuation', async () => {
  const c = context(), pending = gate(), entered = gate();
  c.state.datasets[0].status = 'running';
  c.state.sources = [{ platform: 'AmazonCs', auto_sync: false, dataset_id: 'AMAZON_REPORT' }];
  await c.openAftersaleOverview('AmazonCs');
  const event = c.events[0];
  c.loadDatasets = async () => { entered.resolve(); await pending.promise; };
  event.onmessage({ data: JSON.stringify({ status: 'done', progress: 100 }) });
  await entered.promise;
  c.loadDatasets = async () => {};
  c.applyPlatform('WayfairCs'); await c.openAftersaleOverview('WayfairCs');
  pending.resolve(); await tick();
  assertWayfair(c);
});

(async () => {
  const results = [];
  for (const t of tests) {
    let timer;
    try {
      await Promise.race([t.body(), new Promise((_, reject) => {
        timer = setTimeout(() => reject(new Error('Offline test did not complete within 3 seconds')), 3000);
      })]);
      results.push({ name: t.name, passed: true });
    }
    catch (e) { results.push({ name: t.name, passed: false, error: e.message }); }
    finally { clearTimeout(timer); }
  }
  const outcome = { source, synthetic_only: true, tests: results, passed: results.filter(t => t.passed).length,
    failed: results.filter(t => !t.passed).length };
  if (process.env.VOC_FRONTEND_EVIDENCE) {
    fs.mkdirSync(path.dirname(process.env.VOC_FRONTEND_EVIDENCE), { recursive: true });
    fs.writeFileSync(process.env.VOC_FRONTEND_EVIDENCE, JSON.stringify(outcome, null, 2));
  }
  console.log(JSON.stringify(outcome, null, 2));
  process.exitCode = outcome.failed ? 1 : 0;
})();
