const assert = require('node:assert/strict');
const board = require('../web/board.js');
let passed=0;
function test(name,fn){fn();passed++;console.log('PASS '+name);}
function snapshot(){return {months:['2026-04'],products:[{sku:'P',display_name:'产品',t01:false}],
  aftersale:{connected:false},unmapped:[],diagnostics:[],teams:[{name:'Team 1',shops:[{account:'A',skus:[{sku:'P',sold:{'2026-04':8}}]}],
  unpaired_sku_totals:[{sku:'NONE',units:3,known_qty:3,has_unknown:false}],
  merged_sku_totals:[{sku:'R',target_sku:'P',units:100,known_qty:100,has_unknown:false},
    {sku:'M',target_sku:'P',units:2,known_qty:2,has_unknown:false}]}]};}
test('merged labels and unmatched details are separate and do not inflate warning count',()=>{
  const s=snapshot(), before=JSON.stringify(s), html=board.renderDiagnosticsHtml(s);
  assert(html.includes('已并入 SKU（组件原始件数）'));
  assert(html.includes('R → P（已并入 100 件）'));
  assert(html.includes('NONE（3 件）'));
  const unmatched=html.slice(html.indexOf('未配对 SKU 明细'),html.indexOf('已并入 SKU（组件原始件数）'));
  assert(!unmatched.includes('R → P') && !unmatched.includes('M → P'));
  assert(html.includes('数据诊断（0）'));
  assert(html.indexOf('R → P')<html.indexOf('M → P'));
  assert.equal(JSON.stringify(s),before);
});
test('raw audit quantities never become product-month sales',()=>{
  const result=board.aggregateProductMonths(snapshot());
  assert.equal(result.items[0].sales['2026-04'],8);
});
test('merged source target and Team names are escaped',()=>{
  const s=snapshot();s.teams[0].name='<b>T</b>';s.teams[0].merged_sku_totals[0].sku='<script>R</script>';
  s.teams[0].merged_sku_totals[0].target_sku='<img src=x>';
  const html=board.renderDiagnosticsHtml(s);
  assert(!html.includes('<script>')&&!html.includes('<img '));
  assert(html.includes('&lt;b&gt;T&lt;/b&gt;')&&html.includes('&lt;img'));
});
test('merged true zero and observed unknown remain distinct',()=>{
  const s=snapshot();s.teams[0].merged_sku_totals=[
    {sku:'ZERO',target_sku:'P',units:0,known_qty:0,has_unknown:false},
    {sku:'UNKNOWN',target_sku:'P',units:null,known_qty:3,has_unknown:true}];
  const html=board.renderDiagnosticsHtml(s);
  assert(html.includes('ZERO → P（已并入 0 件）'));
  assert(html.includes('UNKNOWN → P（已并入 3 件（有未知））'));
  assert(!html.includes('UNKNOWN → P（已并入 0 件）'));
});
test('legacy snapshot without merged metadata is still readable',()=>{
  const s=snapshot();delete s.teams[0].merged_sku_totals;
  const html=board.renderDiagnosticsHtml(s);
  assert(!html.includes('已并入 SKU')&&html.includes('NONE（3 件）'));
});
console.log(JSON.stringify({passed,synthetic_only:true}));
