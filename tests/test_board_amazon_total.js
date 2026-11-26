'use strict';

// Public-renderer regression tests. No browser, packages, provider reads, or live data.
// Optional historical check:
// node tests/test_board_amazon_total.js --replay <actual-api-replay.json> --reference <arithmetic-reference.json>
// The equivalent environment variables are AMAZON_TOTAL_REPLAY and AMAZON_TOTAL_REFERENCE.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const board = require('../web/board.js');

let scenarios = 0;
let passed = 0;
function t(name, fn) {
  scenarios += 1;
  try {
    fn();
    passed += 1;
    console.log('ok', scenarios, name);
  } catch (error) {
    process.exitCode = 1;
    console.error('fail', scenarios, name);
    console.error(error.stack || error.message);
  }
}

function copy(value) {
  if (Array.isArray(value)) return value.map(copy);
  if (value && typeof value === 'object') {
    return Object.fromEntries(Object.keys(value).map(key => [key, copy(value[key])]));
  }
  return value;
}

function freeze(value) {
  if (value && typeof value === 'object' && !Object.isFrozen(value)) {
    Object.values(value).forEach(freeze);
    Object.freeze(value);
  }
  return value;
}

const MONTHS = ['2026-04', '2026-05'];
function fixture() {
  const shop = account => ({
    account,
    display_name: '同名店铺',
    units_known: 900000,
    months: { '2026-04': 600000, '2026-05': 300000 },
    skus: []
  });
  // These actual counts have a deliberately unequal distribution:
  // April = (1 + 9 + 0 + 0)/(2 + 98 + 100 + 0) = 5.00%.
  // May = (0 + 1 + 2 + 0)/(25 + 10 + 40 + 25) = 3.00%.
  // Neither an average of shop/Team rates nor the gross sales yields this answer.
  const counts = [
    ['a', '2026-04', 1, 2], ['b', '2026-04', 9, 98],
    ['c', '2026-04', 0, 100], ['d', '2026-04', 0, 0],
    ['a', '2026-05', 0, 25], ['b', '2026-05', 1, 10],
    ['c', '2026-05', 2, 40], ['d', '2026-05', 0, 25]
  ];
  return {
    ok: true,
    platform: 'Amazon',
    months: MONTHS.slice(),
    products: [],
    teams: [
      { name: 'Team 1', shops: [shop('a'), shop('b')], unpaired_units: 0 },
      { name: 'Team 2', shops: [shop('c')], unpaired_units: 0 },
      { name: 'Team 3', shops: [shop('d')], unpaired_units: 0 }
    ],
    aftersale: {
      connected: true,
      // Intentionally inconsistent precomputed rates/totals must not drive the row.
      total: { numerator: 900, denominator: 9999, rate: 99 },
      teams: ['Team 1', 'Team 2', 'Team 3'].map(name => ({ name, numerator: 50, denominator: 1000, rate: 99 })),
      stores: [],
      sku_store_month: [{ account: 'a', sku: 'irrelevant', month: '2026-04', numerator: 99, denominator: 999 }],
      store_month: counts.map(([account, month, numerator, denominator]) => ({
        account, month, numerator, denominator, rate: 99
      }))
    }
  };
}

function attributes(source) {
  const result = {};
  const pattern = /([^\s=/>]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+)))?/g;
  let match;
  while ((match = pattern.exec(source))) {
    result[match[1].toLowerCase()] = match[2] ?? match[3] ?? match[4] ?? '';
  }
  return result;
}

function hasClass(attrs, name) {
  return String(attrs.class || '').split(/\s+/).includes(name);
}

function textContent(html) {
  return html.replace(/<[^>]*>/g, '')
    .replace(/&nbsp;|&#160;/g, ' ')
    .replace(/&mdash;|&#8212;|&#x2014;/gi, '—')
    .replace(/&amp;/g, '&')
    .trim();
}

function rowsIn(html) {
  return [...html.matchAll(/<tr\b([^>]*)>([\s\S]*?)<\/tr>/gi)].map(match => ({
    attrs: attributes(match[1]),
    html: match[0],
    inner: match[2]
  }));
}

function totalRows(html) {
  return rowsIn(html).filter(row => hasClass(row.attrs, 'amazon-total'));
}

function render(snapshot, state = { view: 'all', matrixMode: 'store' }) {
  return board.renderMatrixHtml(snapshot, state);
}

function readTotal(snapshot, state) {
  const html = render(snapshot, state);
  const totals = totalRows(html);
  assert.equal(totals.length, 1, 'one Amazon total row is rendered');
  const row = totals[0];
  const tables = [...html.matchAll(/<table\b([^>]*)>([\s\S]*?)<\/table>/gi)];
  const matrix = tables.find(table => hasClass(attributes(table[1]), 'hm'));
  assert.ok(matrix, 'the store matrix table exists');
  const tbody = matrix[2].match(/<tbody\b[^>]*>([\s\S]*?)<\/tbody>/i);
  assert.ok(tbody, 'the matrix has a tbody');
  const matrixRows = rowsIn(tbody[1]);
  assert.ok(matrixRows.length > 0, 'the matrix has body rows');
  assert.ok(hasClass(matrixRows[0].attrs, 'amazon-total'), 'Amazon total is the first tbody row, before Team/store rows');

  const cells = [...row.inner.matchAll(/<(th|td)\b([^>]*)>([\s\S]*?)<\/\1>/gi)].map(match => ({
    tag: match[1].toLowerCase(), attrs: attributes(match[2]),
    text: textContent(match[3].replace(/<small\b[^>]*>[\s\S]*?<\/small>/gi, '')),
    counts: textContent((match[3].match(/<small\b[^>]*>([\s\S]*?)<\/small>/i) || [null, ''])[1])
  }));
  assert.equal(cells.length, snapshot.months.length + 2, 'label, all displayed months, and cumulative denominator have separate cells');
  assert.equal(cells[0].tag, 'th', 'the row label is a header cell');
  assert.equal(cells[0].attrs.scope, 'row', 'the label identifies the entire row');
  assert.equal(cells[0].text, 'Amazon 合计');
  const monthCells = cells.slice(1, -1);
  assert.deepEqual(monthCells.map(cell => cell.attrs['data-month']), snapshot.months, 'month cells follow snapshot.months exactly');
  assert.ok(monthCells.every(cell => cell.tag === 'td'), 'month rates are data cells');
  const sales = cells[cells.length - 1];
  assert.ok(hasClass(sales.attrs, 'amazon-total-sales'), 'cumulative denominator has its public class');
  return { counts: monthCells.map(cell => cell.counts), rates: monthCells.map(cell => cell.text), cumulative: sales.text, html, row: row.html };
}

function expectTotal(snapshot, rates, cumulative, state) {
  const result = readTotal(snapshot, state);
  assert.deepEqual(result.rates, rates);
  assert.equal(result.cumulative.replace(/[,，\s\u00a0\u202f]/g, ''), String(cumulative));
  return result;
}

function expectAprilGap(snapshot) {
  return expectTotal(snapshot, ['—', '3.00%'], '—');
}

t('public store renderer adds one accessible first row with weighted counts and the actual cumulative denominator', () => {
  expectTotal(fixture(), ['5.00%', '3.00%'], 300);
});

t('the default matrix mode is the existing store mode', () => {
  expectTotal(fixture(), ['5.00%', '3.00%'], 300, { view: 'all' });
});

t('an Amazon snapshot without an explicit platform retains the same total row', () => {
  const snapshot = fixture();
  delete snapshot.platform;
  expectTotal(snapshot, ['5.00%', '3.00%'], 300);
});

t('month order follows the snapshot and is not hardcoded or sorted', () => {
  const snapshot = fixture();
  snapshot.months.reverse();
  expectTotal(snapshot, ['3.00%', '5.00%'], 300);
});

t('only displayed months contribute to the cumulative denominator', () => {
  const snapshot = fixture();
  snapshot.months = ['2026-04'];
  snapshot.aftersale.store_month.push({ account: 'a', month: '2026-12', numerator: 1000, denominator: 9000 });
  expectTotal(snapshot, ['5.00%'], 200);
});

t('gross unknown sales and units_known cannot replace valid aftersale counts', () => {
  const snapshot = fixture();
  snapshot.teams.forEach(team => team.shops.forEach(shop => {
    shop.units_known = null;
    shop.months = { '2026-04': null };
  }));
  expectTotal(snapshot, ['5.00%', '3.00%'], 300);
});

t('snapshot shop membership excludes Team 4 and unknown accounts even when rows claim a required Team', () => {
  const snapshot = fixture();
  snapshot.teams.push({ name: 'Team 4', shops: [{ account: 'outsider', months: {}, units_known: 999999 }] });
  for (const month of snapshot.months) {
    snapshot.aftersale.store_month.push(
      { account: 'outsider', team: 'Team 1', month, numerator: 9000, denominator: 10000 },
      { account: 'unknown', team: 'Team 3', month, numerator: 9000, denominator: 10000 }
    );
  }
  snapshot.aftersale.store_month[0].team = 'Team 4';
  expectTotal(snapshot, ['5.00%', '3.00%'], 300);
});

t('repeated account membership within and across Teams is counted once', () => {
  const snapshot = fixture();
  snapshot.teams[0].shops.push(copy(snapshot.teams[0].shops[1]));
  snapshot.teams[1].shops.push(copy(snapshot.teams[0].shops[0]));
  expectTotal(snapshot, ['5.00%', '3.00%'], 300);
});

t('store aliases and account keys both identify the same required shops', () => {
  const snapshot = fixture();
  snapshot.aftersale.store_month.forEach((row, index) => {
    if (index % 2 === 0) {
      row.store = row.account;
      delete row.account;
    }
  });
  expectTotal(snapshot, ['5.00%', '3.00%'], 300);
});

t('row order does not affect rates or the cumulative denominator', () => {
  const snapshot = fixture();
  snapshot.teams.reverse();
  snapshot.teams.forEach(team => team.shops.reverse());
  snapshot.aftersale.store_month.reverse();
  expectTotal(snapshot, ['5.00%', '3.00%'], 300);
});

t('one missing required shop-month leaves that month and cumulative denominator unknown', () => {
  const snapshot = fixture();
  snapshot.aftersale.store_month = snapshot.aftersale.store_month.filter(row => !(row.account === 'c' && row.month === '2026-04'));
  expectAprilGap(snapshot);
});

t('all rows absent for a month cannot become a measured zero', () => {
  const snapshot = fixture();
  snapshot.aftersale.store_month = snapshot.aftersale.store_month.filter(row => row.month !== '2026-04');
  expectAprilGap(snapshot);
});

for (const teamName of ['Team 1', 'Team 2', 'Team 3']) {
  t(`the total is unknown when required ${teamName} is absent`, () => {
    const snapshot = fixture();
    snapshot.teams = snapshot.teams.filter(team => team.name !== teamName);
    expectTotal(snapshot, ['—', '—'], '—');
  });
  t(`the total is unknown when required ${teamName} has no shops`, () => {
    const snapshot = fixture();
    snapshot.teams.find(team => team.name === teamName).shops = [];
    expectTotal(snapshot, ['—', '—'], '—');
  });
}

for (const disconnected of [false, null, undefined]) {
  t(`aftersale connected=${String(disconnected)} cannot expose a partial total`, () => {
    const snapshot = fixture();
    snapshot.aftersale.connected = disconnected;
    expectTotal(snapshot, ['—', '—'], '—');
  });
}

t('an absent aftersale snapshot renders unknown total cells', () => {
  const snapshot = fixture();
  delete snapshot.aftersale;
  expectTotal(snapshot, ['—', '—'], '—');
});

for (const storeMonth of [null, undefined, []]) {
  t(`missing store-month data (${String(storeMonth)}) renders unknown cells`, () => {
    const snapshot = fixture();
    snapshot.aftersale.store_month = storeMonth;
    expectTotal(snapshot, ['—', '—'], '—');
  });
}

const invalidCounts = [
  ['null', null], ['undefined', undefined], ['empty string', ''],
  ['numeric string zero', '0'], ['numeric string positive', '3'],
  ['boolean true', true], ['boolean false', false],
  ['NaN', NaN], ['positive infinity', Infinity], ['negative infinity', -Infinity],
  ['negative count', -1]
];
for (const field of ['numerator', 'denominator']) {
  for (const [description, value] of invalidCounts) {
    t(`a required ${field} that is ${description} invalidates the month and cumulative total`, () => {
      const snapshot = fixture();
      snapshot.aftersale.store_month[0][field] = value;
      expectAprilGap(snapshot);
    });
  }
  t(`an absent required ${field} invalidates the month and cumulative total`, () => {
    const snapshot = fixture();
    delete snapshot.aftersale.store_month[0][field];
    expectAprilGap(snapshot);
  });
}

t('explicit 0/0 rows are complete counts, with unknown rates and a measured cumulative zero', () => {
  const snapshot = fixture();
  snapshot.aftersale.store_month.forEach(row => {
    row.numerator = 0;
    row.denominator = 0;
    row.rate = 0;
  });
  expectTotal(snapshot, ['—', '—'], 0);
});

t('zero numerators over positive denominators display an explicit 0.00%', () => {
  const snapshot = fixture();
  snapshot.aftersale.store_month.forEach(row => { row.numerator = 0; });
  expectTotal(snapshot, ['0.00%', '0.00%'], 300);
});

t('one complete zero-denominator month does not invalidate known denominators in other months', () => {
  const snapshot = fixture();
  snapshot.aftersale.store_month.filter(row => row.month === '2026-04').forEach(row => {
    row.numerator = 0;
    row.denominator = 0;
  });
  expectTotal(snapshot, ['—', '3.00%'], 100);
});

t('rendering preserves the complete snapshot and accepts deeply frozen input', () => {
  const snapshot = fixture();
  const before = copy(snapshot);
  freeze(snapshot);
  expectTotal(snapshot, ['5.00%', '3.00%'], 300);
  assert.deepEqual(snapshot, before);
});

for (const view of ['Team 1', 'Team 2', 'Team 3', 'Wayfair', 'Independent']) {
  t(`Amazon total is absent from the ${view} view`, () => {
    assert.equal(totalRows(render(fixture(), { view, matrixMode: 'store' })).length, 0);
  });
}

t('Amazon total is absent from Amazon product mode', () => {
  assert.equal(totalRows(render(fixture(), { view: 'all', matrixMode: 'product' })).length, 0);
});

for (const view of ['Wayfair', 'all']) {
  t(`a Wayfair snapshot in ${view} view cannot display an Amazon total`, () => {
    const snapshot = fixture();
    snapshot.platform = 'Wayfair';
    snapshot.teams = [{ name: 'Wayfair', shops: [snapshot.teams[0].shops[0]] }];
    assert.equal(totalRows(render(snapshot, { view, matrixMode: 'store' })).length, 0);
  });
}

t('monthly rate cells visibly show the same-month summed numerator and denominator', () => {
  assert.deepEqual(readTotal(fixture()).counts, ['10/200', '3/100']);
});

t('missing monthly counts show no invented numerator or denominator', () => {
  const snapshot = fixture();
  snapshot.aftersale.store_month.shift();
  const result = expectAprilGap(snapshot);
  assert.deepEqual(result.counts, ['', '3/100']);
});

t('explicit zero counts remain visible below unknown zero-denominator rates', () => {
  const snapshot = fixture();
  snapshot.aftersale.store_month.forEach(row => { row.numerator = 0; row.denominator = 0; });
  assert.deepEqual(expectTotal(snapshot, ['—', '—'], 0).counts, ['0/0', '0/0']);
});

const supplied = {};
for (let index = 2; index < process.argv.length; index += 2) {
  const option = process.argv[index];
  if (!['--replay', '--reference'].includes(option) || !process.argv[index + 1]) {
    throw new Error('Usage: node test_board_amazon_total.js [--replay <path> --reference <path>]');
  }
  supplied[option.slice(2)] = process.argv[index + 1];
}
const replayPath = supplied.replay || process.env.AMAZON_TOTAL_REPLAY;
const referencePath = supplied.reference || process.env.AMAZON_TOTAL_REFERENCE;
if (replayPath || referencePath) {
  t('historical safe replay reconciles all eight actual monthly fractions, 641 cases, and 7327 denominators', () => {
    assert.ok(replayPath && referencePath, 'supply both historical --replay and --reference paths');
    const replayBytes = fs.readFileSync(path.resolve(replayPath));
    const snapshot = JSON.parse(replayBytes.toString('utf8').replace(/^\uFEFF/, ''));
    const reference = JSON.parse(fs.readFileSync(path.resolve(referencePath), 'utf8').replace(/^\uFEFF/, ''));
    assert.equal(crypto.createHash('sha256').update(replayBytes).digest('hex'), reference.source.sha256, 'reference and replay identify the same historical fixture');
    assert.equal(reference.source.kind, 'existing_historical_safe_aggregate_replay_not_production_measurement');
    // Fixed independently supplied reference fractions: do not derive expected rates
    // from application helpers or precomputed snapshot rates.
    const expected = [
      ['2026-04', 84, 1024, '8.20%'],
      ['2026-05', 96, 912, '10.53%'],
      ['2026-06', 70, 1197, '5.85%'],
      ['2026-07', 100, 790, '12.66%'],
      ['2026-08', 88, 855, '10.29%'],
      ['2026-09', 79, 937, '8.43%'],
      ['2026-10', 91, 962, '9.46%'],
      ['2026-11', 33, 650, '5.08%']
    ];
    assert.deepEqual(snapshot.months, expected.map(row => row[0]));
    assert.deepEqual(reference.selection.months, snapshot.months);
    const eligibleAccounts = new Set();
    for (const name of ['Team 1', 'Team 2', 'Team 3']) {
      const team = snapshot.teams.find(item => item.name === name);
      assert.ok(team && team.shops.length, `${name} is present in the replay`);
      team.shops.forEach(shop => eligibleAccounts.add(shop.account));
    }
    assert.equal(eligibleAccounts.size, 8);
    let numeratorTotal = 0;
    let denominatorTotal = 0;
    for (const [month, numerator, denominator, rate] of expected) {
      const rows = snapshot.aftersale.store_month.filter(row =>
        row.month === month && eligibleAccounts.has(row.account || row.store));
      assert.equal(rows.length, 8, `${month}: all eight required account rows exist`);
      assert.equal(new Set(rows.map(row => row.account || row.store)).size, 8, `${month}: rows identify distinct accounts`);
      assert.ok(rows.every(row => [row.numerator, row.denominator].every(value =>
        typeof value === 'number' && Number.isFinite(value) && value >= 0)), `${month}: counts are real finite nonnegative numbers`);
      assert.equal(rows.reduce((sum, row) => sum + row.numerator, 0), numerator, `${month}: actual numerator sum`);
      assert.equal(rows.reduce((sum, row) => sum + row.denominator, 0), denominator, `${month}: actual denominator sum`);
      const referenceMonth = reference.monthly.find(row => row.month === month);
      assert.ok(referenceMonth, `${month}: arithmetic reference exists`);
      assert.equal(referenceMonth.numerator, numerator);
      assert.equal(referenceMonth.denominator, denominator);
      assert.equal(referenceMonth.rate_percent_2dp.toFixed(2) + '%', rate);
      numeratorTotal += numerator;
      denominatorTotal += denominator;
    }
    assert.equal(numeratorTotal, 641);
    assert.equal(denominatorTotal, 7327);
    assert.equal(reference.totals.aftersale_store_month_sum.numerator, 641);
    assert.equal(reference.totals.aftersale_store_month_sum.denominator, 7327);
    const grossKnown = snapshot.teams.reduce((sum, team) =>
      sum + team.shops.reduce((teamSum, shop) => teamSum + shop.units_known, 0), 0);
    assert.equal(grossKnown, 7479, 'gross sales differ from eligible aftersale denominators');
    const before = copy(snapshot);
    const rendered = expectTotal(snapshot, expected.map(row => row[3]), 7327);
    assert.deepEqual(rendered.counts, expected.map(row => `${row[1]}/${row[2]}`), 'visible monthly fractions match actual same-month counts');
    assert.deepEqual(snapshot, before, 'historical snapshot is not mutated');
  });
} else {
  console.log('skip historical safe replay: supply --replay and --reference (or AMAZON_TOTAL_REPLAY and AMAZON_TOTAL_REFERENCE)');
}

console.log(`${passed}/${scenarios} Amazon total regression scenarios passed`);
