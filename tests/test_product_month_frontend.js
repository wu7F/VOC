const assert = require('assert');
const board = require('../web/board.js');

let pass = 0, scenarios = 0;
function t(name, fn) {
  scenarios++;
  try { fn(); pass++; console.log('ok', scenarios, name); }
  catch (e) { console.error('fail', scenarios, name, e.message); process.exit(1); }
}

function makeSnapshot() {
  return {
    months: ['2026-04', '2026-05', '2026-06', '2026-07', '2026-08', '2026-09', '2026-10', '2026-11'],
    aftersale: { connected: true, sku_store_month: [], store_month: [], teams: [], total: {} },
    teams: [{
      name: 'Team 1',
      shops: [{
        account: 'a',
        months: {}, units_known: 9999,
        skus: [{ sku: 'S', display_name: '名称', sold: { '2026-04': 9999 } }]
      }]
    }]
  };
}
function add(snap, team, sku, month, n, d) {
  snap.aftersale.sku_store_month.push({ team, sku, month, numerator: n, denominator: d });
}

// aggregateProductMonths

t('aggregate direct S April 10/100 rate 10, input unchanged', () => {
  const snap = makeSnapshot();
  snap.aftersale.sku_store_month = [
    { team: 'Team 1', account: 'a', sku: 'S', month: '2026-04', numerator: 1, denominator: 2 },
    { team: 'Team 1', account: 'b', sku: 'S', month: '2026-04', numerator: 9, denominator: 98 },
    { team: 'Team 2', account: 'c', sku: 'S', month: '2026-04', numerator: 50, denominator: 100 }
  ];
  const before = JSON.parse(JSON.stringify(snap));
  const agg = board.aggregateProductMonths(snap, 'Team 1');
  assert.strictEqual(agg.connected, true);
  assert.strictEqual(agg.items.length, 1);
  assert.strictEqual(agg.items[0].sku, 'S');
  assert.deepStrictEqual(agg.items[0].months['2026-04'], { numerator: 10, denominator: 100, rate: 10 });
  assert.deepStrictEqual(snap, before);
});

t('zero denominator 0 creates point, n1/d0 null, missing month undefined, invalid excluded', () => {
  const snap = makeSnapshot();
  snap.aftersale.sku_store_month = [];
  add(snap, 'Team 1', 'S', '2026-04', 0, 10);
  add(snap, 'Team 1', 'S', '2026-05', 1, 0);
  add(snap, 'Team 1', 'C', '2026-04', undefined, 10);
  add(snap, 'Team 1', 'D', '2026-04', 5, null);
  add(snap, 'Team 1', 'E', '2026-04', '', 5);
  const agg = board.aggregateProductMonths(snap, 'Team 1');
  const s = agg.items.find(i => i.sku === 'S');
  assert.ok(s);
  assert.strictEqual(s.months['2026-04'].rate, 0);
  assert.strictEqual(s.months['2026-05'].rate, null);
  assert.strictEqual(s.months['2026-06'], undefined);
  assert.strictEqual(agg.items.some(i => i.sku === 'C'), false);
  assert.strictEqual(agg.items.some(i => i.sku === 'D'), false);
  assert.strictEqual(agg.items.some(i => i.sku === 'E'), false);
});

t('distinct SKU same name not merged, T01 metadata only not in items, extra names irrelevant', () => {
  const snap = makeSnapshot();
  snap.teams[0].shops[0].skus = [
    { sku: 'S1', display_name: 'Same' },
    { sku: 'S2', display_name: 'Same' },
    { sku: 'T01', display_name: 'Excluded top-level only', sold: { '2026-04': 15 } }
  ];
  snap.aftersale.sku_store_month = [];
  add(snap, 'Team 1', 'S1', '2026-04', 1, 10);
  add(snap, 'Team 1', 'S2', '2026-04', 1, 20);
  const agg = board.aggregateProductMonths(snap, 'Team 1');
  const skus = agg.items.map(i => i.sku).sort();
  assert.deepStrictEqual(skus, ['S1', 'S2']);
  assert.strictEqual(agg.items.some(i => i.sku === 'T01'), false);
  assert.strictEqual(agg.names['S1'], 'Same');
  assert.strictEqual(agg.names['S2'], 'Same');
});

t('out-of-range months excluded', () => {
  const snap = makeSnapshot();
  snap.aftersale.sku_store_month = [];
  add(snap, 'Team 1', 'S', '2026-03', 1, 2);
  add(snap, 'Team 1', 'S', '2026-12', 1, 2);
  add(snap, 'Team 1', 'S', '2026-04', 1, 2);
  const agg = board.aggregateProductMonths(snap, 'Team 1');
  const s = agg.items.find(i => i.sku === 'S');
  assert.ok(s);
  assert.ok(s.months['2026-04']);
  assert.strictEqual(s.months['2026-03'], undefined);
  assert.strictEqual(s.months['2026-12'], undefined);
});

// renderProductMonthsHtml

t('SVG has 8 month labels 4 月..11 月', () => {
  const snap = makeSnapshot();
  snap.aftersale.sku_store_month = [];
  add(snap, 'Team 1', 'S', '2026-04', 10, 100);
  const html = board.renderProductMonthsHtml(snap, { view: 'Team 1' });
  const svg = html.match(/<svg[^>]*>[\s\S]*?<\/svg>/)[0];
  const labels = [...svg.matchAll(/(\d+) 月/g)].map(m => Number(m[1]));
  assert.deepStrictEqual(labels, [4, 5, 6, 7, 8, 9, 10, 11]);
});

t('April+Jun points => two M path moves with gap, not L', () => {
  const snap = makeSnapshot();
  snap.aftersale.sku_store_month = [];
  add(snap, 'Team 1', 'S', '2026-04', 10, 100);
  add(snap, 'Team 1', 'S', '2026-06', 5, 50);
  const html = board.renderProductMonthsHtml(snap, { view: 'Team 1' });
  const svg = html.match(/<svg[^>]*>[\s\S]*?<\/svg>/)[0];
  const path = svg.match(/<path[^>]*\sd="([^"]*)"/);
  assert.ok(path, 'path with d found');
  assert.strictEqual((path[1].match(/M/g) || []).length, 2);
  assert.strictEqual((path[1].match(/L/g) || []).length, 0);
});

t('real zero => circle data-value=0', () => {
  const snap = makeSnapshot();
  snap.aftersale.sku_store_month = [];
  add(snap, 'Team 1', 'S', '2026-04', 0, 10);
  const html = board.renderProductMonthsHtml(snap, { view: 'Team 1' });
  const svg = html.match(/<svg[^>]*>[\s\S]*?<\/svg>/)[0];
  assert.ok(svg.includes('data-value="0"'));
});

t('all gap (n1/d0) => no circles and 无法计算', () => {
  const snap = makeSnapshot();
  snap.teams[0].shops[0].skus = [{ sku: 'G', display_name: 'Gap' }];
  snap.aftersale.sku_store_month = [];
  add(snap, 'Team 1', 'G', '2026-04', 1, 0);
  const html = board.renderProductMonthsHtml(snap, { view: 'Team 1' });
  assert.strictEqual((html.match(/<circle/g) || []).length, 0);
  assert.ok(html.includes('无法计算售后率'));
});

t('hostile names escaped, Team ARIA label', () => {
  const snap = makeSnapshot();
  snap.teams[0].shops[0].skus = [{ sku: 'S', display_name: '<script>alert(1)</script>' }];
  snap.aftersale.sku_store_month = [];
  add(snap, 'Team 1', 'S', '2026-04', 1, 10);
  const html = board.renderProductMonthsHtml(snap, { view: 'Team 1' });
  assert.ok(html.includes('&lt;script&gt;alert(1)&lt;/script&gt;'));
  assert.ok(!html.includes('<script>alert'));
  assert.match(html, /<svg[^>]*aria-label="Team 1 /);
});

t('13 products stable SKU01..13 => page 3 has 1 card, page99 clamps', () => {
  const snap = makeSnapshot();
  snap.teams[0].shops[0].skus = [];
  for (let i = 1; i <= 13; i++) {
    snap.teams[0].shops[0].skus.push({ sku: `SKU${String(i).padStart(2, '0')}`, display_name: `Product ${i}` });
  }
  snap.aftersale.sku_store_month = [];
  for (let i = 1; i <= 13; i++) add(snap, 'Team 1', `SKU${String(i).padStart(2, '0')}`, '2026-04', 1, 10);
  const state = { view: 'Team 1', productPage: 99 };
  const html = board.renderProductMonthsHtml(snap, state);
  const count = (html.match(/class="card product-card"/g) || []).length;
  assert.strictEqual(count, 1);
  assert.strictEqual(state.productPage, 3);
  assert.ok(html.includes('第 3 / 3 页'));
});

t('search SKU13 before pagination, no-match form stays, clear restore', () => {
  const snap = makeSnapshot();
  snap.teams[0].shops[0].skus = [];
  for (let i = 1; i <= 13; i++) {
    snap.teams[0].shops[0].skus.push({ sku: `SKU${String(i).padStart(2, '0')}`, display_name: `Product ${i}` });
  }
  snap.aftersale.sku_store_month = [];
  for (let i = 1; i <= 13; i++) add(snap, 'Team 1', `SKU${String(i).padStart(2, '0')}`, '2026-04', 1, 10);
  const state = { view: 'Team 1', productQuery: 'SKU13' };
  let html = board.renderProductMonthsHtml(snap, state);
  assert.strictEqual((html.match(/class="card product-card"/g) || []).length, 1);
  assert.ok(html.includes('SKU13'));
  assert.ok(html.includes('<form data-bind="product-search">'));
  state.productQuery = 'nonexistent';
  html = board.renderProductMonthsHtml(snap, state);
  assert.strictEqual((html.match(/class="card product-card"/g) || []).length, 0);
  assert.ok(html.includes('<form data-bind="product-search">'));
  assert.ok(html.includes('搜索没有匹配的 SKU，请清除搜索条件后重试'));
  state.productQuery = '';
  html = board.renderProductMonthsHtml(snap, state);
  assert.strictEqual((html.match(/class="card product-card"/g) || []).length, 6);
});

// renderMatrixHtml basic pure checks

t('renderMatrixHtml Independent/Wayfair no KPI/SVG, all product mode 请选择Team', () => {
  const snap = makeSnapshot();
  let html = board.renderMatrixHtml(snap, { view: 'Independent' });
  assert.ok(!html.includes('<svg'));
  assert.ok(!html.includes('KPI'));
  html = board.renderMatrixHtml(snap, { view: 'Wayfair' });
  assert.ok(!html.includes('<svg'));
  assert.ok(!html.includes('KPI'));
  html = board.renderMatrixHtml(snap, { view: 'all', matrixMode: 'product' });
  assert.ok(html.includes('请选择') && html.includes('Team'));
});

t('renderMatrixHtml disconnected has warning no SVG', () => {
  const snap = makeSnapshot();
  snap.aftersale.connected = false;
  const html = board.renderMatrixHtml(snap, { view: 'Team 1', matrixMode: 'product' });
  assert.ok(html.includes('warnbox'));
  assert.ok(!html.includes('<svg'));
});

console.log('synthetic_only pass ' + pass);
process.exit(0);
