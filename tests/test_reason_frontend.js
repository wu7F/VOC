const assert = require('node:assert/strict');
const board = require('../web/board.js');
let passed = 0;
function test(name, fn) { fn(); passed++; console.log('ok', name); }
const row = (category, count, share, extras = {}) => ({ account: 'a', sku: 's', month: '2026-04', category, count, share, ...extras });
const snap = items => ({ aftersale: { connected: true, reasons: { connected: true, items, subcategory_items: [] } } });
const svgPart = html => (html.match(/<svg[\s\S]*?<\/svg>/) || [''])[0];
const sliceCount = html => (svgPart(html).match(/<path\b/g) || []).length;
const render = data => board.renderReasonsHtml(data, 'a', 's', '2026-04');

test('pie and unchanged primary table show the same counts and shares', () => {
  const data = snap([row('破损损坏', 2, 40), row('功能故障', 3, 60)]);
  const before = JSON.stringify(data);
  const html = render(data);
  assert.equal(sliceCount(html), 2);
  const table = html.match(/<table class="reason-table reason-primary-table"[\s\S]*?<\/table>/)[0];
  assert.match(table, /破损损坏[\s\S]*?<td>2<\/td><td>40\.00%<\/td>/);
  assert.match(table, /功能故障[\s\S]*?<td>3<\/td><td>60\.00%<\/td>/);
  assert.equal(JSON.stringify(data), before);
});
test('no zero-share or zero-count slices, matching original table colors', () => {
  const html = render(snap([row('zero', 0, 0), row('one', 1, 100)]));
  assert.equal(sliceCount(html), 1);
  const fill = svgPart(html).match(/fill="([^"]+)"/)[1];
  const table = html.match(/<table class="reason-table reason-primary-table"[\s\S]*?<\/table>/)[0];
  assert.ok(table.includes('background:' + fill + '"></span>one'));
  assert.equal(sliceCount(board.renderReasonPieHtml([row('rounds-zero', 1, 0)])), 0);
});
test('single 100-percent slice forms a full circle', () => {
  const svg = svgPart(board.renderReasonPieHtml([row('only', 1, 100)], 'only'));
  const path = svg.match(/d="([^"]+)"/)[1];
  assert.equal((path.match(/ A /g) || []).length, 2);
  assert.match(svg, /role="img"/);
  assert.match(svg, /<title>only<\/title>/);
  assert.match(svg, /<desc>/);
});
test('missing invalid and nonnumeric data stays unavailable', () => {
  for (const count of [null, undefined, '1', true, NaN, Infinity, -1, 0]) {
    assert.equal(sliceCount(board.renderReasonPieHtml([row('invalid', count, 100)])), 0);
  }
  for (const share of [null, undefined, '100', true, NaN, Infinity, -1, 0]) {
    assert.equal(sliceCount(board.renderReasonPieHtml([row('invalid', 1, share)])), 0);
  }
  assert.equal(sliceCount(board.renderReasonPieHtml([row('huge', 1e308, 50), row('huge2', 1e308, 50)])), 0);
});
test('empty and disconnected paths visibly show em dash without pie', () => {
  for (const data of [snap([]), {}, { aftersale: { connected: false, reasons: { connected: false, items: null } } }]) {
    const html = render(data);
    assert.ok(html.includes('—'));
    assert.equal(sliceCount(html), 0);
  }
});
test('exact account SKU and month filter excludes sibling values in both levels', () => {
  const data = snap([row('wanted', 1, 100), row('wrongSKU', 99, 100, { sku: 'x' }),
    row('wrongMonth', 99, 100, { month: '2026-05' }), row('wrongShop', 99, 100, { account: 'x' })]);
  data.aftersale.reasons.subcategory_items = [
    { ...row('wanted', 1, 100), subcategory: 'good-child' },
    { ...row('wanted', 99, 100, { sku: 'x' }), subcategory: 'bad-child-sku' },
    { ...row('wanted', 99, 100, { month: '2026-05' }), subcategory: 'bad-child-month' },
    { ...row('wanted', 99, 100, { account: 'x' }), subcategory: 'bad-child-account' },
    { ...row('other-parent', 99, 100), subcategory: 'bad-child-parent' }];
  const html = render(data);
  assert.match(html, /good-child/);
  assert.doesNotMatch(html, /wrongSKU|wrongMonth|wrongShop|bad-child|99/);
});
test('secondary share is clearly scoped to parent and fallback never invents counts', () => {
  const data = snap([row('damage', 2, 40), row('fault', 3, 60)]);
  data.aftersale.reasons.subcategory_items = [{ ...row('damage', 1, 50), subcategory: '包装破损' },
    { ...row('damage', 1, 50), subcategory: '其他（未细分）' }];
  const html = render(data);
  assert.match(html, /占该一级类别/);
  assert.match(html, /50\.00%/);
  assert.match(html, /二级原因未接入/);
  assert.match(html, /其他（未细分）/);
});
test('hostile labels and messages are escaped', () => {
  const label = '<img src=x onerror=bad>';
  const data = snap([row(label, 1, 100)]);
  data.aftersale.reasons.subcategory_items = [{ ...row(label, 1, 100), subcategory: label }];
  const html = render(data);
  assert.doesNotMatch(html, /<img|<script/);
  assert.match(html, /&lt;img/);
  data.aftersale.connected = false;
  data.aftersale.message = label;
  assert.doesNotMatch(render(data), /<img/);
});
test('all valid slices have finite coordinates and no line-chart hover binding', () => {
  const html = board.renderReasonPieHtml([row('a', 1, 1), row('b', 99, 99)], 'label');
  assert.doesNotMatch(svgPart(html), /NaN|Infinity|data-chart=/);
  assert.equal(sliceCount(html), 2);
});
test('actual detail function uses same filtered pie through SKU and month transitions', () => {
  const data = snap([row('s-Apr', 2, 100), row('t-Apr', 3, 100, { sku: 't' }),
    row('s-May', 4, 100, { month: '2026-05' })]);
  data.months = ['2026-04', '2026-05'];
  data.teams = [{ name: 'Team 1', shops: [{ account: 'a', months: {}, skus:
    [{ sku: 's', sold: {} }, { sku: 't', sold: {} }] }] }];
  const cases = [['s', '2026-04', 's-Apr'], ['t', '2026-04', 't-Apr'], ['s', '2026-05', 's-May']];
  for (const [selectedSku, month, expected] of cases) {
    const html = board.renderDetailHtml(data, { shop: 'a', month, selectedSku });
    const pie = svgPart(html.match(/售后原因占比[\s\S]*/)[0]);
    assert.ok(pie.includes(expected));
    for (const other of ['s-Apr', 't-Apr', 's-May'].filter(x => x !== expected)) assert.ok(!pie.includes(other));
  }
  data.aftersale.reasons.items = [];
  const empty = board.renderDetailHtml(data, { shop: 'a', month: '2026-04', selectedSku: 's' });
  const card = empty.match(/②[\s\S]*?(?=③)/)[0];
  assert.ok(card.includes('—'));
  assert.ok(!card.includes('reason-pie-svg'));
});
console.log(JSON.stringify({ synthetic_only: true, passed }));
