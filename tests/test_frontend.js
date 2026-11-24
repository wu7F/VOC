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
  section('function esc(', 'function toast('),
  section('function renderPlatformGroups(', 'function syncGroupFromPlatform('),
  section('function syncGroupFromPlatform(', 'function applyPlatform('),
  section('function applyPlatform(', 'function applyPlatformTexts('),
  section('function applyPlatformTexts(', '/* ============================ 分析引擎'),
  section('function hexA(', 'async function loadDatasets('),
  section('async function loadDatasets(', '/* 记录列表：'),
  section('function updateGroupUI(', 'async function openAftersaleSummary('),
  section('function watchJob(', 'function bindResultEvents()'),
  section('async function openDataset(', 'function renderResult()'),
  section('async function boot(', '/* ---------------- 记住密码'),
  section('function onHashChange()', 'function renderPlatformGroups()'),
  section('function showLogin(', 'function hideLogin('),
  section('async function doLogout(', 'function bindLogin('),
  section('function handleAuthFailure(', 'function emptyVoiceFilter('),
].join('\n');
const initialState = section('const state = {', '/* ============================ 登录与权限');

function gate() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
const platforms = [
  { id: 'AmazonCs', color: '#446688', label_cn: 'Amazon售后', kind: 'aftersale', group: '售后VOC' },
  { id: 'WayfairCs', color: '#446688', label_cn: 'Wayfair售后', kind: 'aftersale', group: '售后VOC' },
  { id: 'Amazon', color: '#446688', label_cn: 'Amazon', kind: 'ecommerce', group: '产品VOC' },
];

function context() {
  const state = vm.runInNewContext(initialState + '\nstate;');
  Object.assign(state, { platforms, platform: 'AmazonCs', group: '售后VOC', datasets: [
    { id: 'AMAZON_REPORT', platform: 'AmazonCs', status: 'done', created_at: '2026-11-10' },
    { id: 'WAYFAIR_REPORT', platform: 'WayfairCs', status: 'done', created_at: '2026-11-10' },
  ], sources: [], current: null, result: null, reportMd: '', files: ['pending.csv'], tab: 'overview',
    build: 0, viewRequest: 0 });

  const effects = [], events = [], elements = new Map();
  const routes = new Map();
  const buttons = new Map();
  const attrCalls = new Map();
  const history = [];
  const bodyClasses = new Set();
  const rootStyle = {};
  const timers = [];

  function element(selector) {
    if (!elements.has(selector)) {
      const el = {
        style: {}, hidden: true, textContent: '', className: '', value: '',
        attributes: {},
        classList: {
          _classes: new Set(),
          add(...cls) { cls.forEach(c => this._classes.add(c)); },
          remove(...cls) { cls.forEach(c => this._classes.delete(c)); },
          toggle(cls, force) {
            if (force === undefined) {
              if (this._classes.has(cls)) { this._classes.delete(cls); return false; }
              this._classes.add(cls); return true;
            }
            if (force) this._classes.add(cls); else this._classes.delete(cls);
            return !!force;
          },
          contains(cls) { return this._classes.has(cls); },
        },
        getAttribute(name) { return this.attributes[name] ?? null; },
        setAttribute(name, value) {
          this.attributes[name] = value;
          attrCalls.set(selector + ':' + name, (attrCalls.get(selector + ':' + name) || 0) + 1);
        },
        removeAttribute(name) {
          delete this.attributes[name];
          attrCalls.set(selector + ':' + name + ':remove', (attrCalls.get(selector + ':' + name + ':remove') || 0) + 1);
        },
      };
      if (selector === '#platformTabs' || selector === '#platformGroups') {
        const key = selector === '#platformTabs' ? 'p' : 'g';
        Object.defineProperty(el, 'innerHTML', {
          get: () => el._html || '',
          set: value => {
            el._html = value;
            el._buttons = [...value.matchAll(new RegExp('data-' + key + '="([^"]+)"', 'g'))]
              .map(match => ({ dataset: { [key]: match[1] }, classList: { toggle() {}, add() {}, remove() {}, contains() { return false; } } }));
            buttons.set(selector, el._buttons);
          },
        });
      }
      elements.set(selector, el);
    }
    return elements.get(selector);
  }

  const c = {
    state, effects, events, Date, console, routes, history,
    document: {
      documentElement: { style: { setProperty: (k, v) => { rootStyle[k] = v; }, getPropertyValue: (k) => rootStyle[k] || '' } },
      body: {
        classList: {
          add(cls) { bodyClasses.add(cls); },
          remove(cls) { bodyClasses.delete(cls); },
          toggle(cls, force) {
            if (force === undefined) {
              if (bodyClasses.has(cls)) { bodyClasses.delete(cls); return false; }
              bodyClasses.add(cls); return true;
            }
            if (force) bodyClasses.add(cls); else bodyClasses.delete(cls);
            return !!force;
          },
          contains(cls) { return bodyClasses.has(cls); },
        },
      },
    },
    location: { hash: '', pathname: '/', search: '', get href() { return this.pathname + this.search + this.hash; } },
    history: {
      replaceState(stateObj, title, url) {
        history.push({ stateObj, title, url });
        if (url?.startsWith('#')) c.location.hash = url;
        else {
          const u = new URL(url, 'http://localhost');
          c.location.pathname = u.pathname;
          c.location.search = u.search;
          c.location.hash = u.hash;
        }
      },
    },
    URLSearchParams: global.URLSearchParams,
    api: async (p, options) => {
      effects.push({ path: p, method: options?.method || 'GET', body: options?.body });
      if (routes.has(p)) return routes.get(p)(options);
      if (p === '/api/datasets') return { json: async () => ({ datasets: state.datasets }) };
      if (p === '/api/platforms') return { json: async () => ({ platforms }) };
      if (p === '/api/health') return { json: async () => ({ build: 42 }) };
      const [, id, suffix] = p.match(/^\/api\/datasets\/([^/]+)(?:\/(result|report))?$/) || [];
      const rec = state.datasets.find(row => row.id === id);
      assert.ok(rec, `Unexpected mock path ${p}`);
      if (suffix === 'result') return { json: async () => ({ report_type: 'aftersale', platform: rec.platform }) };
      if (suffix === 'report') return { text: async () => `Report ${id}` };
      return { json: async () => rec };
    },
    fetch: async (url, options) => {
      effects.push({ path: typeof url === 'string' ? url : url.url, method: options?.method || 'GET', body: options?.body });
      if (routes.has(typeof url === 'string' ? url : url.pathname)) {
        return routes.get(typeof url === 'string' ? url : url.pathname)(options);
      }
      if (typeof url === 'string' && url === '/api/auth/logout') return { ok: true };
      return { ok: true };
    },
    $: element,
    $$: selector => {
      if (selector === '#platformTabs button') return buttons.get('#platformTabs') || [];
      if (selector === '#platformGroups button') return buttons.get('#platformGroups') || [];
      return [];
    },
    renderAftersale: () => effects.push({ rendered: state.current.id, resultPlatform: state.result.platform }),
    renderResult: () => effects.push({ rendered: state.current.id, resultPlatform: state.result.platform }),
    EventSource: class {
      constructor(url) { this.url = url; this.closed = false; events.push(this); }
      close() { this.closed = true; }
    },
    setTimeout: (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
    clearTimeout: () => {},
    destroyCharts: () => effects.push({ destroyCharts: true }),
    endBoot: () => effects.push({ endBoot: true }),
    setLoginError: () => effects.push({ setLoginError: true }),
    clearLoginError: () => effects.push({ clearLoginError: true }),
    prefillRemembered: () => effects.push({ prefillRemembered: true }),
  };

  for (const fn of ['renderDemos', 'renderDatasets', 'resizeAllCharts', 'renderFileList',
    'updateStartState', 'toast', 'bindAftersaleUI', 'bindSetupEvents', 'bindResultEvents',
    'bindListToolbar', 'bindRecordsBoard', 'loadEngine', 'probePdfEngine', 'probeMaintenance', 'applyRoleUI',
    'renderRecordsBoard', 'switchTab']) c[fn] = () => {};

  c.showError = () => effects.push({ showError: true });

  vm.createContext(c);
  vm.runInContext(functions, c);

  // Track attr calls for boardFrame src
  c._getAttrCall = (name = 'src') => attrCalls.get('#boardFrame:' + name) || 0;
  c._getRemoveCall = (name = 'src') => attrCalls.get('#boardFrame:' + name + ':remove') || 0;

  return c;
}

function renders(c) { return c.effects.filter(e => e.rendered).map(e => e.rendered); }
function frameSrc(c) { return c.$('#boardFrame').getAttribute('src'); }
function assertBoard(c, platform = 'AmazonCs') {
  assert.equal(c.state.platform, platform);
  assert.equal(c.state.view, 'board');
  assert.equal(c.$('#viewBoard').hidden, false);
  assert.equal(c.$('#viewSetup').hidden, true);
  assert.equal(frameSrc(c), '/board');
  assert.equal(c.document.body.classList.contains('board-mode'), true);
}
function assertProduct(c, platform = 'Amazon') {
  assert.equal(c.state.platform, platform);
  assert.equal(c.state.view, 'setup');
  assert.equal(c.$('#viewBoard').hidden, true);
  assert.equal(c.$('#viewSetup').hidden, false);
  assert.equal(c.document.body.classList.contains('board-mode'), false);
}
function assertWayfair(c) {
  assert.equal(c.state.platform, 'WayfairCs');
  assert.equal(c.state.current?.id, 'WAYFAIR_REPORT');
  assert.equal(c.state.result?.platform, 'WayfairCs');
  assert.equal(c.state.view, 'result');
  assert.deepEqual(renders(c), ['WAYFAIR_REPORT']);
}

const tests = [];
function test(name, body) { tests.push({ name, body }); }

// ===== Boot and health =====
test('boot calls /api/health and applies build', async () => {
  const c = context();
  c.state.platform = 'Amazon';
  c.state.group = '产品VOC';
  await c.boot();
  assert.equal(c.state.build, 42);
  assert.ok(c.effects.some(e => e.path === '/api/health'));
});

test('product-only boot does not load iframe', async () => {
  const c = context();
  c.state.platform = 'Amazon';
  c.state.group = '产品VOC';
  await c.boot();
  assertProduct(c);
  assert.equal(frameSrc(c), null);
  assert.equal(c._getAttrCall(), 0);
  assert.equal(c.effects.filter(e => e.path === '/board').length, 0);
});

test('aftersale boot loads board iframe', async () => {
  const c = context();
  await c.boot();
  assertBoard(c);
  assert.ok(c._getAttrCall() >= 1);
});

// ===== Platform/group navigation =====
test('programmatic applyPlatform shows board and loads /board once', async () => {
  const c = context();
  c.state.platform = 'Amazon';
  c.state.group = '产品VOC';
  const before = c._getAttrCall();
  c.applyPlatform('AmazonCs');
  assertBoard(c);
  assert.equal(frameSrc(c), '/board');
  assert.ok(c._getAttrCall() > before);
  const mid = c._getAttrCall();
  c.applyPlatform('AmazonCs');
  assertBoard(c);
  assert.equal(c._getAttrCall(), mid); // no reload
});

test('group navigation to aftersale shows board', async () => {
  const c = context();
  c.state.platform = 'Amazon';
  c.state.group = '产品VOC';
  c.renderPlatformGroups();
  const btn = c.$$('#platformGroups button').find(b => b.dataset.g === '售后VOC');
  assert.ok(btn, 'aftersale group button must render');
  btn.onclick();
  await Promise.resolve();
  assertBoard(c);
  assert.equal(c.effects.filter(e => e.path === '/api/datasets').length, 0); // group nav no fetch
  assert.equal(c.effects.filter(e => e.path === '/api/feishu/sources').length, 0);
});

test('return to product restores setup and preserves draft', async () => {
  const c = context();
  c.state.files = ['draft.csv'];
  c.$('#fName').value = 'draft name';
  c.applyPlatform('AmazonCs');
  assertBoard(c);
  c.applyPlatform('Amazon');
  assertProduct(c);
  assert.deepEqual(c.state.files, ['draft.csv']);
  assert.equal(c.$('#fName').value, 'draft name');
  assert.equal(c.$('#viewBoard').hidden, true);
  assert.equal(c.$('#viewSetup').hidden, false);
  assert.equal(frameSrc(c), '/board'); // frame remains loaded
});

test('old config DOM is hidden in board mode', async () => {
  const c = context();
  c.$('#btnAsSummary').hidden = false;
  c.$('#btnNew').hidden = false;
  c.$('#cardUpload').hidden = false;
  c.$('#cardConfig').hidden = false;
  c.$('#demoCard').hidden = false;
  c.$('#btnStart').hidden = false;
  c.state.group = '售后VOC';
  c.updateGroupUI();
  assert.equal(c.$('#btnAsSummary').hidden, false);
  assert.equal(c.$('#btnNew').hidden, true);
  assert.equal(c.$('#cardUpload').hidden, true);
  assert.equal(c.$('#cardConfig').hidden, true);
  assert.equal(c.$('#demoCard').hidden, true);
  assert.equal(c.$('#btnStart').hidden, true);
  c.applyPlatform('AmazonCs');
  assertBoard(c);
});

test('group navigation does not fetch datasets', async () => {
  const c = context();
  c.state.platform = 'Amazon';
  c.state.group = '产品VOC';
  c.renderPlatformGroups();
  c.$$('#platformGroups button').find(b => b.dataset.g === '售后VOC').onclick();
  await Promise.resolve();
  assert.equal(c.effects.filter(e => e.path === '/api/datasets').length, 0);
  assert.equal(c.effects.filter(e => e.path === '/api/feishu/sources').length, 0);
  assert.equal(c.effects.filter(e => /\/sync$/.test(e.path)).length, 0);
  assert.equal(c.effects.filter(e => e.path === '/api/config').length, 0);
});

// ===== Async race protection =====
for (const waitAt of ['metadata', 'result', 'report']) {
  test(`slow ${waitAt} response cannot hijack board`, async () => {
    const c = context(), pending = gate(), entered = gate();
    const endpoint = '/api/datasets/AMAZON_REPORT' + (waitAt === 'metadata' ? '' : '/' + waitAt);
    c.routes.set(endpoint, async () => {
      entered.resolve(); await pending.promise;
      if (waitAt === 'metadata') return { json: async () => c.state.datasets[0] };
      if (waitAt === 'result') return { json: async () => ({ report_type: 'aftersale', platform: 'AmazonCs' }) };
      return { text: async () => 'Stale Amazon report' };
    });
    const old = c.openDataset('AMAZON_REPORT');
    await entered.promise;
    c.applyPlatform('WayfairCs');
    pending.resolve();
    await old;
    assertBoard(c, 'WayfairCs');
    assert.equal(c.state.current, null);
  });
}

test('late SSE completion cannot hijack board', async () => {
  const c = context();
  c.state.datasets[0].status = 'running';
  await c.openDataset('AMAZON_REPORT');
  const event = c.events[0];
  assert.ok(event);
  c.applyPlatform('AmazonCs');
  assertBoard(c);
  const before = renders(c).length;
  event.onmessage({ data: JSON.stringify({ status: 'done', progress: 100 }) });
  assertBoard(c);
  assert.equal(renders(c).length, before);
  assert.equal(event.closed, true);
});

// ===== Hash behavior =====
test('programmatic aftersale writes #board', async () => {
  const c = context();
  c.state.platform = 'Amazon';
  c.state.group = '产品VOC';
  c.applyPlatform('AmazonCs');
  assert.equal(c.location.hash, '#board');
});

test('product navigation clears #board', async () => {
  const c = context();
  c.location.hash = '#board';
  c.applyPlatform('Amazon');
  assert.equal(c.location.hash, '');
});

test('deep-link #ds opens historical report', async () => {
  const c = context();
  c.location.hash = '#ds=WAYFAIR_REPORT';
  await c.boot();
  assertWayfair(c);
  assert.equal(c.location.hash, '#ds=WAYFAIR_REPORT');
});

test('deep-link #board opens board', async () => {
  const c = context();
  c.location.hash = '#board';
  await c.boot();
  assertBoard(c);
});

test('initial boot #records opens records', async () => {
  const c = context();
  c.location.hash = '#records';
  await c.boot();
  assert.equal(c.state.view, 'records');
});

test('onHashChange #board routes to board', async () => {
  const c = context();
  c.state.platform = 'Amazon';
  c.state.group = '产品VOC';
  c.location.hash = '#board';
  c.onHashChange();
  await Promise.resolve();
  assertBoard(c);
});

test('hash beginning board-but-not-exact does not route board', async () => {
  const c = context();
  c.applyPlatform('Amazon');
  c.location.hash = '#boardroom';
  c.onHashChange();
  await Promise.resolve();
  assertProduct(c);
});

test('explicit Wayfair report opening works', async () => {
  const c = context();
  await c.openDataset('WAYFAIR_REPORT');
  assertWayfair(c);
});

// ===== Logout/auth failure =====
test('logout removes board frame and invalidates pending requests', async () => {
  const c = context();
  c.applyPlatform('AmazonCs');
  assertBoard(c);
  c.state.email = 'user@test.com';
  c.routes.set('/api/auth/logout', async () => ({ ok: true }));
  await c.doLogout();
  assert.equal(c.state.email, null);
  assert.equal(c.document.body.classList.contains('locked'), true);
  assert.equal(c.$('#loginScreen').hidden, false);
  assert.equal(c.$('#viewBoard').hidden, true);
  assert.equal(c.document.body.classList.contains('board-mode'), false);
  assert.equal(frameSrc(c), null);
  assert.ok(c._getRemoveCall() >= 1);
  assert.ok(c.effects.some(e => e.endBoot));
  assert.ok(c.effects.some(e => e.setLoginError));
  assert.ok(c.effects.some(e => e.prefillRemembered));
});

test('401 auth failure shows login and clears board', async () => {
  const c = context();
  c.applyPlatform('AmazonCs');
  const handled = c.handleAuthFailure(401);
  assert.equal(handled, true);
  assert.equal(c.state.email, null);
  assert.equal(c.document.body.classList.contains('locked'), true);
  assert.equal(c.$('#loginScreen').hidden, false);
  assert.equal(c.$('#viewBoard').hidden, true);
  assert.equal(frameSrc(c), null);
  assert.ok(c.effects.some(e => e.setLoginError));
});

// ===== Progress/error =====
test('running dataset enters progress view', async () => {
  const c = context();
  c.state.datasets[0].status = 'running';
  await c.openDataset('AMAZON_REPORT');
  assert.equal(c.state.view, 'progress');
  assert.equal(c.events.length, 1);
  assert.equal(c.events[0].url, '/api/datasets/AMAZON_REPORT/events');
});

test('queued dataset enters progress but no report render', async () => {
  const c = context();
  c.state.datasets[0].status = 'queued';
  await c.openDataset('AMAZON_REPORT');
  assert.equal(c.state.view, 'progress');
  assert.equal(renders(c).length, 0);
});

test('error dataset shows setup with message', async () => {
  const c = context();
  c.state.datasets[0].status = 'error';
  c.state.datasets[0].error = 'Bad file';
  await c.openDataset('AMAZON_REPORT');
  assert.equal(c.state.view, 'setup');
  assert.equal(c.$('#runNote').textContent, '上次分析失败');
  assert.ok(c.effects.some(e => e.showError));
});

// ===== Delayed response coverage =====
test('delayed metadata then result/report still opens', async () => {
  const c = context();
  const metaGate = gate();
  c.routes.set('/api/datasets/AMAZON_REPORT', async () => {
    await metaGate.promise;
    return { json: async () => c.state.datasets[0] };
  });
  const p = c.openDataset('AMAZON_REPORT');
  metaGate.resolve();
  await p;
  assert.equal(c.state.current.id, 'AMAZON_REPORT');
  assert.equal(c.state.view, 'result');
  assert.equal(renders(c).length, 1);
});

test('delayed report response eventually renders', async () => {
  const c = context();
  const reportGate = gate();
  c.routes.set('/api/datasets/AMAZON_REPORT/report', async () => {
    await reportGate.promise;
    return { text: async () => 'Slow report' };
  });
  const p = c.openDataset('AMAZON_REPORT');
  reportGate.resolve();
  await p;
  assert.equal(c.state.reportMd, 'Slow report');
  assert.equal(c.state.view, 'result');
});

// ===== Static HTML assertions =====
test('HTML removes old config DOM and board frame is lazy', () => {
  const htmlFile = path.join(source, 'web/index.html');
  const html = fs.readFileSync(htmlFile, 'utf8');
  assert.ok(!html.includes('id="cardFeishu"'), 'cardFeishu should be removed');
  assert.ok(!/id="fs[A-Za-z]*(Credential|Source|Sales)/.test(html), 'fs-prefixed credential/source/sales inputs should be removed');
  assert.ok(!/id="btnFs/.test(html), 'btnFs buttons should be removed');
  const boardMatch = html.match(/<iframe[^>]*id="boardFrame"[^>]*>/);
  assert.ok(boardMatch, 'boardFrame iframe missing');
  assert.ok(/title="[^"]+"/.test(boardMatch[0]), 'boardFrame iframe needs accessible title');
  assert.ok(!/\ssrc=/.test(boardMatch[0]), 'boardFrame iframe must not have initial src');
});

// ===== CSS assertions (static) =====
test('CSS hides sidebar and platform tabs but not topbar/group', () => {
  const cssFile = path.join(source, 'web/style.css');
  assert.ok(fs.existsSync(cssFile), 'style.css missing');
  const css = fs.readFileSync(cssFile, 'utf8');
  assert.ok(css.includes('.board-mode .sidebar') || css.includes('board-mode .sidebar'), 'sidebar hide missing');
  assert.ok(css.includes('.board-mode .platform-tabs') || css.includes('board-mode .platform-tabs'), 'platform tabs hide missing');
  assert.ok(!css.includes('.board-mode .platform-groups'), 'platform groups should not be hidden');
  assert.ok(!css.includes('.board-mode .topbar'), 'topbar should not be hidden');
});

test('topbar and platform groups remain in HTML', () => {
  const htmlFile = path.join(source, 'web/index.html');
  const html = fs.readFileSync(htmlFile, 'utf8');
  assert.ok(html.includes('topbar'), 'topbar missing');
  assert.ok(html.includes('platformGroups'), 'platformGroups missing');
});

// ===== Group subtab rerender =====
test('programmatic applyPlatform rerenders platform subtabs', async () => {
  const c = context();
  c.state.platform = 'Amazon';
  c.state.group = '产品VOC';
  c.renderPlatformTabs();
  assert.deepEqual(c.$$('#platformTabs button').map(b => b.dataset.p), ['Amazon']);
  c.applyPlatform('AmazonCs');
  assert.deepEqual(c.$$('#platformTabs button').map(b => b.dataset.p), ['AmazonCs', 'WayfairCs']);
});

// ===== Product draft preservation =====
test('product upload draft preserved through aftersale roundtrip', async () => {
  const c = context();
  c.state.platform = 'Amazon';
  c.state.group = '产品VOC';
  c.state.files = ['pending.csv'];
  c.$('#fName').value = 'draft analysis name';
  c.$('#fCategory').value = 'Babyletto';
  c.applyPlatform('AmazonCs');
  assertBoard(c);
  c.applyPlatform('Amazon');
  assertProduct(c);
  assert.deepEqual(c.state.files, ['pending.csv']);
  assert.equal(c.$('#fName').value, 'draft analysis name');
  assert.equal(c.$('#fCategory').value, 'Babyletto');
});

// ===== Run all =====
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