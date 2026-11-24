const assert = require('node:assert/strict');
const board = require('../web/board.js');
let passed = 0;
function test(name, fn) { fn(); passed++; console.log('PASS ' + name); }
function snap() {
  return {teams:[{name:'Team 1',unpaired_skus:['SMALL','BIG','ZERO','UNKNOWN'],unpaired_sku_totals:[
    {sku:'SMALL',units:2,known_qty:2,has_unknown:false},
    {sku:'UNKNOWN',units:null,known_qty:3,has_unknown:true},
    {sku:'ZERO',units:0,known_qty:0,has_unknown:false},
    {sku:'BIG',units:12,known_qty:12,has_unknown:false}]},
    {name:'Team 2',unpaired_skus:['SECOND'],unpaired_sku_totals:[{sku:'SECOND',units:4,known_qty:4,has_unknown:false}]},
    {name:'Team 3',unpaired_skus:[],unpaired_sku_totals:[]}],diagnostics:[],unmapped:[]};
}
test('grouped totals show actual SKU names and known values in descending order',()=>{
  const input=snap(), before=JSON.stringify(input), h=board.renderDiagnosticsHtml(input);
  assert(h.includes('Team 1') && h.includes('Team 2'));
  assert(h.includes('BIG（12 件）') && h.includes('SMALL（2 件）') && h.includes('ZERO（0 件）'));
  assert(h.indexOf('BIG（12 件）')<h.indexOf('SMALL（2 件）'));
  assert(h.indexOf('SMALL（2 件）')<h.indexOf('ZERO（0 件）'));
  assert(h.indexOf('ZERO（0 件）')<h.indexOf('UNKNOWN'));
  assert(h.indexOf('UNKNOWN')<h.indexOf('Team 2'));
  assert.equal(JSON.stringify(input),before,'Sorting cannot mutate the cached API response');
});
test('unknown totals stay unknown while real zero remains zero',()=>{
  const h=board.renderDiagnosticsHtml(snap());
  const unknown=h.slice(h.indexOf('UNKNOWN'),h.indexOf('Team 2'));
  assert(unknown.includes('—') && !unknown.includes('UNKNOWN（0 件）'));
  assert(h.includes('ZERO（0 件）'));
});
test('three warnings plus 87 SKU details do not become 90 diagnostic issues',()=>{
  const s={unmapped:[],diagnostics:[1,2,3].map(i=>({level:'warning',code:'unpaired_summary',message:'warning-'+i})),
    teams:[{name:'Team 1',unpaired_skus:Array.from({length:87},(_,i)=>'SKU-'+i),
    unpaired_sku_totals:Array.from({length:87},(_,i)=>({sku:'SKU-'+i,units:i,known_qty:i,has_unknown:false}))}]};
  const h=board.renderDiagnosticsHtml(s);
  assert(h.includes('数据诊断（3）') && !h.includes('数据诊断（90）'));
  for(let i=1;i<=3;i++) assert(h.includes('warning-'+i));
  assert(h.includes('SKU-86（86 件）') && h.includes('SKU-0（0 件）'));
});
test('older string-array API remains readable without guessing counts',()=>{
  const s={teams:[{name:'Team 1',unpaired_skus:['OLD-SKU']}],diagnostics:[],unmapped:[]};
  const h=board.renderDiagnosticsHtml(s);
  assert(h.includes('OLD-SKU') && h.includes('件数未提供'));
  assert(!h.includes('OLD-SKU（0 件）'));
});
test('Team and SKU labels are escaped in current and legacy fields',()=>{
  const s=snap(); s.teams[0].name='<script>bad()</script>';
  s.teams[0].unpaired_sku_totals[0].sku='<img src=x onerror=bad()>';
  let h=board.renderDiagnosticsHtml(s);
  assert(!h.includes('<script>') && !h.includes('<img ') && h.includes('&lt;img'));
  delete s.teams[0].unpaired_sku_totals; s.teams[0].unpaired_skus=['<b>old</b>'];
  h=board.renderDiagnosticsHtml(s); assert(h.includes('&lt;b&gt;old&lt;/b&gt;'));
});
test('unmapped accounts count as one summary row; empty diagnostics stay empty',()=>{
  assert.equal(board.renderDiagnosticsHtml({teams:[],diagnostics:[],unmapped:[]}), '');
  const h=board.renderDiagnosticsHtml({teams:[],diagnostics:[],unmapped:[{account:'A',rows:1,units:2},{account:'B',rows:1,units:3}]});
  assert(h.includes('数据诊断（1）') && h.includes('A') && h.includes('B'));
});
test('empty new-field arrays do not render empty Team groups',()=>{
  const h=board.renderDiagnosticsHtml({teams:[{name:'EMPTY',unpaired_skus:[],unpaired_sku_totals:[]}],diagnostics:[{level:'info',message:'check'}]});
  assert(h.includes('数据诊断（1）') && !h.includes('EMPTY'));
});
console.log(JSON.stringify({synthetic_only:true,passed}));
