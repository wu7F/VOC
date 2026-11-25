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
    months: ['2026-04', '2026-05'],
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
            skus: [
              { sku: 'PAIRED-SKU-1', display_name: '产品一中文名', wf_part_number: 'WF-1', sold: { '2026-04': 1, '2026-05': 0 } }
            ]
          }
        ]
      },
      { name: 'Team 2', shops: [] },
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
        { account: 'xingshan_US', team: 'Team 1', numerator: 89, denominator: 908, rate: 9.8018 }
      ],
      store_month: [
        { account: 'xingshan_US', month: '2026-04', numerator: 10, denominator: 100, rate: 10.0 }
      ],
      sku_store_month: [
        { account: 'xingshan_US', month: '2026-04', sku: 'PAIRED-SKU-1', numerator: 5, denominator: 50, rate: 10.0 }
      ],
      reasons: { connected: true, items: [
        { account: 'xingshan_US', sku: 'PAIRED-SKU-1', month: '2026-04', category: '破损损坏', count: 2, share: 40.0 },
        { account: 'xingshan_US', sku: 'PAIRED-SKU-1', month: '2026-04', category: '功能故障', count: 3, share: 60.0 },
        { account: 'xingshan_US', sku: 'OTHER', month: '2026-04', category: '未注明原因', count: 99, share: 100.0 }
      ] }
    },
    wayfair: { connected: false, message: 'Wayfair 销量尚未接入' },
    diagnostics: [],
    stale: false,
    error: null
  };
}

async function createBoard(initialQueue = []) {
  const elements = {};
  for (const key of ['nav', 'main', 'banner', 'stale-banner']) {
    elements[key] = { innerHTML: '', querySelectorAll: () => [] };
  }
  const handlers = {};
  const queue = initialQueue.length ? initialQueue.slice() : [response(baseSnapshot())];
  const context = {
    document: {
      hidden: true,
      getElementById: id => elements[id],
      addEventListener: (name, handler) => { handlers[name] = handler; }
    },
    fetch: url => url.startsWith('/api/bitable/wayfair')
      ? Promise.resolve(response({ ok: false, error: 'Wayfair 销量尚未接入' }))
      : Promise.resolve(queue.shift()),
    setInterval: () => {},
    module: { exports: {} }
  };
  vm.runInNewContext(code, context);
  await flush();
  const click = async attrs => {
    handlers.click({ target: { closest: () => ({ getAttribute: key => attrs[key] || null }) } });
    await flush();
  };
  return { elements, queue, click, handlers, api: context.module.exports };
}

let passed = 0;
function ok(cond, msg) {
  assert.ok(cond, msg);
  passed++;
}

(async () => {
  // 1. Team 导航保留
  const board = await createBoard();
  const navKeys = [...board.elements.nav.innerHTML.matchAll(/data-view="([^"]+)"/g)]
    .map(m => m[1]).filter(key => !['all', 'Wayfair', 'Independent'].includes(key));
  ok(navKeys.length === 3, `应恰好 3 个 Team 导航控件，实际 ${navKeys.length}`);
  ok(board.elements.nav.innerHTML.includes('data-view="Independent"'), '导航应包含独立站未接入');

  // 2. Wayfair 独立数据源尚不可用时保留错误占位，不借用 Amazon 数据。
  await board.click({ 'data-bind': 'nav', 'data-view': 'Wayfair' });
  ok(board.elements.main.innerHTML.includes('Wayfair 销量尚未接入'), 'Wayfair 应显示未接入占位');

  // 2b. 独立站未接入占位
  await board.click({ 'data-bind': 'nav', 'data-view': 'Independent' });
  ok(board.elements.main.innerHTML.includes('独立站尚未接入'), '独立站应显示未接入占位');
  ok(!board.elements.main.innerHTML.includes('Amazon'), '独立站占位不应包含 Amazon 指标');

  // 3. 矩阵页显示售后率（确认快照）
  await board.click({ 'data-bind': 'nav', 'data-view': 'all' });
  const matrixHtml = board.elements.main.innerHTML;
  ok(matrixHtml.includes('10.64%'), '矩阵页应显示总售后率 10.64%');
  ok(matrixHtml.includes('确认快照') || matrixHtml.includes('非实时'), '应标注确认快照/非实时');
  ok(matrixHtml.includes('售后率 10.00%，销量 1'), '蓝色月份格应显示店铺月份售后率，并在提示中保留销量');
  ok(!matrixHtml.includes('销量缺失或范围待确认'), '售后率有效时不应把蓝色格标为销量缺失');

  // Matrix view path must show the selected Team's own KPI, not a global total.
  for (const [team, rate] of [['Team 1', '9.93%'], ['Team 2', '13.26%'], ['Team 3', '7.38%']]) {
    await board.click({ 'data-bind': 'nav', 'data-view': team });
    const teamHtml = board.elements.main.innerHTML;
    ok(teamHtml.includes(rate), `${team} matrix KPI should be ${rate}`);
    ok(!teamHtml.includes('10.64%'), `${team} matrix KPI must not show the all-team total`);
  }
  await board.click({ 'data-bind': 'nav', 'data-view': 'all' });
  const missingMatrixSnapshot = clone(baseSnapshot());
  missingMatrixSnapshot.aftersale.store_month = [];
  const missingMatrixBoard = await createBoard([response(missingMatrixSnapshot)]);
  ok(missingMatrixBoard.elements.main.innerHTML.includes('class=\"rate-na\"><button') && missingMatrixBoard.elements.main.innerHTML.includes('>—</button>'),
    'store-month rate missing from confirmed snapshot should render as em dash rather than 0%');

  // 4. 详情页显示 Team/店铺/月度/SKU 售后率
  await board.click({ 'data-bind': 'matrix-cell', 'data-shop': 'xingshan_US', 'data-month': '2026-04' });
  const detailHtml = board.elements.main.innerHTML;
  ok(detailHtml.includes('Team 1'), '详情页应显示 Team 售后率');
  ok(detailHtml.includes('9.93%'), '详情页应显示 Team 1 售后率');
  ok(detailHtml.includes('xingshan_US'), '详情页应显示店铺售后率');
  ok(detailHtml.includes('2026-04'), '详情页应显示月度售后率');
  ok(detailHtml.includes('售后率 <b>10.00%</b>') && detailHtml.includes('售后 5 / 销量 50'),
    'selected SKU detail card should show matched numerator, denominator, and rate');
ok(detailHtml.includes('<td>2026-04</td><td>5</td><td>50</td><td>10.00%</td>'),
    'selected SKU detail card should show its confirmed month history');
  ok(detailHtml.includes('PAIRED-SKU-1'), '详情页应显示选中 SKU 月度售后率');
  ok(detailHtml.includes('破损损坏') && detailHtml.includes('功能故障') && detailHtml.includes('40.00%') && detailHtml.includes('60.00%'), '原因卡应展示已聚合类别及占比');
  ok(!detailHtml.includes('原文数据访问受限'), '实时聚合成功时不应显示权限失败空态');
  ok(!detailHtml.includes('99'), '其他SKU的原因不得混入当前SKU结果');
  ok(!detailHtml.includes('售后分子和原因尚未接入多维表格，售后率显示'),
    'confirmed snapshot detail must not retain the old after-sale-unavailable placeholder');
  const skuRow = detailHtml.match(/<tr[^>]*data-bind="sku-row"[\s\S]*?<\/tr>/);
  ok(skuRow && skuRow[0].includes('<td>5</td><td>10.00%</td>'),
    'SKU table after-sale numerator and rate must match selected store/month/SKU snapshot row');

  // Missing snapshot SKU rows stay unknown, never silently become zero.
  const missingSkuSnapshot = clone(baseSnapshot());
  missingSkuSnapshot.aftersale.sku_store_month = [];
  const missingSkuBoard = await createBoard([response(missingSkuSnapshot)]);
  await missingSkuBoard.click({ 'data-bind': 'matrix-cell', 'data-shop': 'xingshan_US', 'data-month': '2026-04' });
  const missingSkuDetail = missingSkuBoard.elements.main.innerHTML;
  const missingSkuRow = missingSkuDetail.match(/<tr[^>]*data-bind="sku-row"[\s\S]*?<\/tr>/);
  ok(missingSkuRow && /<td>—<\/td><td>—<\/td>/.test(missingSkuRow[0]),
    'missing matched SKU row should stay unknown in both table cells');
  ok(!/(?:>|\s)0(?:\.00)?%/.test(missingSkuRow ? missingSkuRow[0] : ''),
    'missing SKU snapshot row must never be rendered as a 0% rate');
  ok(missingSkuDetail.includes('未知，不按 0% 计算'),
    'missing matched SKU detail must explicitly remain unknown, not unavailable or zero');

  // 5. 刷新失败保留旧快照
  const b = await createBoard();
  ok(b.elements.main.innerHTML.includes('xingshan_US'), '应已加载初始快照');
  b.queue.push(response({ detail: 'SYNTHETIC server error' }, 500));
  await b.click({ 'data-bind': 'action', 'data-action': 'refresh' });
  ok(b.elements.main.innerHTML.includes('xingshan_US'), '刷新失败应保留旧快照');
  ok(b.elements['stale-banner'].innerHTML.length > 0, '刷新失败应显示错误提示');

  // 6. 售后未接入时显示占位
  const snapNoAfter = clone(baseSnapshot());
  snapNoAfter.aftersale = { connected: false, message: '售后分子和原因尚未接入多维表格,售后率显示“—”,不按 0% 计算', reasons: { connected: false, items: null } };
  const bNoAfter = await createBoard([response(snapNoAfter)]);
  ok(bNoAfter.elements.main.innerHTML.includes('售后分子和原因尚未接入'), '未接入时应显示占位');

  // 7. 售后已接入但无 SKU 月度数据时显示未知/未接入警告，不显示 0.00%
  const snapNoSku = clone(baseSnapshot());
  snapNoSku.aftersale.connected = true;
  snapNoSku.aftersale.sku_store_month = [];
  const bNoSku = await createBoard([response(snapNoSku)]);
  const noSkuHtml = bNoSku.api.renderAftersaleHtml(snapNoSku, { shop: 'xingshan_US', selectedSku: 'PAIRED-SKU-1' });
  ok(noSkuHtml.includes('未知') || noSkuHtml.includes('尚未接入'), '无 SKU 月度数据时应显示未知/未接入警告');
  ok(!/(?:>|\s)0\.00%/.test(noSkuHtml), '无 SKU 月度数据时不应显示 0.00%');

  console.log(JSON.stringify({ synthetic_only: true, passed }));
  process.exitCode = 0;
})();
