const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const board = require('../web/board.js');
const clone = x => JSON.parse(JSON.stringify(x));
const code = fs.readFileSync(path.join(__dirname, '../web/board.js'), 'utf8');
const flush = async () => { for (let i = 0; i < 4; i++) await new Promise(setImmediate); };
const reply = body => ({ok: true, status: 200, json: async () => clone(body)});
const deferred = () => { let resolve; const promise = new Promise(r => { resolve = r; }); return {promise, resolve}; };

function fixture(platform = 'Wayfair') {
  const wf = platform === 'Wayfair';
  const account = wf ? 'Wayfair玺家' : 'xingshan_US';
  const group = wf ? 'Wayfair' : 'Team 1';
  const name = wf ? '玺家沙发<script>bad()</script>' : 'Amazon独有产品';
  const counts = {numerator: 145, denominator: 1416, rate: 145 / 1416 * 100};
  const rows = [{account, team: group, sku: 'CANON', month: '2026-04', numerator: 145, denominator: 600, rate: 145 / 6},
    {account, team: group, sku: 'CANON', month: '2026-12', numerator: 0, denominator: 800, rate: 0}];
  return {ok: true, version: 2, ...(wf ? {platform} : {}),
    fetched_at: '2027-01-03T12:34:56+08:00', last_success_at: '2027-01-03T12:34:56+08:00',
    months: ['2026-04', '2026-05', '2026-12', '2027-01'], current_month: '2027-01', current_month_partial: true,
    products: [{sku: 'CANON', display_name: name, wf_part_number: 'WF-A / WF-B'},
      {sku: 'NO-SALE', display_name: '未销售产品', wf_part_number: 'WF-N'}],
    teams: [{name: group, unpaired_units: 7, unpaired_skus: ['<unpaired>'],
      unpaired_sku_totals: [{sku: '<unpaired>', units: 7}], merged_sku_totals: [], shops: [{
        account, display_name: account, units_known: 1416, missing_months: [],
        months: {'2026-04': 600, '2026-05': 0, '2026-12': 800, '2027-01': 16},
        skus: [{sku: 'CANON', display_name: name, wf_part_number: 'WF-A / WF-B',
          sold: {'2026-04': 600, '2026-05': 0, '2026-12': 800, '2027-01': 16}},
        {sku: 'NO-SALE', display_name: '未销售产品', wf_part_number: 'WF-N', sold: {}}]
      }]}],
    aftersale: {connected: true, realtime: true, message: '一级和二级来自售后原因字段',
      total: counts, summary: counts, teams: wf ? [] : [{name: group, ...counts}],
      stores: [{account, ...counts}], store_month: rows.map(({sku, team, ...r}) => r), sku_store_month: rows,
      reasons: {connected: true, message: '售后原因字段', items: [
        {account, sku: 'CANON', month: '2026-04', category: '质量问题', count: 121, share: 121 / 145 * 100},
        {account, sku: 'CANON', month: '2026-04', category: '其他问题', count: 24, share: 24 / 145 * 100}],
        subcategory_items: [{account, sku: 'CANON', month: '2026-04', category: '质量问题', subcategory: '<断裂>', count: 121, share: 100}]}},
    source: {table_name: platform, row_count: 5, page_count: 1},
    counts: {aftersale_duplicate_extra_rows: 1},
    diagnostics: [{code: 'unmatched', message: '未配对', count: 2}],
    unpaired: {total_units: 7, sales: [{sku: '<unpaired>', units: 7, rows: 2}], aftersale: [{sku: 'AF-NOMATCH', rows: 3}]},
    unmapped: [], other_platforms: [], stale: false, error: null};
}

async function harness() {
  const elements = Object.fromEntries(['nav', 'main', 'banner', 'stale-banner'].map(k => [k, {innerHTML: '', querySelectorAll: () => []}]));
  const handlers = {}, calls = [], pending = {Amazon: [], Wayfair: []};
  let interval;
  const document = {hidden: false, getElementById: id => elements[id], addEventListener: (event, fn) => { handlers[event] = fn; }};
  vm.runInNewContext(code, {document, module: {exports: {}}, setInterval: fn => { interval = fn; },
    fetch: url => {
      calls.push(url);
      const market = url.startsWith('/api/bitable/wayfair') ? 'Wayfair' : 'Amazon';
      if (pending[market].length) return pending[market].shift();
      return Promise.resolve(reply(fixture(market)));
    }});
  await flush();
  const click = async attrs => {
    handlers.click({target: {closest: () => ({getAttribute: k => attrs[k] || null})}});
    await flush();
  };
  return {elements, calls, pending, click, interval: () => interval(), main: () => elements.main.innerHTML};
}

(async () => {
  const snap = fixture(), original = JSON.stringify(snap);
  const agg = board.aggregateProductMonths(snap);
  const canon = agg.items.find(x => x.sku === 'CANON');
  assert.equal(canon.sales['2026-04'], 600, 'Wayfair销售不能被三Team过滤器丢弃');
  assert.equal(canon.sales['2026-05'], 0, '保留真实0');
  assert.equal(canon.months['2026-04'].numerator, 145);
  assert.equal(canon.months['2026-12'].rate, 0, 'Wayfair月份轴不能固定到11月');
  assert.equal(agg.items.find(x => x.sku === 'NO-SALE').sales['2026-04'], undefined, '无销量不补Amazon确认0');
  const tip = board.renderProductReasonTooltipHtml(snap, 'CANON', '2026-04');
  assert.ok(tip.includes('其他问题：16.55%'));
  assert.ok(tip.indexOf('质量问题') < tip.indexOf('其他问题'));
  assert.ok(tip.includes('&lt;断裂&gt;') && !tip.includes('<断裂>'));
  assert.ok(!tip.includes('<script>'));
  assert.ok(board.renderProductReasonTooltipHtml(snap, 'CANON', '2026-05').includes('—'));
  const products = board.renderProductMonthsHtml(snap, {});
  assert.ok(products.includes('2027-01') && products.includes('2026-12'));
  assert.ok(!products.includes('全部亚马逊'));
  const matrix = board.renderMatrixHtml(snap, {view: 'Wayfair', matrixMode: 'product'});
  assert.ok(matrix.includes('data-product-chart'));
  assert.ok(!matrix.includes('Wayfair 销量尚未接入'));
  assert.ok(!matrix.includes('Amazon 已知销量') && !matrix.includes('Team 1'));
  const diag = board.renderDiagnosticsHtml(snap);
  assert.ok(diag.includes('&lt;unpaired&gt;') && diag.includes('AF-NOMATCH'));
  assert.ok(diag.includes('销量') && diag.includes('售后'));
  const counterSnap = clone(snap);
  counterSnap.diagnostics = [{code: 'invalid_rows', message: '故障行数量', count: 913}];
  const counterHtml = board.renderDiagnosticsHtml(counterSnap);
  assert.ok(counterHtml.includes('故障行数量') && counterHtml.includes('913'), '无level诊断也须显示实际计数');
  const detail = board.renderDetailHtml(snap, {view: 'Wayfair', shop: 'Wayfair玺家', month: '2026-04', selectedSku: 'CANON'});
  assert.ok(detail.includes('<svg') && detail.includes('16.55%'));
  assert.ok(!detail.includes('二级按关键词'));
  assert.equal(JSON.stringify(snap), original, 'render不得修改来源快照');

  const h = await harness();
  assert.ok(h.calls.every(x => !x.includes('wayfair')), '初始Amazon不应读取Wayfair');
  const slowWf = deferred(); h.pending.Wayfair.push(slowWf.promise);
  await h.click({'data-bind': 'nav', 'data-view': 'Wayfair'});
  assert.ok(!h.main().includes('xingshan_US'), 'WF加载中不能挂Amazon旧数据');
  await h.click({'data-bind': 'nav', 'data-view': 'Team 1'});
  slowWf.resolve(reply(fixture())); await flush();
  assert.ok(h.main().includes('xingshan_US') && !h.main().includes('Wayfair玺家'), '过期WF响应不能覆盖Amazon');
  await h.click({'data-bind': 'nav', 'data-view': 'Wayfair'});
  assert.ok(h.main().includes('Wayfair玺家') && !h.main().includes('xingshan_US'));
  await h.click({'data-bind': 'matrix-cell', 'data-shop': 'Wayfair玺家', 'data-month': '2026-04'});
  await h.click({'data-bind': 'sku-row', 'data-i': '1'});
  assert.ok(/selected-sku-code[^>]*>NO-SALE /.test(h.main()), 'Wayfair点击第二SKU必须从活跃平台快照选品');
  await h.click({'data-bind': 'action', 'data-action': 'back'});
  const slowOld = deferred(); h.pending.Wayfair.push(slowOld.promise);
  await h.click({'data-bind': 'action', 'data-action': 'refresh'});
  await h.click({'data-bind': 'nav', 'data-view': 'all'});
  slowOld.resolve(reply(fixture())); await flush();
  assert.ok(h.main().includes('xingshan_US') && !h.main().includes('Wayfair玺家'));
  await h.click({'data-bind': 'nav', 'data-view': 'Wayfair'});
  h.pending.Wayfair.push(Promise.resolve({ok: false, status: 500, json: async () => ({error: 'WF_ONLY_FAILURE'})}));
  await h.click({'data-bind': 'action', 'data-action': 'refresh'});
  assert.ok(h.main().includes('Wayfair玺家') && !h.main().includes('xingshan_US'));
  assert.ok(h.elements['stale-banner'].innerHTML.includes('WF_ONLY_FAILURE'));
  h.pending.Wayfair.push(Promise.resolve(reply(fixture('Amazon'))));
  await h.click({'data-bind': 'action', 'data-action': 'refresh'});
  assert.ok(h.main().includes('Wayfair玺家') && !h.main().includes('xingshan_US'), 'Wayfair接口返回错误平台必须拒绝并保留同平台快照');
  h.interval(); await flush();
  assert.ok(h.calls.at(-1).startsWith('/api/bitable/wayfair'), '轮询仅读活跃平台');
  const before = h.calls.length;
  await h.click({'data-bind': 'nav', 'data-view': 'Independent'}); h.interval(); await flush();
  assert.ok(h.main().includes('未接入'));
  assert.ok(!h.main().includes('数据诊断'), '独立站占位不应附带Amazon诊断');
  assert.equal(h.calls.length, before, '独立站占位不读其他平台');
  await h.click({'data-bind': 'nav', 'data-view': 'Team 2'});
  assert.ok(!h.main().includes('data-mode="product"'), 'Team页仍无产品×月份切换');

  const firstFail = await harness();
  firstFail.pending.Wayfair.push(Promise.resolve(reply({ok: false, error: 'NO_WF_ACCESS'})));
  await firstFail.click({'data-bind': 'nav', 'data-view': 'Wayfair'});
  assert.ok(!firstFail.main().includes('xingshan_US') && firstFail.main().includes('NO_WF_ACCESS'));
  console.log('Wayfair render, missing/zero, reason shares, escaping, month axes and platform request/cache isolation passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
