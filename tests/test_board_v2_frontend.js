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
    version: 1,
    ok: true,
    fetched_at: '2026-11-15T10:00:00+08:00',
    last_success_at: '2026-11-15T10:00:00+08:00',
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
              { sku: 'PAIRED-SKU-1', display_name: '产品一中文名', wf_part_number: 'WF-1', sold: { '2026-04': 1, '2026-05': 0 } },
              { sku: 'UNPAIRED-PART', display_name: '', wf_part_number: null, sold: { '2026-04': 1, '2026-05': null } }
            ]
          },
          {
            account: 'southwest_US',
            display_name: 'southwest_US',
            months: { '2026-04': null, '2026-05': null },
            units_known: 0,
            missing_months: [],
            skus: []
          }
        ]
      },
      {
        name: 'Team 2',
        shops: [
          {
            account: 'Wekas_US',
            display_name: 'Wekas_US',
            months: { '2026-04': 2, '2026-05': null },
            units_known: 2,
            missing_months: [],
            skus: [
              { sku: 'PAIRED-SKU-2', display_name: '产品二中文名', wf_part_number: 'WF-2', sold: { '2026-04': 2, '2026-05': null } }
            ]
          }
        ]
      },
      {
        name: 'Team 3',
        shops: [
          {
            account: 'maoyitong_US',
            display_name: 'maoyitong_US',
            months: { '2026-04': 3, '2026-05': null },
            units_known: 3,
            missing_months: [],
            skus: [
              { sku: 'PAIRED-SKU-3', display_name: '产品三中文名', wf_part_number: 'WF-3', sold: { '2026-04': 3, '2026-05': null } }
            ]
          }
        ]
      }
    ],
    unmapped: [],
    other_platforms: [],
    aftersale: { connected: false, message: '售后分子和原因尚未接入多维表格,售后率显示“—”,不按 0% 计算' },
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
    fetch: () => Promise.resolve(queue.shift()),
    setInterval: () => {},
    module: { exports: {} }
  };
  vm.runInNewContext(code, context);
  await flush();
  const click = async attrs => {
    handlers.click({ target: { closest: () => ({ getAttribute: key => attrs[key] || null }) } });
    await flush();
  };
  return { elements, queue, click, handlers };
}

let passed = 0;
function ok(cond, msg) {
  assert.ok(cond, msg);
  passed++;
}

(async () => {
  // 1. 实际 nav 控件点击显示正确组
  const boardNav = await createBoard();
  const navKeys = [...boardNav.elements.nav.innerHTML.matchAll(/data-view="([^"]+)"/g)]
    .map(m => m[1]).filter(key => !['all', 'Wayfair'].includes(key));
  ok(navKeys.length === 3, `应恰好 3 个 Team 导航控件，实际 ${navKeys.length}`);
  for (const view of navKeys) {
    await boardNav.click({ 'data-bind': 'nav', 'data-view': view });
    const teamNameWithSpace = view.replace('Team', 'Team '); // Team1 -> Team 1
    const teamNameNoSpace = view;
    const hasTeam = boardNav.elements.main.innerHTML.includes(teamNameWithSpace) ||
                    boardNav.elements.main.innerHTML.includes(teamNameNoSpace);
    ok(hasTeam, `点击 ${view} 后应显示对应组内容`);
    // 不应显示“该视图暂无店铺数据”
    ok(!boardNav.elements.main.innerHTML.includes('该视图暂无店铺数据'),
      `点击 ${view} 不应显示空视图`);
  }

  // 2. 刷新失败保留旧快照（HTTP 401, 500, ok:false, 坏JSON, 无效结构）
  for (const [name, body, status, jsonReject] of [
    ['http401', { detail: '请先登录' }, 401],
    ['http500', { detail: 'SYNTHETIC server error' }, 500],
    ['ok_false', { ok: false, error: 'SYNTHETIC read failure' }, 200],
    ['bad_json', 'not json', 200, true],
    ['invalid_structure', null, 200, false]
  ]) {
    const b = await createBoard();
    // 初始快照已加载
    ok(b.elements.main.innerHTML.includes('xingshan_US'), '应已加载初始快照');
    const successTime = b.elements.banner.innerHTML.match(/数据读取于 ([\d:]+)/)[1];
    const resp = jsonReject ? { ok: status < 400, status, json: async () => { throw new SyntaxError('Unexpected token'); } } : response(body, status);
    b.queue.push(resp);
    await b.click({ 'data-bind': 'action', 'data-action': 'refresh' });
    const retained = b.elements.main.innerHTML.includes('xingshan_US');
    ok(retained, `场景 ${name} 应保留旧快照`);
    const staled = b.elements['stale-banner'].innerHTML;
    ok(staled.length > 0, `场景 ${name} 应显示错误提示`);
    ok(staled.includes(successTime), `场景 ${name} 应保留成功时间`);
    // 对于 ok:false 或其他失败，错误消息应显示
    if (name === 'ok_false') {
      ok(staled.includes('SYNTHETIC read failure'), 'ok:false 错误消息应显示');
    } else if (name === 'bad_json') {
      ok(staled.includes('看板响应不是 JSON') || staled.includes('读取失败'), '坏JSON应显示错误');
    } else if (name === 'invalid_structure') {
      ok(staled.length > 0, '无效结构应有错误');
    }
  }

  // 3. 真实初始错误：从未成功加载时，应显示初始错误
  const bInitError = await createBoard([response({ ok: false, error: 'initial fail' }, 200)]);
  ok(bInitError.elements.main.innerHTML.includes('加载失败') || bInitError.elements.main.innerHTML.includes('initial fail'),
    '初始加载失败应显示错误');
  ok(!bInitError.elements.main.innerHTML.includes('xingshan_US'), '初始失败不应有旧快照');

  // 4. 刷新插入/重排 SKU 保持所选产品
  const bSku = await createBoard();
  // 进入详情
  await bSku.click({ 'data-bind': 'matrix-cell', 'data-shop': 'xingshan_US', 'data-month': '2026-04' });
  // 选择 SKU-C? 当前只有两个 SKU，选择第二个（UNPAIRED-PART）作为目标
  await bSku.click({ 'data-bind': 'sku-row', 'data-i': '1' });
  const selectedLabel = html => html.match(/class="selected-sku-code">([^<]*)<\/span>/)[1].split(' · ')[0];
  const before = selectedLabel(bSku.elements.main.innerHTML);
  ok(before === 'UNPAIRED-PART', '应选中 UNPAIRED-PART');
  // 模拟刷新后 SKU 列表在前面插入新 SKU，且重排
  const newSnap = clone(baseSnapshot());
  newSnap.teams[0].shops[0].skus = [
    { sku: 'SKU-A', display_name: '新插入SKU', wf_part_number: 'WF-NEW', sold: { '2026-04': 3, '2026-05': null } },
    ...newSnap.teams[0].shops[0].skus
  ];
  // 重排：将 UNPAIRED-PART 移到后面
  newSnap.teams[0].shops[0].skus.sort((a, b) => a.sku.localeCompare(b.sku));
  bSku.queue.push(response(newSnap));
  await bSku.click({ 'data-bind': 'action', 'data-action': 'refresh' });
  const after = selectedLabel(bSku.elements.main.innerHTML);
  ok(after === 'UNPAIRED-PART', `刷新后应保持选中 UNPAIRED-PART，实际 ${after}`);

  // 5. 删除已选 SKU 才回退
  const bDel = await createBoard();
  await bDel.click({ 'data-bind': 'matrix-cell', 'data-shop': 'xingshan_US', 'data-month': '2026-04' });
  await bDel.click({ 'data-bind': 'sku-row', 'data-i': '1' });
  const selectedBeforeDel = selectedLabel(bDel.elements.main.innerHTML);
  ok(selectedBeforeDel === 'UNPAIRED-PART', '删除前选中 UNPAIRED-PART');
  const snapDel = clone(baseSnapshot());
  snapDel.teams[0].shops[0].skus = snapDel.teams[0].shops[0].skus.filter(s => s.sku !== 'UNPAIRED-PART');
  bDel.queue.push(response(snapDel));
  await bDel.click({ 'data-bind': 'action', 'data-action': 'refresh' });
  const afterDel = selectedLabel(bDel.elements.main.innerHTML);
  ok(afterDel !== 'UNPAIRED-PART', '删除已选 SKU 后应回退到其他 SKU');

  // 6. 中文名与分母表头：详情页应显示中文名，表头应包含分母信息
  const bDetail = await createBoard();
  await bDetail.click({ 'data-bind': 'matrix-cell', 'data-shop': 'xingshan_US', 'data-month': '2026-04' });
  const detailHtml = bDetail.elements.main.innerHTML;
  ok(detailHtml.includes('产品一中文名'), '详情页应显示 SKU 中文名');
  ok(detailHtml.includes('WF-1'), '详情页应显示 WF Part Number');
  // 表头应有未配对件数/诊断等
  const matrixHtml = (await createBoard()).elements.main.innerHTML;
  ok(matrixHtml.includes('未配对') || matrixHtml.includes('诊断'), '矩阵表头应包含未配对/诊断信息');

  // 7. 售后率标题跟随所选 SKU 且未接入，销量线不替代售后率占位
  ok(detailHtml.includes('售后原因占比'), '应保留售后原因占位');
  ok(detailHtml.includes('售后分子和原因尚未接入'), '售后未接入消息应显示');
  // 从h3中分别找到“月度售后率”和“售后原因占比”，验证两个标题都含当前所选原SKU或中文名
  const h3s = [...detailHtml.matchAll(/<h3[^>]*>([\s\S]*?)<\/h3>/g)].map(m => m[1]);
  const afterSaleRateTitle = h3s.find(h => h.includes('月度售后率'));
  const afterSaleReasonTitle = h3s.find(h => h.includes('售后原因占比'));
  ok(afterSaleRateTitle && (afterSaleRateTitle.includes('UNPAIRED-PART') || afterSaleRateTitle.includes('产品一中文名')),
      '月度售后率标题应包含所选 SKU 或中文名');
  ok(afterSaleReasonTitle && (afterSaleReasonTitle.includes('UNPAIRED-PART') || afterSaleReasonTitle.includes('产品一中文名')),
      '售后原因占比标题应包含所选 SKU 或中文名');
  // 再点第二SKU确认两个标题跟随
  await bDetail.click({ 'data-bind': 'sku-row', 'data-i': '0' });
  const secondDetailHtml = bDetail.elements.main.innerHTML;
  const h3sSecond = [...secondDetailHtml.matchAll(/<h3[^>]*>([\s\S]*?)<\/h3>/g)].map(m => m[1]);
  const afterSaleRateTitleSecond = h3sSecond.find(h => h.includes('月度售后率'));
  const afterSaleReasonTitleSecond = h3sSecond.find(h => h.includes('售后原因占比'));
  ok(afterSaleRateTitleSecond && (afterSaleRateTitleSecond.includes('PAIRED-SKU-1') || afterSaleRateTitleSecond.includes('产品一中文名')),
      '切换后月度售后率标题应跟随新 SKU');
  ok(afterSaleReasonTitleSecond && (afterSaleReasonTitleSecond.includes('PAIRED-SKU-1') || afterSaleReasonTitleSecond.includes('产品一中文名')),
      '切换后售后原因占比标题应跟随新 SKU');

  // Regression: team denominators must be shop-unit sums, not team.units_known placeholders
  const uiRegressionErrors = [];
  const captureUiRegression = async (name, fn) => {
    try { await fn(); }
    catch (err) { uiRegressionErrors.push({ group: name, error: String(err && err.stack || err) }); }
  };

  await captureUiRegression('matrix_team_denominator_authority', async () => {
    const snap = clone(baseSnapshot());
    snap.teams.forEach((team) => { team.units_known = 999; });
    snap.teams[0].shops[0].months = { '2026-04': 0, '2026-05': null };
    snap.teams[0].shops[0].units_known = 1;
    snap.teams[0].shops[0].missing_months = [];
    const matrixBoard = await createBoard([response(snap)]);

    const parseGroupRows = () => [...matrixBoard.elements.main.innerHTML.matchAll(/<tr class="g"><td[^>]*>Amazon · (.*?) · 分母 ([0-9,，]+) · 未配对 ([^<]*)<\/td><\/tr>/g)]
      .map((m) => ({ name: m[1].trim(), denominator: Number(String(m[2]).replace(/[^0-9]/g, '')), unpaired: m[3].trim() }));

    let rows = parseGroupRows();
    ok(rows.length === 3, 'all view should render 3 Team group rows');
    const expectedTeamDenominators = { 'Team 1': 1, 'Team 2': 2, 'Team 3': 3 };
    rows.forEach((row) => {
      ok(expectedTeamDenominators[row.name] === row.denominator,
        `Team group ${row.name} denominator should be shop sum ${expectedTeamDenominators[row.name]}, got ${row.denominator}`);
    });
    ok(matrixBoard.elements.main.innerHTML.includes('产品销量分母：6'), 'all view summary should remain correct shop-sum denominator');
    ok(/<button[^>]*>—<\/button>/.test(matrixBoard.elements.main.innerHTML), 'confirmed snapshot missing rate should remain unknown, even when sales are zero');
    ok(/—/.test(matrixBoard.elements.main.innerHTML), 'matrix should preserve missing-month em dash');

    await matrixBoard.click({ 'data-bind': 'nav', 'data-view': 'Team 1' });
    rows = parseGroupRows();
    ok(rows.length === 1, 'one Team view should render one Team group row');
    ok(rows[0].name === 'Team 1' && rows[0].denominator === 1,
      `Team 1 one-view denominator should use shop sum 1, got ${JSON.stringify(rows[0])}`);
  });

  await captureUiRegression('detail_sku_scroll_containment', async () => {
    const snap = clone(baseSnapshot());
    const longA = {
      sku: 'SKU-LONG-' + 'A'.repeat(120),
      display_name: '很长很长中文名' + '名'.repeat(80),
      wf_part_number: 'WF-LONG-A-' + 'A'.repeat(120),
      sold: { '2026-04': 5, '2026-05': null }
    };
    const longB = {
      sku: 'SKU-LONG-' + 'B'.repeat(120),
      display_name: '另一个很长中文名' + '称'.repeat(80),
      wf_part_number: 'WF-LONG-B-' + 'B'.repeat(120),
      sold: { '2026-04': null, '2026-05': 7 }
    };
    snap.teams[0].shops[0].skus = [longA, longB];
    snap.teams[0].shops[0].months = { '2026-04': 5, '2026-05': 7 };
    snap.teams[0].shops[0].units_known = 12;
    snap.teams[0].shops[0].missing_months = [];
    const detailBoard = await createBoard([response(snap)]);
    await detailBoard.click({ 'data-bind': 'matrix-cell', 'data-shop': 'xingshan_US', 'data-month': '2026-04' });
    const detail = detailBoard.elements.main.innerHTML;

    const scrollStart = detail.indexOf('<div class="sku-table-scroll">');
    const tableStart = detail.indexOf('<table class="sk">');
    const tableEnd = tableStart >= 0 ? detail.indexOf('</table>', tableStart) : -1;
    const wrapperEnd = tableEnd >= 0 ? detail.indexOf('</div>', tableEnd) : -1;
    const rightColumn = detail.indexOf('<div><div class="card"><h3>①', tableEnd >= 0 ? tableEnd : 0);
    ok(scrollStart !== -1, 'detail should wrap table.sk in div.sku-table-scroll');
    ok(tableStart !== -1 && scrollStart !== -1 && scrollStart < tableStart, 'scroll wrapper should open before SKU table');
    ok(wrapperEnd !== -1 && rightColumn !== -1 && wrapperEnd < rightColumn, 'scroll wrapper should close before right chart column');
    ok(detail.includes(longA.sku), 'long synthetic SKU value should be retained');
    ok(detail.includes(longA.display_name), 'long synthetic Chinese name should be retained');
    ok(detail.includes('<th>SKU中文名称</th><th>SKU编码</th>'), 'matched Chinese SKU name should be the primary detail column');
    ok(detail.includes('已选 SKU中文名称'), 'selected product KPI should present the paired Chinese name first');
    ok(detail.includes(longA.wf_part_number), 'long synthetic WF part number should be retained');
    ok(detail.includes('data-bind="sku-row" data-i="0"') && detail.includes('data-bind="sku-row" data-i="1"'),
      'sku-row controls should remain for all rows');
    ok(detail.includes('搜索 SKU') && detail.includes('data-bind=\"sku-search\"'), 'SKU detail should offer a fuzzy search input');
    ok(detail.includes('模糊搜索中文名称、SKU编码或 WF Part Number'), 'search placeholder should explain searchable identities');
    ok(!detail.includes('<th>图表</th>') && !detail.includes('data-bind=\"sku-select\"'), 'remove chart checkboxes and selection controls from SKU table');
    ok(!detail.includes('只看当前 SKU') && !detail.includes('全选本店 SKU'), 'remove old SKU selection shortcuts');
    ok(detail.includes('③ ' + longA.display_name + ' (' + longA.sku + ') 月度销量'), 'chart should display the currently selected SKU only');
    await detailBoard.click({ 'data-bind': 'sku-row', 'data-i': '1' });
    const selectedDetail = detailBoard.elements.main.innerHTML;
    ok(selectedDetail.includes('① ' + longB.display_name + ' (' + longB.sku + ') 月度售后率'), 'clicking a search result row selects its SKU and history');
    ok(selectedDetail.includes('③ ' + longB.display_name + ' (' + longB.sku + ') 月度销量'), 'monthly sales chart should follow selected SKU');

    const htmlCss = fs.readFileSync(path.join(root, 'web', 'board.html'), 'utf8');
    ok(/\.sku-table-scroll\s*\{[^}]*overflow-x\s*:\s*auto[^}]*\}/.test(htmlCss), 'CSS .sku-table-scroll should set overflow-x auto');
    ok(/\.sku-table-scroll\s*\{[^}]*max-width\s*:\s*100%[^}]*\}/.test(htmlCss), 'CSS .sku-table-scroll should set max-width 100%');
    ok(/\.sku-table-scroll\s*\{[^}]*min-width\s*:\s*0[^}]*\}/.test(htmlCss), 'CSS .sku-table-scroll should set min-width 0');
    const skCssRules = htmlCss.match(/table\.sk[^{]*\{[^}]*\}/g) || [];
    const hasMinWidth = skCssRules.some((rule) => {
      const m = rule.match(/min-width\s*:\s*([0-9]+)px/);
      return m && Number(m[1]) >= 600;
    });
    ok(hasMinWidth, 'table.sk CSS should define min-width at least 600px');
    const skuNowrapRules = htmlCss.match(/[^{}]*table\.sk[^{}]*\{[^}]*white-space\s*:\s*nowrap[^}]*\}/g) || [];
    ok(skuNowrapRules.length > 0, 'numeric SKU columns should use white-space nowrap');
    ok(!/table\.sk\s*(?:th|td)\s*\{[^}]*white-space\s*:\s*nowrap[^}]*\}/.test(htmlCss),
      'text SKU columns should remain wrappable');
  });

  console.log(JSON.stringify({ synthetic_only: true, passed, failed: uiRegressionErrors.length, ui_regressions: uiRegressionErrors }));
  process.exitCode = uiRegressionErrors.length ? 1 : 0;
})();
