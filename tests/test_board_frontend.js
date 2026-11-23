const assert = require('node:assert/strict');
const b = require('../web/board.js');

function baseSnapshot() {
  return {
    version: 1,
    ok: true,
    fetched_at: '2026-11-15T10:00:00+08:00',
    last_success_at: '2026-11-15T10:00:00+08:00',
    months: ['2026-04', '2026-05'],
    current_month: '2026-11',
    current_month_partial: true,
    source: { table_name: 'AMAZON销量数据', row_count: 2, page_count: 1 },
    teams: [{
      name: 'Team 1',
      shops: [{
        account: 'xingshan_US',
        display_name: 'xingshan_US',
        months: { '2026-04': null, '2026-05': 0 },
        units_known: 0,
        missing_months: [],
        skus: [{ sku: 'SKU-A', sold: { '2026-04': null, '2026-05': 0 } }]
      }]
    }],
    unmapped: [],
    other_platforms: [],
    aftersale: { connected: false, message: '售后分子和原因尚未接入多维表格,售后率显示“—”,不按 0% 计算' },
    wayfair: { connected: false, message: 'Wayfair 销量尚未接入' },
    diagnostics: [],
    stale: false,
    error: null
  };
}

let passed = 0;
function ok(cond) {
  assert.ok(cond);
  passed++;
}

ok(b.fmtUnits(null) === '—');
ok(b.fmtUnits(0) === '0');
ok(b.fmtUnits(1234) === '1,234');
ok(b.matchesSkuSearch({ display_name: '8018双人位猫抓皮黑色', sku: 'LAYJMZP8018BK-2L', wf_part_number: 'WF-8018' }, '猫抓皮黑'));
ok(b.matchesSkuSearch({ display_name: '8018双人位猫抓皮黑色', sku: 'LAYJMZP8018BK-2L', wf_part_number: 'WF-8018' }, 'layjmzp8018'));
ok(b.matchesSkuSearch({ display_name: '8018双人位猫抓皮黑色', sku: 'LAYJMZP8018BK-2L', wf_part_number: 'WF-8018' }, 'WF-80'));
ok(!b.matchesSkuSearch({ display_name: '8018双人位猫抓皮黑色', sku: 'LAYJMZP8018BK-2L', wf_part_number: 'WF-8018' }, '不同产品'));
ok(b.matchesSkuSearch({ sku: 'SKU-A' }, ''));

ok(b.esc('<script>').includes('&lt;script&gt;'));
ok(!b.esc('<script>').includes('<script>'));

const snap = baseSnapshot();
const matrix = b.renderMatrixHtml(snap, { view: 'all' });
ok(/<td class="na"[^>]*>—<\/td>/.test(matrix));
ok(matrix.includes('>—</button>'));
ok(!matrix.includes('>0</button>'));
ok(matrix.includes('Amazon 售后率'));
ok(matrix.includes('<strong>—</strong>'));
ok(b.renderBannerHtml(snap).includes('数据读取于 10:00:00'));
ok(b.renderBannerHtml(snap).includes('截至读取时点'));

const diagSnap = baseSnapshot();
diagSnap.unmapped = [{ account: 'unmapped_shop', display_name: 'unmapped_shop', rows: 1, units: 12 }];
diagSnap.diagnostics = [{ level: 'warning', code: 'unmapped_account', message: '账号 unmapped_shop 不在 TEAM_MEMBERS 映射中' }];
ok(!b.renderMatrixHtml(diagSnap, { view: 'all' }).includes('unmapped_shop'));
ok(b.renderDiagnosticsHtml(diagSnap).includes('unmapped_shop'));

const stale = b.renderStaleHtml('2026-11-15T10:00:00+08:00', '读取失败 test');
ok(stale.includes('2026-11-15T10:00:00+08:00'));
ok(stale.includes('读取失败 test'));

const detailSnap = baseSnapshot();
detailSnap.teams[0].shops[0].months = { '2026-04': 5, '2026-05': 9 };
detailSnap.teams[0].shops[0].units_known = 14;
detailSnap.teams[0].shops[0].skus = [{ sku: '<script>alert(1)</script>', sold: { '2026-04': 5, '2026-05': 9 } }];
const detail = b.renderDetailHtml(detailSnap, {
  view: 'Team 1',
  shop: 'xingshan_US',
  month: '2026-04',
  selectedSku: 0,
  skuSearch: 'alert'
});
ok(detail.includes('&lt;script&gt;'));
ok(!detail.includes('<script>alert'));

console.log(JSON.stringify({
  synthetic_only: true,
  passed,
  failed: 0,
  methods: ['fmtUnits', 'esc', 'renderMatrixHtml', 'renderDetailHtml', 'renderBannerHtml', 'renderDiagnosticsHtml', 'renderStaleHtml']
}));
