const assert = require('node:assert/strict');
const board = require('../web/board.js');
let passed = 0;
function test(name, fn) { fn(); passed++; console.log('ok', name); }

const sliceCount = html => (html.match(/<path\b/g) || []).length;
const flat = (category, count, share, extras = {}) => ({ account: 'a', sku: 's', month: '2026-04', category, count, share, ...extras });
const legacy = (category, count, share, extras = {}) => ({ account: 'a', sku: 's', month: '2026-04', category, count, share, ...extras });
function snap(items, fieldItems, subcategoryItems) {
  const reasons = { connected: true, items, subcategory_items: subcategoryItems || [] };
  if (fieldItems !== undefined) reasons.field_items = fieldItems;
  return { aftersale: { connected: true, reasons } };
}

function detailSnap(reasons) {
  return { months: ['2026-04'], current_month: '2026-04',
    teams: [{ name: 'Team 1', shops: [{ account: 'a', months: { '2026-04': 10 },
      skus: [{ sku: 's', sold: { '2026-04': 10 } }] }] }],
    aftersale: { connected: true, reasons } };
}

function detailReasons() {
  return { connected: true,
    items: [legacy('破损损坏', 1, 100)],
    subcategory_items: [{ account: 'a', sku: 's', month: '2026-04', category: '破损损坏', subcategory: '包装破损', count: 1, share: 100 }],
    field_items: [flat('包装破损', 1, 50), flat('铁架变形', 1, 50)] };
}

test('only flat in Apr renders new pie and old empty cohort dash', () => {
  const html = board.renderReasonsHtml(snap([], [flat('包装破损', 1, 100)]), 'a', 's', '2026-04');
  assert.equal(sliceCount(html), 1);
  assert.match(html, /包装破损/);
  assert.match(html, /2026年10月前[\s\S]*?—/);
  assert.doesNotMatch(html, /一级类别|二级类别/);
});

test('mixed cohorts each share 100 independently', () => {
  const html = board.renderReasonsHtml(snap([legacy('破损损坏', 2, 100)], [flat('包装破损', 3, 100)]), 'a', 's', '2026-04');
  assert.equal(sliceCount(html), 2);
  assert.match(html, /破损损坏[\s\S]*?100\.00%/);
  assert.match(html, /包装破损[\s\S]*?100\.00%/);
});

test('product aggregate cross-store sums counts and recomputes share', () => {
  const s = { products: [{ sku: 'S', display_name: 'P' }],
    teams: [{ name: 'Team 1', shops: [{ account: 'A' }, { account: 'B' }] }],
    aftersale: { connected: true, reasons: { connected: true,
      items: [legacy('oldcat', 1, 100, { account: 'A', sku: 'S' })],
      field_items: [flat('x', 1, 100, { account: 'A', sku: 'S' }), flat('x', 3, 100, { account: 'B', sku: 'S' }),
        flat('y', 4, 100, { account: 'A', sku: 'S' }),
        flat('foreign', 99, 100, { account: 'OUT', sku: 'S' }),
        flat('foreign', 99, 100, { account: 'A', sku: 'X' }),
        flat('foreign', 99, 100, { account: 'A', sku: 'S', month: '2026-05' }),
        flat('bad', '1', 100, { account: 'A', sku: 'S' }),
        flat('__proto__', 2, 100, { account: 'A', sku: 'S' })] } } };
  const before = JSON.stringify(s);
  const r = board.aggregateProductReasons(s, 'S', '2026-04');
  const by = Object.fromEntries(r.field_items.map(i => [i.category, i]));
  assert.equal(by.x.count, 4);
  assert.equal(by.y.count, 4);
  assert(Math.abs(by.x.share - 40) < 1e-9);
  assert(Math.abs(by.y.share - 40) < 1e-9);
  assert.equal(by.__proto__.count, 2);
  assert(Math.abs(by.__proto__.share - 20) < 1e-9);
  assert(!by.foreign && !by.bad);
  assert.equal(JSON.stringify(s), before);
});

test('actual renderDetailHtml new field path and selection filters', () => {
  const html = board.renderDetailHtml(detailSnap(detailReasons()), { shop: 'a', month: '2026-04', selectedSku: 's' });
  const card = html.match(/②[\s\S]*?(?=③)/)[0];
  assert.match(card, /包装破损/);
  assert.match(card, /铁架变形/);
  assert.match(card, /破损损坏/);
  const other = detailReasons();
  other.field_items.push(flat('外店', 9, 100, { account: 'z' }));
  const html2 = board.renderDetailHtml(detailSnap(other), { shop: 'a', month: '2026-04', selectedSku: 's' });
  assert.doesNotMatch(html2.match(/②[\s\S]*?(?=③)/)[0], /外店/);
});

test('tooltip only flat shows count share and note without child mislabel', () => {
  const s = { products: [{ sku: 'S', display_name: 'P' }],
    teams: [{ name: 'Team 1', shops: [{ account: 'A', skus: [{ sku: 'S', sold: { '2026-04': 10 } }] }] }],
    aftersale: { connected: true, reasons: { connected: true,
      items: [], subcategory_items: [], field_items: [flat('包装破损', 1, 100, { account: 'A', sku: 'S' })] } } };
  const html = board.renderProductReasonTooltipHtml(s, 'S', '2026-04');
  assert.match(html, /包装破损/);
  assert.match(html, /100\.00%/);
  assert.match(html, /1 条/);
  assert.match(html, /占比按原因次数计算/);
  assert.doesNotMatch(html, /二级原因未接入/);
});

test('WF injected field_items renders same reasons and tooltip as no injection', () => {
  const base = { products: [{ sku: 'S', display_name: 'P' }], platform: 'Wayfair',
    teams: [{ name: 'Wayfair', shops: [{ account: 'A', skus: [{ sku: 'S', sold: { '2026-04': 10 } }] }] }],
    aftersale: { connected: true, reasons: { connected: true,
      items: [legacy('破损损坏', 1, 100, { account: 'A', sku: 'S' })], subcategory_items: [] } } };
  const injected = JSON.parse(JSON.stringify(base));
  injected.aftersale.reasons.field_items = [flat('包装破损', 1, 100, { account: 'A', sku: 'S' })];
  const a = board.renderProductReasonTooltipHtml(base, 'S', '2026-04');
  const b = board.renderProductReasonTooltipHtml(injected, 'S', '2026-04');
  assert.equal(a, b);
  assert.doesNotMatch(b, /2026年10月起/);
  const ra = board.renderReasonsHtml(base, 'A', 'S', '2026-04');
  const rb = board.renderReasonsHtml(injected, 'A', 'S', '2026-04');
  assert.equal(ra, rb);
  assert.doesNotMatch(rb, /2026年10月起/);
  assert.match(rb, /破损损坏/);
});

test('flat unknown label is escaped in html and prototype safe', () => {
  const html = board.renderReasonsHtml(snap([], [flat('<img src=x onerror=alert(1)>', 1, 100)]), 'a', 's', '2026-04');
  assert.doesNotMatch(html, /<img src=x onerror=alert\(1\)>/);
  assert.match(html, /&lt;img/);
  const proto = board.renderReasonsHtml(snap([], [flat('__proto__', 1, 100)]), 'a', 's', '2026-04');
  assert.match(proto, /__proto__/);
});

test('detail new field excludes sibling sku month and shop', () => {
  const reasons = detailReasons();
  reasons.field_items.push(flat('他月', 9, 100, { month: '2026-05' }));
  reasons.field_items.push(flat('他sku', 9, 100, { sku: 'other' }));
  const html = board.renderDetailHtml(detailSnap(reasons), { shop: 'a', month: '2026-04', selectedSku: 's' });
  const card = html.match(/②[\s\S]*?(?=③)/)[0];
  assert.match(card, /包装破损/);
  assert.doesNotMatch(card, /他月/);
  assert.doesNotMatch(card, /他sku/);
});

test('old API without field_items keeps two levels and no cohort headings', () => {
  const html = board.renderReasonsHtml(snap([legacy('破损损坏', 1, 100)], undefined,
    [{ account: 'a', sku: 's', month: '2026-04', category: '破损损坏', subcategory: '包装破损', count: 1, share: 100 }]), 'a', 's', '2026-04');
  assert.match(html, /二级原因明细/);
  assert.match(html, /包装破损/);
  assert.doesNotMatch(html, /2026年10月前|2026年10月起/);
});

test('disconnected shows no fake percent', () => {
  const s = { aftersale: { connected: false, reasons: { connected: false, items: null, field_items: null } } };
  const html = board.renderReasonsHtml(s, 'a', 's', '2026-04');
  assert.match(html, /—/);
  assert.doesNotMatch(html, /\d+\.\d+%/);
});

console.log(JSON.stringify({ synthetic_only: true, passed }));
