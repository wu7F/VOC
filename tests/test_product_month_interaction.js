const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const root = path.resolve(__dirname, '..');
const code = fs.readFileSync(path.join(root, 'web', 'board.js'), 'utf8');

const clone = value => JSON.parse(JSON.stringify(value));
const response = (body, status = 200) => ({
  ok: status < 400,
  status,
  json: async () => clone(body)
});
const flush = () => new Promise(resolve => setImmediate(resolve));

function baseSnapshot() {
  return {
    version: 2,
    ok: true,
    fetched_at: '2026-11-20T10:00:00+08:00',
    last_success_at: '2026-11-20T10:00:00+08:00',
    months: ['2026-04', '2026-05', '2026-06', '2026-07', '2026-08', '2026-09', '2026-10', '2026-11'],
    current_month: '2026-11',
    current_month_partial: true,
    source: { table_name: '合成', row_count: 2, page_count: 1 },
    teams: [
      {
        name: 'Team 1',
        shops: [
          {
            account: 'xingshan_US',
            display_name: 'xingshan_US',
            months: { '2026-04': 1, '2026-05': 0 },
            units_known: 1,
            missing_months: [],
            skus: []
          }
        ]
      },
      {
        name: 'Team 2',
        shops: [
          {
            account: 'other_US',
            display_name: 'other_US',
            months: { '2026-04': 0, '2026-05': 0 },
            units_known: 0,
            missing_months: ['2026-04', '2026-05'],
            skus: []
          }
        ]
      },
      { name: 'Team 3', shops: [] }
    ],
    unmapped: [],
    other_platforms: [],
    aftersale: {
      connected: true,
      realtime: false,
      message: '售后数据来自本地确认快照（非实时），分母可能与销量看板不同。',
      source: 'local_confirmed_snapshot',
      source_as_of: '2026-11-20',
      coverage: { start_month: '2026-04', end_month: '2026-11', team_count: 3, store_count: 8, store_month_count: 50, sku_store_month_count: 497 },
      total: { numerator: 765, denominator: 7188, rate: 10.6427 },
      teams: [
        { name: 'Team 1', numerator: 410, denominator: 4127, rate: 9.9346 },
        { name: 'Team 2', numerator: 291, denominator: 2194, rate: 13.2634 },
        { name: 'Team 3', numerator: 64, denominator: 867, rate: 7.3818 }
      ],
      stores: [
        { account: 'xingshan_US', team: 'Team 1', numerator: 89, denominator: 908, rate: 9.8018 },
        { account: 'other_US', team: 'Team 2', numerator: 0, denominator: 0, rate: 0 }
      ],
      store_month: [
        { account: 'xingshan_US', month: '2026-04', numerator: 10, denominator: 100, rate: 10.0 }
      ],
      sku_store_month: [],
      reasons: { connected: true, items: [] }
    },
    wayfair: { connected: false, message: 'Wayfair 销量尚未接入' },
    diagnostics: [],
    stale: false,
    error: null
  };
}

function makeSnapshot(skuCount) {
  const snap = baseSnapshot();
  const months = snap.months;
  const soldZero = {};
  months.forEach(m => { soldZero[m] = 0; });

  // Team 1 SKUs
  const team1Skus = [];
  for (let i = 1; i <= skuCount; i++) {
    const sku = `SKU${String(i).padStart(2, '0')}`;
    team1Skus.push({
      sku,
      display_name: `产品 ${i}`,
      wf_part_number: `WF-${i}`,
      sold: { ...soldZero }
    });
  }
  snap.teams[0].shops[0].skus = team1Skus;

  // Team 2 single SKU
  snap.teams[1].shops[0].skus = [
    {
      sku: 'SKU20',
      display_name: '产品 20',
      wf_part_number: 'WF-20',
      sold: { ...soldZero }
    }
  ];

  // aftersale.sku_store_month rows
  const rows = [];
  for (let i = 1; i <= skuCount; i++) {
    rows.push({
      account: 'xingshan_US',
      month: '2026-04',
      sku: `SKU${String(i).padStart(2, '0')}`,
      numerator: 5,
      denominator: 50,
      rate: 10.0,
      team: 'Team 1'
    });
  }
  rows.push({
    account: 'other_US',
    month: '2026-04',
    sku: 'SKU20',
    numerator: 3,
    denominator: 30,
    rate: 10.0,
    team: 'Team 2'
  });
  snap.aftersale.sku_store_month = rows;

  return snap;
}

async function createBoard(initialQueue = []) {
  const elements = {};
  for (const key of ['nav', 'main', 'banner', 'stale-banner']) {
    elements[key] = { innerHTML: '', querySelectorAll: () => [] };
  }
  const handlers = {};
  const fetchLog = [];
  const queue = initialQueue.length ? initialQueue.slice() : [response(baseSnapshot())];
  const context = {
    document: {
      hidden: true,
      getElementById: id => elements[id],
      addEventListener: (name, handler) => { handlers[name] = handler; }
    },
    fetch: (url) => {
      fetchLog.push(url);
      return Promise.resolve(queue.shift());
    },
    setInterval: () => {},
    module: { exports: {} }
  };
  vm.runInNewContext(code, context);
  await flush();
  const click = async attrs => {
    handlers.click({ target: { closest: () => ({ getAttribute: key => attrs[key] || null }) } });
    await flush();
  };
  const submit = async (value) => {
    const form = {
      querySelector: (sel) => {
        if (sel === '[data-bind="product-query"]') return { value };
        return null;
      }
    };
    handlers.submit({ target: { closest: () => form }, preventDefault: () => {} });
    await flush();
  };
  return { elements, queue, click, submit, handlers, api: context.module.exports, fetchLog };
}

(async () => {
  let passed = 0;
  try {
    // Initial snapshot with 13 Team1 SKUs + 1 Team2 SKU
    const snapshot13 = makeSnapshot(13);
    const board = await createBoard([response(snapshot13)]);

    // Initial store matrix
    assert(board.elements.main.innerHTML.includes('data-bind="matrix-cell"'), 'Initial view should retain store matrix');
    passed++;

    // Switch to Team 1 view, then product matrix mode
    await board.click({ 'data-bind': 'nav', 'data-view': 'Team 1' });
    await board.click({ 'data-bind': 'matrix-mode', 'data-mode': 'product' });
    let products = board.elements.main.innerHTML;
    for (let i = 1; i <= 6; i++) {
      assert(products.includes(`SKU${String(i).padStart(2, '0')}`), `Page 1 should show SKU${String(i).padStart(2, '0')}`);
    }
    assert(!products.includes('SKU07'), 'Page 1 should not show SKU07');
    passed++;

    // Next page
    await board.click({ 'data-bind': 'product-page', 'data-delta': '1' });
    products = board.elements.main.innerHTML;
    for (let i = 7; i <= 12; i++) {
      assert(products.includes(`SKU${String(i).padStart(2, '0')}`), `Page 2 should show SKU${String(i).padStart(2, '0')}`);
    }
    assert(!products.includes('SKU13'), 'Page 2 should not show SKU13');
    passed++;

    // Next page (last)
    await board.click({ 'data-bind': 'product-page', 'data-delta': '1' });
    products = board.elements.main.innerHTML;
    assert(products.includes('SKU13'), 'Page 3 should show SKU13');
    assert(!products.includes('SKU12'), 'Page 3 should not show SKU12');
    passed++;

    // Previous page
    await board.click({ 'data-bind': 'product-page', 'data-delta': '-1' });
    products = board.elements.main.innerHTML;
    assert(products.includes('SKU07') && products.includes('SKU12'), 'Prev from page3 should return to page2');
    passed++;

    // Product search: match one SKU
    await board.submit('SKU13');
    products = board.elements.main.innerHTML;
    assert(products.includes('SKU13'), 'Search SKU13 should show that SKU');
    assert(!products.includes('SKU01'), 'Search SKU13 should not show SKU01');
    passed++;

    // Zero-match keeps form and shows empty state
    await board.submit('nonexistent');
    products = board.elements.main.innerHTML;
    assert.strictEqual((products.match(/class="card product-card"/g) || []).length, 0, 'No cards for zero matches');
    assert(products.includes('data-bind="product-search"'), 'Search form should remain');
    passed++;

    // Clearing query restores page1 with 6 SKUs
    await board.submit('');
    products = board.elements.main.innerHTML;
    for (let i = 1; i <= 6; i++) {
      assert(products.includes(`SKU${String(i).padStart(2, '0')}`), `Clear search should restore SKU${String(i).padStart(2, '0')}`);
    }
    passed++;

    // Team2 resets query/page, preserves product mode
    await board.submit('SKU13');
    await board.click({ 'data-bind': 'nav', 'data-view': 'Team 2' });
    products = board.elements.main.innerHTML;
    assert(products.includes('SKU20'), 'Team2 should show SKU20');
    assert(!products.includes('SKU01'), 'Team2 should not show Team1 SKUs');
    assert(products.includes('value=""') && products.includes('第 1 / 1 页'), 'Team switch clears query and page');
    passed++;

    // Placeholder navigation must not display Amazon charts or product controls.
    await board.click({ 'data-bind': 'nav', 'data-view': 'Independent' });
    products = board.elements.main.innerHTML;
    assert(products.includes('未接入') && !products.includes('<svg') && !products.includes('product-card'));
    // Back to Team1, product mode retained.
    await board.click({ 'data-bind': 'nav', 'data-view': 'Team 1' });
    products = board.elements.main.innerHTML;
    assert(products.includes('SKU01') && products.includes('SKU06'), 'Team1 product matrix page1 restored');
    assert(products.includes('<svg'), 'Product view should contain actual SVG charts');
    passed++;

    // Query preserve on successful refresh
    await board.submit('SKU01');
    const snapshot13Clone = clone(snapshot13);
    board.queue.push(response(snapshot13Clone));
    await board.click({ 'data-bind': 'action', 'data-action': 'refresh' });
    products = board.elements.main.innerHTML;
    assert(products.includes('SKU01') && !products.includes('SKU02'), 'After refresh query should persist');
    assert(board.elements.main.innerHTML.includes('Team 1'), 'Team preserved');
    assert(board.elements.main.innerHTML.includes('product-card'), 'Mode preserved');
    passed++;

    // Page shrink on successful refresh with empty query
    await board.submit(''); // clear query
    await board.click({ 'data-bind': 'product-page', 'data-delta': '1' });
    await board.click({ 'data-bind': 'product-page', 'data-delta': '1' }); // page3 (1 card)
    products = board.elements.main.innerHTML;
    assert(products.includes('SKU13'), 'Before shrink page3 shows SKU13');
    const snapshot8 = makeSnapshot(8);
    board.queue.push(response(snapshot8));
    await board.click({ 'data-bind': 'action', 'data-action': 'refresh' });
    products = board.elements.main.innerHTML;
    assert(products.includes('SKU07') && products.includes('SKU08'), 'After shrink page should clamp to page2 showing SKU07 and SKU08');
    assert(!products.includes('SKU13'), 'SKU13 should disappear after shrink');
    assert(products.includes('第 2 / 2 页'), 'Page indicator should show page 2 of 2');
    const cardMatches = products.match(/class="card product-card"/g) || [];
    assert.strictEqual(cardMatches.length, 2, 'Should show exactly 2 cards after shrink');
    passed++;

    // Stay in PRODUCT mode after shrink for hover tests
    const rateTip = { style: {}, innerHTML: '' };
    const salesTip = { style: {}, innerHTML: '' };
    const rateChart = {
      getAttribute: (attr) => attr === 'data-chart' ? 'rate' : null,
      querySelector: (sel) => sel === '.tip' ? rateTip : null,
      getBoundingClientRect: () => ({ left: 0, top: 0 })
    };
    const salesChart = {
      getAttribute: (attr) => attr === 'data-chart' ? 'sku' : null,
      querySelector: (sel) => sel === '.tip' ? salesTip : null,
      getBoundingClientRect: () => ({ left: 0, top: 0 })
    };
    const rateCallbacks = [];
    const salesCallbacks = [];
    const rateCircle = {
      getAttribute: (name) => {
        if (name === 'data-chart') return 'rate';
        if (name === 'data-series') return 'SYNTHETIC';
        if (name === 'data-month') return '2026-04';
        if (name === 'data-value') return '10';
        return null;
      },
      addEventListener: (event, cb) => rateCallbacks.push({ event, cb }),
      closest: (sel) => rateChart
    };
    const salesCircle = {
      getAttribute: (name) => {
        if (name === 'data-chart') return 'sku';
        if (name === 'data-series') return 'SYNTHETIC';
        if (name === 'data-month') return '2026-04';
        if (name === 'data-value') return '10';
        return null;
      },
      addEventListener: (event, cb) => salesCallbacks.push({ event, cb }),
      closest: (sel) => salesChart
    };
    board.elements.main.querySelectorAll = (selector) => {
      if (selector === '[data-chart] circle') return [rateCircle, salesCircle];
      return [];
    };

    // Trigger actual render via nav Team 1 (same view still renders)
    await board.click({ 'data-bind': 'nav', 'data-view': 'Team 1' });
    await flush();

    // Invoke mouseenter on each circle and assert tips
    const rateEnter = rateCallbacks.find(c => c.event === 'mouseenter');
    const salesEnter = salesCallbacks.find(c => c.event === 'mouseenter');
    assert(rateEnter && salesEnter, 'Both mouseenter handlers should be attached');
    rateEnter.cb();
    assert.strictEqual(rateTip.style.display, 'block', 'Rate tip should be shown');
    assert(rateTip.innerHTML.includes('10.00%'), 'Rate tip should be formatted as percentage');
    salesEnter.cb();
    assert.strictEqual(salesTip.style.display, 'block', 'Sales tip should be shown');
    assert(salesTip.innerHTML.includes('10') && !salesTip.innerHTML.includes('%'), 'Sales tip should show raw value without percentage');
    passed++;

    // Failed refresh: preserve product cards and SVG in stale state
    const beforeFailure = board.elements.main.innerHTML;
    board.queue.push(response({ error: '服务器错误' }, 500));
    await board.click({ 'data-bind': 'action', 'data-action': 'refresh' });
    assert.strictEqual(board.elements.main.innerHTML, beforeFailure, 'HTML should remain unchanged after failed refresh');
    assert(beforeFailure.includes('product-card'), 'Product cards should remain after failed refresh');
    assert(beforeFailure.includes('<svg') || beforeFailure.includes('<circle'), 'SVG elements should remain after failed refresh');
    assert(board.elements['stale-banner'].innerHTML.length > 0, 'Stale banner should be visible');
    passed++;

    assert(board.fetchLog.every(url => url === '/api/bitable/board' || url === '/api/bitable/board?refresh=1'), 'Only existing read endpoint is used');
    console.log(JSON.stringify({ synthetic_only: true, passed }));
  } catch (err) {
    console.error(err);
    process.exitCode = 1;
  }
})();
