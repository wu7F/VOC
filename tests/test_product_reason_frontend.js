const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const board = require('../web/board.js');
const code = fs.readFileSync(path.join(__dirname, '../web/board.js'), 'utf8');
const clone = x => JSON.parse(JSON.stringify(x));
const months = ['2026-04','2026-05','2026-06','2026-07','2026-08','2026-09','2026-10','2026-11'];
let passed = 0;
function test(name, run) { run(); passed++; console.log('PASS ' + name); }
function snapshot() {
  return {ok:true,months,current_month:'2026-11',products:[{sku:'S',display_name:'产品 S'},{sku:'ZERO'}],
    teams:[{name:'Team 1',shops:[{account:'A',skus:[{sku:'S',sold:{'2026-04':10}}],months:{'2026-04':10}}]},
      {name:'Team 2',shops:[{account:'B',skus:[{sku:'S',sold:{'2026-04':30}}],months:{'2026-04':30}}]},
      {name:'Team 3',shops:[]}],diagnostics:[],
    aftersale:{connected:true,sku_store_month:[
      {team:'Team 1',account:'A',sku:'S',month:'2026-04',numerator:5,denominator:10},
      {team:'Team 2',account:'B',sku:'S',month:'2026-04',numerator:3,denominator:30},
      {team:'Team 1',account:'A',sku:'ZERO',month:'2026-04',numerator:0,denominator:10}],
    reasons:{connected:true,items:[
      {account:'A',sku:'S',month:'2026-04',category:'其他问题',count:1,share:25},
      {account:'A',sku:'S',month:'2026-04',category:'破损损坏',count:3,share:75},
      {account:'B',sku:'S',month:'2026-04',category:'其他问题',count:9,share:90},
      {account:'B',sku:'S',month:'2026-04',category:'破损损坏',count:1,share:10},
      {account:'OUTSIDE',sku:'S',month:'2026-04',category:'不应出现',count:999,share:100},
      {account:'A',sku:'X',month:'2026-04',category:'不应出现',count:999,share:100},
      {account:'A',sku:'S',month:'2026-05',category:'另一月份',count:2,share:100}],
    subcategory_items:[
      {account:'A',sku:'S',month:'2026-04',category:'破损损坏',subcategory:'包装破损',count:1,share:33.3333},
      {account:'A',sku:'S',month:'2026-04',category:'破损损坏',subcategory:'断裂开裂',count:2,share:66.6667},
      {account:'B',sku:'S',month:'2026-04',category:'破损损坏',subcategory:'包装破损',count:1,share:100},
      {account:'OUTSIDE',sku:'S',month:'2026-04',category:'破损损坏',subcategory:'外平台',count:999,share:100}
    ]}}};
}
test('global reason shares use summed event counts, not average shares or SKU units', () => {
  const snap = snapshot(), before = JSON.stringify(snap);
  const result = board.aggregateProductReasons(snap,'S','2026-04');
  assert.equal(result.connected,true);
  assert.deepEqual(result.items.map(r=>[r.category,r.count]), [['其他问题',10],['破损损坏',4]]);
  assert(Math.abs(result.items[0].share-1000/14)<0.0001);
  assert(Math.abs(result.items[1].share-400/14)<0.0001);
  assert.equal(JSON.stringify(snap),before,'Aggregation must not mutate API snapshot');
});
test('secondary percentages use merged primary parent total', () => {
  const result = board.aggregateProductReasons(snapshot(),'S','2026-04');
  assert.deepEqual(result.subcategory_items.map(r=>[r.subcategory,r.count,r.share]).sort(),
    [['包装破损',2,50],['断裂开裂',2,50]].sort());
});
test('invalid counts and prototype-like labels remain safe', () => {
  const snap = snapshot();
  for (const count of [null,'6',false,-1,NaN,Infinity,0]) snap.aftersale.reasons.items.push({account:'A',sku:'S',month:'2026-04',category:'坏计数',count,share:100});
  snap.aftersale.reasons.items.push({account:'A',sku:'S',month:'2026-04',category:'__proto__',count:2,share:100});
  const result = board.aggregateProductReasons(snap,'S','2026-04');
  assert(!result.items.some(r=>r.category==='坏计数'));
  assert.equal(result.items.find(r=>r.category==='__proto__').count,2);
});
test('tooltip includes product/month/weighted rate and descending reason percentages', () => {
  const html = board.renderProductReasonTooltipHtml(snapshot(),'S','2026-04');
  assert(html.includes('产品 S') && html.includes('S') && html.includes('2026-04'));
  assert(html.includes('20.00%'),'Rate must remain 8 / 40, independent of 14 reason records');
  assert(html.includes('71.43%') && html.includes('28.57%'));
  assert(html.indexOf('其他问题')<html.indexOf('破损损坏'));
  assert(html.includes('<details') && html.includes('包装破损') && html.includes('50.00%'));
  assert(!html.includes('OUTSIDE') && !html.includes('不应出现') && !html.includes('另一月份'));
});
test('no events, disconnected and old API show dash without fabricated percentages', () => {
  let snap = snapshot();
  let html = board.renderProductReasonTooltipHtml(snap,'ZERO','2026-04');
  assert(html.includes('—') && html.includes('0.00%'),'A real zero rate may coexist with missing reason percentages');
  assert(!html.includes('71.43%'));
  snap.aftersale.reasons.connected=false;
  assert(board.renderProductReasonTooltipHtml(snap,'S','2026-04').includes('—'));
  delete snap.aftersale.reasons;
  assert(board.renderProductReasonTooltipHtml(snap,'S','2026-04').includes('—'));
});
test('missing secondary fields stay explicitly unconnected', () => {
  const snap = snapshot(); delete snap.aftersale.reasons.subcategory_items;
  const html = board.renderProductReasonTooltipHtml(snap,'S','2026-04');
  assert(html.includes('71.43%') && html.includes('二级原因未接入'));
});
test('tooltip escapes metadata and category labels', () => {
  const snap = snapshot(); snap.products[0].display_name='<img src=x onerror=evil()>';
  snap.aftersale.reasons.items[0].category='<script>evil()</script>';
  const html = board.renderProductReasonTooltipHtml(snap,'S','2026-04');
  assert(!html.includes('<img') && !html.includes('<script>'));
  assert(html.includes('&lt;img') && html.includes('&lt;script&gt;'));
});
test('Team views never render products even with stale or forged product state', () => {
  for (const view of ['Team 1','Team 2','Team 3']) {
    const snap = snapshot(); if(view==='Team 3') snap.teams[2].shops=[{account:'C',skus:[],months:{'2026-04':0}}];
    const html = board.renderMatrixHtml(snap,{view,matrixMode:'product'});
    assert(!html.includes('product-card') && !html.includes('data-mode="product"'));
    assert(html.includes('店铺×月份') || html.includes('matrix-cell'));
  }
});
test('Amazon overview keeps full product catalogue while other platforms remain placeholders', () => {
  const snap=snapshot();
  const productHtml=board.renderMatrixHtml(snap,{view:'all',matrixMode:'product'});
  assert(productHtml.includes('product-card') && productHtml.includes('kpis'),'Amazon product view keeps existing KPI cards');
  for(const view of ['Wayfair','Independent']) {
    const html=board.renderMatrixHtml(snap,{view,matrixMode:'product'});
    assert(html.includes('未接入') && !html.includes('<svg') && !html.includes('kpis'));
  }
});

async function interaction() {
  const elements={};
  for(const key of ['nav','main','banner','stale-banner']) elements[key]={innerHTML:'',querySelectorAll:()=>[]};
  const handlers={}, snap=snapshot(), queue=[snap], reads=[];
  const context={document:{hidden:true,getElementById:id=>elements[id],addEventListener:(event,cb)=>handlers[event]=cb},
    fetch:url=>{reads.push(url);return Promise.resolve({ok:true,json:()=>Promise.resolve(clone(queue.shift()))});},
    setInterval:()=>{},module:{exports:{}}};
  vm.runInNewContext(code,context);
  const flush=()=>new Promise(resolve=>setImmediate(resolve)); await flush();
  const click=async attrs=>{handlers.click({target:{closest:()=>({getAttribute:key=>attrs[key]||null})}});await flush();};
  const nav=elements.nav.innerHTML;
  assert.equal((nav.match(/data-bind="nav"/g)||[]).length,6);
  for(const title of ['Amazon 概览','Wayfair 概览','独立站概览']) assert.equal(nav.split('>'+title+'</button>').length-1,1);
  assert(!nav.includes('>全部</button>') && !nav.includes('>未接入</button>'));
  await click({'data-bind':'matrix-mode','data-mode':'product'});
  assert(elements.main.innerHTML.includes('product-card'));
  await click({'data-bind':'nav','data-view':'Team 1'});
  await click({'data-bind':'matrix-mode','data-mode':'product'});
  assert(!elements.main.innerHTML.includes('product-card'),'Forged mode must be ignored on Team');
  await click({'data-bind':'nav','data-view':'all'});
  await click({'data-bind':'matrix-mode','data-mode':'product'});
  assert(elements.main.innerHTML.includes('product-card'));
  assert(elements.main.innerHTML.includes('tabindex="0" role="button"'));
  const pointHandlers={}, chartHandlers={}, tipClasses=new Set();
  const tip={style:{display:'none'},innerHTML:'',
    classList:{add:name=>tipClasses.add(name),remove:name=>tipClasses.delete(name)},
    matches:()=>false,getBoundingClientRect:()=>({width:284,height:200})};
  let selectedMonth='2026-04';
  const chart={getAttribute:name=>name==='data-sku'?'S':name==='data-chart'?'rate':null,
    hasAttribute:name=>name==='data-product-chart',
    querySelector:()=>tip,addEventListener:(event,cb)=>chartHandlers[event]=cb,
    getBoundingClientRect:()=>({left:0,top:0,width:300,height:250}),
    contains:node=>node===tip};
  const circle={closest:selector=>selector==='[data-chart]'||selector==='[data-product-chart]'?chart:null,
    getAttribute:name=>name==='data-month'?selectedMonth:name==='data-series'?'S':name==='data-value'?'20':null,
    getBoundingClientRect:()=>({left:280,top:80,width:8,height:8}),
    addEventListener:(event,cb)=>pointHandlers[event]=cb};
  elements.main.querySelectorAll=selector=>selector==='[data-chart] circle'?[circle]:
    selector==='[data-product-chart]'?[chart]:selector==='.tip.product-tip'?[tip]:[];
  await click({'data-bind':'nav','data-view':'all'});
  pointHandlers.mouseenter();
  assert(tip.innerHTML.includes('71.43%') && tip.innerHTML.includes('20.00%'));
  assert.equal(tip.style.display,'block');
  assert(parseFloat(tip.style.left)>=0 && parseFloat(tip.style.left)+284<=300,'Tooltip stays inside a 300px chart');
  pointHandlers.mouseleave();
  assert.equal(tip.style.display,'block','Leaving point keeps tooltip reachable for secondary expansion');
  chartHandlers.mouseleave();
  assert.equal(tip.style.display,'none','Leaving chart closes unpinned hover tooltip');
  passed++; console.log('PASS actual hover, placement and pointer transfer events');

  const event=()=>({preventDefault(){this.prevented=true;},stopPropagation(){this.stopped=true;}});
  pointHandlers.focus(); assert.equal(tip.style.display,'block');
  chartHandlers.focusout({relatedTarget:tip}); assert.equal(tip.style.display,'block');
  chartHandlers.focusout({relatedTarget:null}); assert.equal(tip.style.display,'none');
  const touch=event(); pointHandlers.click(touch);
  assert(touch.stopped && touch.prevented && tipClasses.has('pinned'));
  chartHandlers.mouseleave(); assert.equal(tip.style.display,'block');
  selectedMonth='2026-05'; pointHandlers.click(event());
  assert(tip.innerHTML.includes('2026-05') && tip.innerHTML.includes('另一月份') && !tip.innerHTML.includes('71.43%'));
  assert.equal(tip.style.display,'block','Touching another month in same chart updates pinned tooltip');
  chartHandlers.click({...event(),target:{closest:selector=>selector==='.tip-close'?{}:null}});
  assert.equal(tip.style.display,'none');
  pointHandlers.focus();
  handlers.keydown({key:'Escape'}); assert.equal(tip.style.display,'none');
  const enter={...event(),key:'Enter'}; pointHandlers.keydown(enter);
  assert(enter.prevented && tipClasses.has('pinned'));
  handlers.keydown({key:'Escape'}); assert.equal(tip.style.display,'none');
  const summaryClick={...event(),target:{closest:()=>null}}; handlers.click(summaryClick);
  assert(!summaryClick.prevented,'Secondary details native toggle must not be cancelled');
  passed++; console.log('PASS actual touch pin, second month, close, focus, Escape and native details events');
  assert(reads.every(url=>url==='/api/bitable/board' || url==='/api/bitable/board?refresh=1'));
  passed++;
  console.log('PASS actual navigation events and readonly endpoint');
}
interaction().then(()=>console.log(JSON.stringify({synthetic_only:true,passed}))).catch(error=>{console.error(error);process.exitCode=1;});
