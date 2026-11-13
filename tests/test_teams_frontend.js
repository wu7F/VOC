/* Offline Team UI regressions; no browser, provider calls or credentials. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const code = fs.readFileSync(path.join(__dirname, '../web/app.js'), 'utf8');
const html = fs.readFileSync(path.join(__dirname, '../web/index.html'), 'utf8');
function section(from, to) {
  const start = code.indexOf(from), end = code.indexOf(to, start);
  assert.ok(start >= 0 && end > start, `Missing source section ${from}`);
  return code.slice(start, end);
}
const functions = [
  section('function esc(', 'function toast('),
  section('function renderAftersaleTeams(', '/** 店铺名单弹窗'),
  section('async function openAftersaleSummary(', 'function switchTab('),
].join('\n');
const groups = [
  ['xingshan_US', 'KornerHK_US', 'southwest_US'],
  ['Wekas_US', 'kaifengzhejie_US'],
  ['maoyitong_US', 'AmazonMJSKY_US_US', 'RUIBAO_US_US'],
];
function teams() {
  return groups.map((members, i) => ({ team: `Team ${i + 1}`, members,
    records: i === 0 ? 7 : 0, aftersale: i === 0 ? 7 : 0,
    rate_numerator: i === 0 ? 7 : 0, sales_qty: 1000,
    rate: i === 0 ? 0.007 : 0, rate_basis: 'feishu_qty',
    missing_shops: [], excluded_shops: [], rate_warning: '',
    shops: members.map((account, j) => ({ account, shop_names: [account],
      records: j === 0 ? 7 : 0, aftersale: j === 0 ? 7 : 0,
      sales_qty: j === 0 ? 10 : 990, rate: j === 0 ? .7 : 0, status: 'ok' })),
  }));
}
function context() {
  const elements = new Map();
  const products = [];
  const card = { dataset: { shop: 'xingshan_US' }, classList: { toggle() {}, remove() {} } };
  const c = { elements, products, card, state: {
    asShop: '', platforms: [{ id: 'AmazonCs' }, { id: 'WayfairCs' }],
    current: { created_at: '2026-11-11', elapsed: 1, warnings: [] },
    result: { platform: 'AmazonCs', aftersale: { mode: 'ledger', rate_basis: 'feishu_qty',
      total_orders: 7, aftersale_orders: 7, overall_rate: .007, products: [],
      sales: { qty_used: 1000 }, teams: teams(), shops: [{ shop: 'xingshan_US',
        product_count: 1, aftersale: 7, rate: .7, sales_qty: 10, share: 1 }] } },
  },
  $: selector => {
    if (!elements.has(selector)) elements.set(selector, {
      innerHTML: '', textContent: '', hidden: false,
      classList: { add() {}, remove() {} },
      closest: () => ({ querySelector: () => ({ textContent: '' }) }),
    });
    return elements.get(selector);
  },
  $$: selector => selector === '#panel-overview .as-shop-card' ? [card] : [],
  renderAsProducts: (...args) => products.push(args), renderAsFresh() {}, showWarnings() {},
  openShopPicker() {}, setTimeout() {},
  document: { documentElement: { style: { setProperty() {} } } },
  api: async resource => {
    assert.equal(resource, '/api/aftersale/summary');
    return { json: async () => ({ overall: { total: 7, aftersale: 7, rate: .007, sales_qty: 1000 },
      mode: 'feishu_qty', products: [], datasets: 1, platforms: [{ platform: 'AmazonCs' }],
      teams: teams() }) };
  },
  };
  vm.createContext(c);
  vm.runInContext(functions, c);
  return c;
}
const cases = [];
function test(name, fn) { cases.push({ name, fn }); }

test('Team cards show all eight memberships and the backend weighted rate', () => {
  const output = context().renderAftersaleTeams(teams());
  assert.equal((output.match(/class="as-card as-team-card"/g) || []).length, 3);
  for (const account of groups.flat()) assert.ok(output.includes(account));
  assert.ok(output.indexOf('Team 1') < output.indexOf('Team 2'));
  assert.ok(output.indexOf('Team 2') < output.indexOf('Team 3'));
  // Backend returns 7/1000 while individual rates are .7 and 0.
  assert.match(output, /as-team-rate">0\.7%/);
  assert.doesNotMatch(output, /as-team-rate">(?:23\.3|35\.0)%/);
});

test('Zero-event sales show 0% while missing members keep null values visible', () => {
  const c = context();
  const zero = c.renderAftersaleTeams([teams()[1]]);
  assert.match(zero, /as-team-rate">0\.0%/);
  const missing = teams()[2];
  Object.assign(missing, { rate: null, sales_qty: null, missing_shops: ['RUIBAO_US_US'] });
  const shop = missing.shops[2];
  Object.assign(shop, { status: 'missing', rate: 0, sales_qty: 0 });
  const output = c.renderAftersaleTeams([missing]);
  assert.match(output, /as-team-rate">—/);
  assert.match(output, /飞书销量表缺少店铺：RUIBAO_US_US/);
  assert.match(output, /销量 — 件/);
  const row = output.slice(output.indexOf('<tr><td><b>RUIBAO_US_US'));
  assert.match(row, /<td>—<\/td>\s*<td>—<\/td>/);
  assert.doesNotMatch(row, /0\.0%/);
});

test('Unavailable, excluded and zero-sales shops retain distinct explanations', () => {
  const group = teams()[0];
  group.shops.forEach((shop, i) => { shop.status = ['unavailable', 'excluded', 'zero_sales'][i]; });
  Object.assign(group, { rate: null, sales_qty: null, excluded_shops: ['KornerHK_US'],
    rate_warning: '销量尚未确认完整' });
  const output = context().renderAftersaleTeams([group]);
  assert.match(output, /销量尚未确认/);
  assert.match(output, /店铺名单已排除：KornerHK_US/);
  assert.match(output, /销量为 0，暂不计算率/);
});

test('Store names and server warning text are escaped in every Team display', () => {
  const group = teams()[0];
  const attack = '<img src=x onerror="alert(1)">';
  Object.assign(group, { team: attack, members: [attack], missing_shops: [attack],
    excluded_shops: [attack], rate_warning: attack });
  Object.assign(group.shops[0], { account: attack, shop_names: [attack] });
  const output = context().renderAftersaleTeams([group]);
  assert.doesNotMatch(output, /<img/);
  assert.match(output, /&lt;img src=x onerror=&quot;alert\(1\)&quot;&gt;/);
});

test('Unmatched raw shop names are deduplicated, escaped and never assigned to a Team', () => {
  const groups = teams();
  const unknown = '02ruibao_US<img src=x>';
  groups.forEach(group => { group.unmatched_shops = [unknown]; });
  const output = context().renderAftersaleTeams(groups);
  assert.equal((output.match(/未归入 Team 的数据店名/g) || []).length, 1);
  assert.equal((output.match(/02ruibao_US/g) || []).length, 1);
  assert.match(output, /02ruibao_US&lt;img src=x&gt;/);
  assert.doesNotMatch(output, /<img/);
  assert.ok(output.indexOf('02ruibao_US') < output.indexOf('class="as-team-grid"'));
});

test('Amazon report renders Teams before shops and preserves shop-product filtering', () => {
  const c = context();
  c.renderAftersale();
  const output = c.elements.get('#panel-overview').innerHTML;
  assert.ok(output.indexOf('亚马逊 Team 售后汇总') < output.indexOf('各店铺售后'));
  assert.match(output, /data-shop="xingshan_US"/);
  c.card.onclick();
  assert.equal(c.state.asShop, 'xingshan_US');
  assert.equal(c.products.at(-1)[1], 'xingshan_US');
  c.card.onclick();
  assert.equal(c.state.asShop, '');
  assert.equal(c.products.at(-1)[1], '');
  Object.assign(c.state.result.aftersale.shops[0], { rate: null, sales_qty: 0 });
  c.renderAftersale();
  assert.match(c.elements.get('#panel-overview').innerHTML, /销量 —（缺分母） 件/);
  assert.doesNotMatch(c.elements.get('#panel-overview').innerHTML, /销量 0 件/);
});

test('Legacy Amazon reports ask for reanalysis and Wayfair never renders Teams', () => {
  const c = context();
  delete c.state.result.aftersale.teams;
  c.renderAftersale();
  assert.match(c.elements.get('#panel-overview').innerHTML, /历史报告尚无 Team 汇总，请重新分析/);
  c.state.result.platform = 'WayfairCs';
  c.state.result.aftersale.teams = teams();
  c.renderAftersale();
  assert.doesNotMatch(c.elements.get('#panel-overview').innerHTML, /Team/);
});

test('Summary API result renders Teams in its dedicated modal container', async () => {
  assert.match(html, /id="asSumTeams"/);
  const c = context();
  await c.openAftersaleSummary();
  const output = c.elements.get('#asSumTeams').innerHTML;
  for (const account of groups.flat()) assert.ok(output.includes(account));
  assert.match(output, /as-team-rate">0\.7%/);
  c.renderAftersaleSummary({ overall: { total: 0, aftersale: 0, rate: null },
    products: [], mode: 'ledger', datasets: 0, platforms: [{ platform: 'WayfairCs' }], teams: [] });
  assert.equal(c.elements.get('#asSumTeams').innerHTML, '');
});

test('Legacy summary Team counts remain unknown rather than zero', () => {
  const c = context();
  const groups = teams();
  groups.forEach(group => {
    Object.assign(group, { records: null, aftersale: null, rate_numerator: null,
      sales_qty: null, rate: null, rate_warning: '历史报告缺少 Team 明细，请重新分析。' });
    group.shops.forEach(shop => Object.assign(shop, { records: null, aftersale: null,
      sales_qty: null, rate: null, status: 'unavailable' }));
  });
  c.renderAftersaleSummary({ overall: { total: 7, aftersale: 7, rate: null },
    mode: 'ledger', products: [], platforms: [{ platform: 'AmazonCs' }], datasets: 1, teams: groups });
  const output = c.elements.get('#asSumTeams').innerHTML;
  assert.match(output, /售后登记 — 条 · 计入率 — 条 \/ 销量 — 件/);
  assert.match(output, /请重新分析/);
  assert.doesNotMatch(output, /售后登记 0|<td>0<\/td>|0\.0%/);
});

test('Detail-mode Team denominator is labeled as table orders', () => {
  const group = teams()[0];
  Object.assign(group, { rate_basis: 'detail', records: 10, rate_numerator: 7, rate: .7 });
  const output = context().renderAftersaleTeams([group]);
  assert.match(output, /表内订单 10 单/);
  assert.match(output, /as-team-rate">70\.0%/);
  assert.doesNotMatch(output, /飞书销量表/);
  assert.doesNotMatch(output, /同期销量/);
});

(async () => {
  const failed = [];
  for (const entry of cases) {
    try { await entry.fn(); }
    catch (error) { failed.push({ case: entry.name, error: error.stack }); }
  }
  console.log(JSON.stringify({ synthetic_only: true, passed: cases.length - failed.length,
    failed: failed.length, cases: cases.map(entry => entry.name), failures: failed }, null, 2));
  if (failed.length) process.exitCode = 1;
})();
