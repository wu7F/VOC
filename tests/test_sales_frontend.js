/* Synthetic DOM; verify zero and missing rates remain distinct for Feishu. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const code = fs.readFileSync(path.join(__dirname, '../web/app.js'), 'utf8');
const start = code.indexOf('function renderAsProducts(');
const end = code.indexOf('function renderAsCharts(', start);
const wrapper = { innerHTML: '' };
const context = { $: () => wrapper, esc: String };
vm.createContext(context);
vm.runInContext(code.slice(start, end), context);
const a = { mode: 'ledger', rate_basis: 'feishu_qty', overall_rate: 0,
  total_orders: 0, products: [
    { product: 'SYNTH-ZERO', total: 0, aftersale: 0, sales_qty: 25, rate: 0 },
    { product: 'SYNTH-MISSING', total: 1, aftersale: 1, sales_qty: 0, rate: null },
  ], shops: [] };
context.renderAsProducts(a, '');
assert.match(wrapper.innerHTML, /SYNTH-ZERO/);
assert.match(wrapper.innerHTML, /<b>0\.0%<\/b>/);
assert.match(wrapper.innerHTML, /SYNTH-MISSING/);
assert.match(wrapper.innerHTML, /<b>—<\/b>/);
assert.match(wrapper.innerHTML, /当月销量/);
let chartOption;
context.state = { charts: {} };
context.document = { getElementById: id => id === 'asRateChart' ? {} : null };
context.echarts = { init: () => ({ setOption: value => { chartOption = value; } }) };
const chartEnd = code.indexOf('function bindAftersaleUI(', end);
vm.runInContext(code.slice(end, chartEnd), context);
a.shops = [{ shop: 'Missing', rate: null, aftersale: 1 },
           { shop: 'Zero', rate: 0, aftersale: 0, sales_qty: 25 }];
context.renderAsCharts(a, '#000');
assert.equal(chartOption.series[0].data[0], 0);
assert.equal(chartOption.series[0].data[1], null);
assert.match(chartOption.tooltip.formatter([{ dataIndex: 1, name: 'Missing', value: null }]), /缺销量分母/);
assert.equal(chartOption.series[0].label.formatter({ value: null }), '—');
assert.equal(chartOption.series[0].label.formatter({ value: 0 }), '0.0%');
console.log(JSON.stringify({ synthetic_only: true, passed: 2, failed: 0,
  cases: ['Feishu table distinguishes zero and missing rates',
          'Feishu chart retains null and labels missing denominator'] }));
