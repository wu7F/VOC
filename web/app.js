/* ==========================================================================
   VOC洞察平台 · 前端应用
   ========================================================================== */

const PALETTE = ['#2f6df6', '#6d4df6', '#0ea5e9', '#14b8a6', '#f59e0b',
                 '#ef4444', '#8b5cf6', '#10b981', '#f97316', '#64748b'];
const C_POS = '#16a34a', C_NEG = '#e5484d', C_NEU = '#94a3b8';
// 购买人群统一配色（全站一致）
const C_PURCHASED = '#2f6df6', C_NOT_PURCHASED = '#f59e0b';

const AXIS_LINE = { lineStyle: { color: '#e5e7eb' } };
const AXIS_LABEL = { color: '#8b93a1', fontSize: 11 };
const SPLIT_LINE = { lineStyle: { color: '#f1f2f4' } };
const TOOLTIP = {
  backgroundColor: 'rgba(255,255,255,.98)',
  borderColor: '#e5e7eb', borderWidth: 1,
  textStyle: { color: '#15181d', fontSize: 12 },
  extraCssText: 'box-shadow:0 8px 24px -8px rgba(16,24,40,.18);border-radius:8px;padding:9px 12px;'
};

const PLAT_COLOR = { Amazon: '#FF9900', Wayfair: '#7B189F', Reddit: '#FF4500' };
const DIM_ORDER = ['user_personas', 'usage_scenarios', 'pre_purchase_concerns',
                   'post_purchase_experience', 'unmet_needs'];

/* 结果页六个板块（顺序即导出 PDF 的顺序） */
const ALL_TABS = ['overview', 'dimensions', 'features', 'persona', 'voice', 'report'];
const TAB_TITLES = {
  overview: '一、概览',
  dimensions: '二、六维分析',
  features: '三、功能体验',
  persona: '四、人群与交叉',
  voice: '五、数据原声',
  report: '六、分析报告',
};
const REC_SECTION_LABELS = ['概览', '六维', '功能', '人群', '原声', '报告'];
const REC_SECTION_FULL = '概览 / 六维分析 / 功能体验 / 人群与交叉 / 数据原声 / 报告';

const state = {
  platforms: [],
  platform: 'Amazon',
  group: '产品VOC',
  view: 'setup',     // setup | progress | result | records
  datasets: [],
  current: null,     // 当前数据集记录
  result: null,      // 当前结果 JSON
  reportMd: '',
  tab: 'overview',
  files: [],         // 待上传文件
  charts: {},        // elId -> echarts instance
  noAnim: false,     // 导出时关闭图表动画，保证一次画到终态
  exporting: false,  // 导出进行中（防重复点击）
  voiceFilter: { q: '', purchase: '', dim: '', platform: '', tag: '', persona: '', brand: '' },
  engineList: [],
  engineCfg: null,
  dsSort: 'desc',        // 记录排序：desc = 最新在前
  dsFilter: 'all',       // 记录平台筛选（仅「全部平台」视图下生效）
  recQuery: '',          // 记录板块搜索词
  recScope: 'platform',  // 记录板块范围：platform = 当前平台 | all = 全部平台
  maintenance: null,     // 磁盘残留统计 {orphan_dirs, orphan_uploads, ...}
  build: 0,              // 前端构建号（= web 静态文件最新修改时间），用于检测服务重启后的版本漂移
  selMode: false,        // 记录多选模式（侧边栏与记录板块共用）
  selected: new Set(),   // 已勾选的记录 id（共用）
  pdfEngine: true,       // 服务端 PDF 引擎是否可用
  auth: { enabled: false, account_count: 0 },
  email: null,            // 当前登录账号（邮箱）；null = 未登录
}

/* ============================ 登录与权限 ============================ */

/* 账号制，不分角色：登录即可用全部功能，因此没有 readonly 态 */
const GUEST_MODE = () => false;

/* ---------------- 登录错误展示 ---------------- */

let _lockTimer = null;

function setLoginDisabled(disabled) {
  const btn = $('#loginBtn');
  const em = $('#loginEmail');
  const pw = $('#loginPw');
  if (btn) btn.disabled = disabled;
  if (em) em.disabled = disabled;
  if (pw) pw.disabled = disabled;
}

function clearLoginError() {
  if (_lockTimer) { clearInterval(_lockTimer); _lockTimer = null; }
  const e = $('#loginErr');
  if (e) { e.hidden = true; e.className = 'login-err'; e.innerHTML = ''; }
  setLoginDisabled(false);
}

function fmtCountdown(s) {
  s = Math.max(0, Math.round(s));
  if (s < 60) return `${s} 秒`;
  const m = Math.floor(s / 60), r = s % 60;
  return r ? `${m} 分 ${r} 秒` : `${m} 分`;
}

/* type: 'error' | 'lock' | 'info'
   - error: 红框，可附 remaining（剩余可尝试次数）
   - lock:  橙框，附 retry_after 倒计时，期间禁用表单
   - info:  蓝框，普通提示（如已退出登录） */
function setLoginError({ msg, type = 'error', remaining, retry_after = 0 }) {
  const e = $('#loginErr');
  if (!e) return;
  e.hidden = false;
  e.className = 'login-err' +
    (type === 'lock' ? ' is-lock' : type === 'info' ? ' is-info' : '');
  const ico = type === 'lock' ? '!' : type === 'info' ? 'i' : '×';
  let html = `<span class="le-ico">${ico}</span><span class="le-body">${esc(msg)}`;
  if (type === 'error' && typeof remaining === 'number' && remaining > 0) {
    html += `<span class="le-count">还可尝试 ${remaining} 次</span>`;
  } else if (type === 'lock' && retry_after > 0) {
    // 先占位，倒计时启动后按秒刷新
    html += `<span class="le-count">请 ${fmtCountdown(retry_after)} 后重试</span>`;
  }
  html += `</span>`;
  e.innerHTML = html;

  if (type === 'lock' && retry_after > 0) {
    setLoginDisabled(true);
    startLockCountdown(retry_after);
  }
}

function startLockCountdown(sec) {
  if (_lockTimer) clearInterval(_lockTimer);
  const e = $('#loginErr');
  const cnt = e.querySelector('.le-count');
  if (cnt) cnt.textContent = `请 ${fmtCountdown(sec)} 后重试`;
  let left = sec;
  _lockTimer = setInterval(() => {
    left -= 1;
    if (left <= 1) {                 // 倒计时结束自动解锁
      clearInterval(_lockTimer); _lockTimer = null;
      clearLoginError();
      return;
    }
    if (cnt) cnt.textContent = `请 ${fmtCountdown(left)} 后重试`;
  }, 1000);
}

function loginShake() {
  const card = $('#loginForm');
  const pw = $('#loginPw') && $('#loginPw').closest('.lf-input');
  const em = $('#loginEmail') && $('#loginEmail').closest('.lf-input');
  [card, pw, em].forEach(el => {
    if (!el) return;
    const cls = el === card ? 'err-shake' : 'shake';
    el.classList.remove(cls); void el.offsetWidth; el.classList.add(cls);
  });
  setTimeout(() => {
    if (card) card.classList.remove('err-shake');
    if (pw) pw.classList.remove('shake');
    if (em) em.classList.remove('shake');
  }, 420);
}

function showLogin(msg) {
  // 登录页是不透明全屏遮罩，身后的应用外壳完全用不上 —— 一并藏掉，
  // 既省一次渲染，也杜绝任何"边缘透出界面"的可能
  document.body.classList.add('locked');
  const s = $('#loginScreen');
  if (!s) return;
  s.hidden = false;
  if (msg) setLoginError({ msg, type: 'info' });
  else clearLoginError();
  prefillRemembered();          // 退出后再进来，已记住的账号密码自动回填
  setTimeout(() => { try { $('#loginEmail').focus(); } catch (_) {} }, 60);
}

function hideLogin() {
  document.body.classList.remove('locked');
  const s = $('#loginScreen');
  if (s) s.hidden = true;
  clearLoginError();
}

function applyRoleUI() {
  const bar = $('#roleBar');
  if (bar) bar.hidden = !state.email;
  const nm = $('#roleName');
  if (nm) nm.textContent = state.email || '';
  document.title = state.email ? `VOC洞察平台 · ${state.email}` : 'VOC洞察平台';
}

async function fetchAuthStatus() {
  try {
    const j = await (await api('/api/auth/status')).json();
    state.auth = { enabled: !!j.auth_enabled, account_count: j.account_count || 0 };
  } catch (e) {
    state.auth = { enabled: false, account_count: 0 };
  }
}

async function boot() {
  const { platforms } = await (await api('/api/platforms')).json();
  state.platforms = platforms;
  const _first = platforms.find(p => p.id === state.platform) || platforms[0];
  state.group = _first ? (_first.group || '其他') : '其他';
  renderPlatformGroups();
  renderPlatformTabs();
  updateGroupUI();
  bindAftersaleUI();
  bindSetupEvents();
  bindResultEvents();
  bindListToolbar();
  bindRecordsBoard();
  await loadDatasets();
  await loadEngine();
  probePdfEngine();
  probeMaintenance(true);
  applyPlatform(state.platform);
  applyRoleUI();

  try {
    const h = await (await api('/api/health')).json();
    state.build = h.build || 0;
  } catch (e) { /* 取不到构建号就不做版本提示 */ }

  // 深链：#records 打开记录板块；#ds=<id>&tab=<页签> 直接定位到某次分析结果
  const hash = (location.hash || '').replace(/^#/, '');
  const params = new URLSearchParams(hash);
  const dsId = params.get('ds');
  const tab = params.get('tab');
  if (!dsId && hash === 'records') {
    showView('records');
  } else if (dsId && state.datasets.some(d => d.id === dsId)) {
    await openDataset(dsId);
    if (tab) switchTab(tab);
  }
}

/* ---------------- 记住密码（本机浏览器保存） ---------------- */
/* 说明：凭据仅存于当前浏览器 localStorage（密码经 base64 简单混淆，非加密），
   不会上送服务器。用于避免每次重输；在公共 / 共享电脑上请勿勾选。 */

const REMEMBER_KEY = 'voc_remember_creds';

function loadRemembered() {
  try {
    const raw = localStorage.getItem(REMEMBER_KEY);
    if (!raw) return null;
    const o = JSON.parse(raw);
    if (o && o.email) {
      o.password = o.password ? safeB64Decode(o.password) : '';
      return o;
    }
  } catch (_) {}
  return null;
}
function saveRemembered(email, pw) {
  try {
    localStorage.setItem(REMEMBER_KEY,
      JSON.stringify({ email, password: safeB64Encode(pw) }));
  } catch (_) {}
}
function clearRemembered() {
  try { localStorage.removeItem(REMEMBER_KEY); } catch (_) {}
}
function safeB64Encode(s) {
  try { return btoa(unescape(encodeURIComponent(s))); } catch (_) { return ''; }
}
function safeB64Decode(s) {
  try { return decodeURIComponent(escape(atob(s))); } catch (_) { return ''; }
}
function prefillRemembered() {
  const o = loadRemembered();
  if (!o) return;
  const em = $('#loginEmail'), pw = $('#loginPw'), rem = $('#loginRemember');
  // 只填空字段，避免覆盖用户已输入的内容
  if (em && !em.value) em.value = o.email;
  if (pw && !pw.value) pw.value = o.password || '';
  if (rem) rem.checked = true;
}

/* ---------------- 登录 / 退出 ---------------- */

async function doLogin(email, pw) {
  let r;
  try {
    r = await fetch('/api/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email, password: pw }),
    });
  } catch (netErr) {
    setLoginError({ msg: '无法连接服务器，请检查网络后重试', type: 'error' });
    loginShake();
    throw new Error('网络错误');
  }

  if (!r.ok) {
    const info = await parseLoginError(r);
    if (r.status === 429) {
      setLoginError({ msg: info.msg || '尝试过于频繁，已临时锁定', type: 'lock', retry_after: info.retry_after || 60 });
    } else {
      setLoginError({ msg: info.msg || '登录失败', type: 'error', remaining: info.remaining });
    }
    loginShake();
    // 清空密码、保留邮箱、聚焦密码，方便立刻重试
    const pwEl = $('#loginPw');
    if (pwEl) pwEl.value = '';
    setTimeout(() => { try { pwEl && pwEl.focus(); } catch (_) {} }, 60);
    throw new Error(info.msg || '登录失败');
  }

  const j = await r.json();
  state.email = j.email;

  // 记住密码：勾选则保存，未勾选则清除（即使之前存过）
  if ($('#loginRemember') && $('#loginRemember').checked) {
    saveRemembered(email, pw);
  } else {
    clearRemembered();
  }

  hideLogin();
  await boot();
  endBoot();
  toast(`已登录：${j.email}`);
}

async function parseLoginError(r) {
  let body = null;
  try { body = await r.json(); } catch (_) {}
  // FastAPI 把错误放在 {detail: ...} 里；detail 既可能是字符串，也可能是结构化对象
  const detail = (body && body.detail !== undefined) ? body.detail : body;
  if (detail && typeof detail === 'object' && detail.msg) {
    return { msg: detail.msg, remaining: detail.remaining, retry_after: detail.retry_after };
  }
  let m = r.statusText;
  if (typeof detail === 'string') m = detail;
  return { msg: m };
}

async function doLogout() {
  try { await fetch('/api/auth/logout', { method: 'POST' }); } catch (e) {}
  state.email = null;
  state.current = null; state.result = null;
  destroyCharts();
  showLogin('已退出登录');
  endBoot();
}

function bindLogin() {
  const form = $('#loginForm');
  if (!form) return;
  prefillRemembered();
  form.onsubmit = async (ev) => {
    ev.preventDefault();
    const btn = $('#loginBtn');
    btn.disabled = true;
    btn.textContent = '验证中…';
    try {
      await doLogin($('#loginEmail').value.trim(), $('#loginPw').value);
    } catch (e) {
      // 错误已在 doLogin 内以结构化方式展示（含剩余次数 / 锁定倒计时）
    } finally {
      btn.disabled = false;
      btn.textContent = '进入平台';
    }
  };
  const lo = $('#btnLogout');
  if (lo) lo.onclick = doLogout;

  // 显示 / 隐藏密码
  const tog = $('#loginPwToggle');
  if (tog) tog.onclick = () => {
    const i = $('#loginPw');
    if (!i) return;
    const show = i.type === 'password';
    i.type = show ? 'text' : 'password';
    tog.classList.toggle('on', show);
    try { i.focus(); } catch (_) {}
  };

  // 回车在密码框也能提交（form 默认已支持，这里仅保证 toggle 不会吞事件）
  if (tog) tog.addEventListener('keydown', e => { if (e.key === 'Enter') e.preventDefault(); });
}

/* 401 一律送回登录页（会话过期、没登录、被踢下线） */
function handleAuthFailure(status) {
  if (status === 401) {
    state.email = null;
    showLogin('请先登录');
    return true;
  }
  return false;
}

function emptyVoiceFilter() {
  return { q: '', purchase: '', dim: '', platform: '', tag: '', persona: '', brand: '' };
}

const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));

function esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function toast(msg, isErr) {
  const t = $('#toast');
  t.textContent = msg;
  t.className = 'toast show' + (isErr ? ' err' : '');
  clearTimeout(t._timer);
  t._timer = setTimeout(() => { t.className = 'toast'; }, 3200);
}

/* 失败诊断：完整展示后端给出的多行原因 */
function showError(msg) {
  $('#errorBody').textContent = (msg || '未知错误').trim();
  $('#errorBox').hidden = false;
}
function clearError() {
  $('#errorBox').hidden = true;
  $('#errorBody').textContent = '';
}

async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) {
    let msg = r.statusText;
    try { const j = await r.json(); msg = j.detail || msg; } catch (e) {}
    if (r.status === 401 && handleAuthFailure(401)) {
      throw new Error('请先登录');
    }
    throw new Error(msg);
  }
  return r;
}

/* ============================ 初始化 ============================ */

/* 撤掉启动遮罩：不论最终进平台还是进登录页，都由这里统一收口，
   保证不会出现"完整界面先闪一下、然后才弹登录页"。 */
function endBoot() {
  document.body.classList.remove('booting');
  const s = $('#bootScreen');
  if (s) s.hidden = true;
}

async function init() {
  bindLogin();

  // 焦点回到页面时与服务端对齐，避免界面停留在旧状态
  window.addEventListener('focus', debounce(resyncOnFocus, 700));
  document.addEventListener('visibilitychange', () => { if (!document.hidden) resyncOnFocus(); });
  window.addEventListener('hashchange', onHashChange);

  await fetchAuthStatus();

  // 一个账号都没有：本机直接进（保留「本机双击即用」的老体验）
  if (!state.auth.enabled) {
    state.email = 'local@localhost';
    hideLogin();
    await boot();
    endBoot();
    return;
  }

  // 已有账号：先问服务端「我是谁」，能认出来就直接进
  try {
    const me = await (await api('/api/auth/me')).json();
    if (me.authenticated && me.email) {
      state.email = me.email;
      hideLogin();
      await boot();
      endBoot();
      return;
    }
  } catch (e) { /* 落到登录页 */ }

  showLogin();
  endBoot();
}

function onHashChange() {
  const raw = (location.hash || '').replace(/^#/, '');
  if (raw === 'records') { showView('records'); return; }
  const h = new URLSearchParams(raw);
  const ds = h.get('ds');
  const tab = h.get('tab');
  if (ds && ds !== (state.current && state.current.id)) {
    openDataset(ds).then(() => { if (tab) switchTab(tab); });
    return;
  }
  // 仅页签变化时也要切换（浏览器对纯 hash 变化不重新加载页面）
  if (ds && !tab) return;
  let target = tab;
  if (!target) {
    const m = (location.hash || '').match(/^#(overview|dimensions|features|persona|voice|report)$/);
    if (m) target = m[1];
  }
  if (target && target !== state.tab && state.result) switchTab(target);
}

function renderPlatformGroups() {
  const seen = [];
  state.platforms.forEach(p => { const g = p.group || '其他'; if (!seen.includes(g)) seen.push(g); });
  $('#platformGroups').innerHTML = seen.map(g =>
    `<button data-g="${esc(g)}" class="${g === state.group ? 'active' : ''}">${esc(g)}</button>`
  ).join('');
  $$('#platformGroups button').forEach(b => {
    b.onclick = () => applyGroup(b.dataset.g);
  });
}

function renderPlatformTabs() {
  const kids = state.platforms.filter(p => (p.group || '其他') === state.group);
  $('#platformTabs').innerHTML = kids.map(p =>
    `<button data-p="${esc(p.id)}" class="${p.id === state.platform ? 'active' : ''}">${esc(p.label_cn || p.name)}</button>`
  ).join('');
  $$('#platformTabs button').forEach(b => {
    b.onclick = () => { state.platform = b.dataset.p; applyPlatform(state.platform); };
  });
}

function applyGroup(g) {
  state.group = g;
  const kids = state.platforms.filter(p => (p.group || '其他') === g);
  if (kids.length) state.platform = kids[0].id;
  renderPlatformGroups();
  renderPlatformTabs();
  updateGroupUI();
  applyPlatform(state.platform);
}

function syncGroupFromPlatform() {
  const p = state.platforms.find(x => x.id === state.platform);
  if (p && p.group) state.group = p.group;
  renderPlatformGroups();
  renderPlatformTabs();
  updateGroupUI();
}

function applyPlatform(pid) {
  state.platform = pid;
  state.recScope = 'platform';      // 切换平台板块时，报告记录自动跟随到该平台
  state.dsFilter = 'all';
  applyPlatformTexts(pid);
  $$('#platformTabs button').forEach(b => b.classList.toggle('active', b.dataset.p === pid));
  showView('setup');
  renderDatasets();
  renderDemos();
  resizeAllCharts();
}

/* 只更新与平台相关的文案与主题色（不改视图、不重渲染列表） */
function applyPlatformTexts(pid) {
  const p = state.platforms.find(x => x.id === pid);
  if (!p) return;
  document.documentElement.style.setProperty('--accent', p.color);
  document.documentElement.style.setProperty('--accent-soft', hexA(p.color, 0.09));
  document.documentElement.style.setProperty('--accent-ring', hexA(p.color, 0.22));

  $('#setupTitle').textContent = `${p.label_cn} · 新建 VOC 分析`;
  $('#setupDesc').textContent = p.desc + '。平台将自动完成适配 → 标注 → 聚合 → 报告。';
  $('#uploadHint').textContent = p.hint || '';
  $('#fieldCommentLimit').hidden = (p.kind !== 'forum');
}

/* ============================ 分析引擎 ============================ */

async function loadEngine() {
  try {
    const d = await (await api('/api/engine')).json();
    state.engineList = d.engines || [];
    state.engineCfg = d.llm || {};
    renderEngineUI();
    if (state.engineCfg.configured) testEngine(true);   // 静默自检，拿到真实状态
  } catch (e) { /* 忽略：引擎信息不可用不影响主流程 */ }
}

function renderEngineUI() {
  const cfg = state.engineCfg || {};
  const sel = $('#fEngine');
  if (sel) {
    const prev = sel.value || 'auto';
    sel.innerHTML = state.engineList.map(e =>
      `<option value="${e.id}">${esc(e.label)} — ${esc(e.desc)}</option>`).join('');
    sel.value = state.engineList.some(e => e.id === prev) ? prev : 'auto';
  }
  const hint = $('#engineHint');
  if (hint) {
    hint.innerHTML = cfg.configured
      ? `${esc(cfg.model)} @ ${esc(cfg.base_url)}　密钥 ${esc(cfg.api_key_masked || '')}`
      : '未配置 LLM，将使用内置引擎。启用方式：编辑 llm_config.json 或设置 LLM_CONNECTOR_* 后重启';
  }
  renderEnginePill();
}

function renderEnginePill(busy) {
  const pill = $('#enginePill');
  if (!pill) return;
  const cfg = state.engineCfg || {};
  pill.className = 'engine-pill';
  const txt = $('#epText');
  if (busy) {
    pill.classList.add('busy');
    txt.textContent = '正在检测 LLM…';
    return;
  }
  if (!cfg.configured) {
    txt.textContent = '内置引擎';
    pill.title = '未配置 LLM，当前使用内置词典引擎。点击查看';
    return;
  }
  const t = cfg.lastTest;
  if (!t) {
    // 尚未检测：保持中性，不谎称可用/不可用
    txt.textContent = `LLM · ${cfg.model}`;
    pill.title = '尚未检测。点击检测 LLM 连通性';
    return;
  }
  pill.classList.add(t.ok ? 'ok' : 'bad');
  txt.textContent = t.ok ? `LLM 就绪 · ${cfg.model}` : 'LLM 不可用';
  pill.title = t.message || '';
}

async function testEngine(silent) {
  const cfg = state.engineCfg || {};
  if (!cfg.configured) {
    if (!silent) toast('未配置 LLM：请编辑 llm_config.json 或设置 LLM_CONNECTOR_* 后重启服务');
    return;
  }
  if (!silent) renderEnginePill(true);
  try {
    const r = await (await api('/api/engine/test', { method: 'POST' })).json();
    state.engineCfg.lastTest = r;
    renderEnginePill();
    if (!silent) toast(r.ok ? `LLM 连接正常（${r.latency_ms}ms）` : `LLM 不可用：${r.message}`, !r.ok);
  } catch (e) {
    renderEnginePill();
    if (!silent) toast('检测失败：' + e.message, true);
  }
}

/* 已完成后端的回退/告警提示 */
function showWarnings(list) {
  if (!list || !list.length) return;
  const box = $('#warnBox');
  if (!box) return;
  box.innerHTML = list.map(w => `<div class="wb-row"><span class="wb-icon">!</span><span>${esc(w)}</span></div>`).join('');
  box.hidden = false;
}
function clearWarnings() { const b = $('#warnBox'); if (b) { b.hidden = true; b.innerHTML = ''; } }

function hexA(hex, a) {
  const h = hex.replace('#', '');
  const r = parseInt(h.slice(0, 2), 16), g = parseInt(h.slice(2, 4), 16), b = parseInt(h.slice(4, 6), 16);
  return `rgba(${r},${g},${b},${a})`;
}

function showView(name) {
  state.view = name;
  $('#viewSetup').hidden = name !== 'setup';
  $('#viewProgress').hidden = name !== 'progress';
  $('#viewResult').hidden = name !== 'result';
  const vr = $('#viewRecords');
  if (vr) vr.hidden = name !== 'records';
  if (name === 'records') renderRecordsBoard();
}

/* ============================ 数据集列表 ============================ */

async function loadDatasets() {
  const { datasets } = await (await api('/api/datasets')).json();
  state.datasets = datasets;
  renderDatasets();
}

/* 记录列表：平台筛选 + 时间排序 + 多选删除 */
function dsPrefs() {
  try {
    return JSON.parse(localStorage.getItem('voc.dsPrefs') || '{}');
  } catch (e) { return {}; }
}
function saveDsPrefs() {
  try {
    localStorage.setItem('voc.dsPrefs', JSON.stringify({
      sort: state.dsSort, filter: state.dsFilter,
    }));
  } catch (e) { /* 隐私模式下可能不可用 */ }
}

function fmtTime(s) {
  if (!s) return '';
  const d = new Date(String(s).replace(' ', 'T'));
  if (isNaN(d.getTime())) return String(s).slice(5, 16);
  const now = new Date();
  const sameDay = d.toDateString() === now.toDateString();
  const y = d.getFullYear(), m = String(d.getMonth() + 1).padStart(2, '0');
  const dd = String(d.getDate()).padStart(2, '0');
  const hm = String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0');
  if (sameDay) return `今天 ${hm}`;
  if (y === now.getFullYear()) return `${m}-${dd} ${hm}`;
  return `${y}-${m}-${dd}`;
}

function visibleDatasets() {
  let list = state.datasets.slice();
  // 与报告记录板块保持同一套范围：平台板块下只看本平台，「全部平台」下才用下拉筛选
  if (state.recScope === 'platform' && state.platform) {
    list = list.filter(d => d.platform === state.platform);
  } else if (state.dsFilter && state.dsFilter !== 'all') {
    list = list.filter(d => d.platform === state.dsFilter);
  }
  list.sort((a, b) => {
    const ka = a.created_at || '', kb = b.created_at || '';
    const c = ka < kb ? -1 : ka > kb ? 1 : 0;
    return state.dsSort === 'asc' ? c : -c;
  });
  return list;
}

function renderDatasets() {
  const list = visibleDatasets();
  $('#dsCount').textContent = list.length;

  // 平台筛选下拉：仅在「全部平台」范围下有意义
  const sel = $('#dsFilter');
  if (sel) {
    sel.hidden = state.recScope === 'platform';
    const plats = [...new Set(state.datasets.map(d => d.platform))].sort();
    const cur = state.dsFilter;
    sel.innerHTML = `<option value="all">全部平台</option>` +
      plats.map(p => `<option value="${esc(p)}">${esc(p)}</option>`).join('');
    sel.value = (cur === 'all' || plats.includes(cur)) ? cur : 'all';
  }
  const sideLabel = $('#dsHeadLabel');
  if (sideLabel) {
    const p = state.platforms.find(x => x.id === state.platform);
    sideLabel.textContent = (state.recScope === 'platform' && p) ? `${p.name} 报告记录` : '分析报告记录';
  }
  const sb = $('#dsSort');
  if (sb) {
    sb.textContent = state.dsSort === 'desc' ? '最新在前' : '最早在前';
    sb.title = state.dsSort === 'desc' ? '当前：最新在前（点击切换）' : '当前：最早在前（点击切换）';
  }
  const selBtn = $('#dsSelBtn');
  if (selBtn) selBtn.classList.toggle('active', state.selMode);
  $('#dsSelBar').hidden = !state.selMode;

  const el = $('#dsList');
  if (!list.length) {
    const p = state.platforms.find(x => x.id === state.platform);
    const scoped = state.recScope === 'platform' && state.platform;
    el.innerHTML = state.datasets.length
      ? (scoped
          ? `${esc(p ? p.label_cn : state.platform)} 板块下还没有报告<br><span>上传该平台数据即可生成</span>`
          : '当前筛选下没有记录<br><span>可切换平台筛选</span>')
      : '<div class="empty-hint">暂无分析记录<br><span>点击「新建分析」开始</span></div>';
    updateSelBar();
    if (state.view === 'records') renderRecordsBoard();
    return;
  }

  el.innerHTML = list.map(d => {
    const checked = state.selected.has(d.id);
    const n = d.overview ? d.overview.units : null;
    const meta = [esc(d.platform), fmtTime(d.created_at)].filter(Boolean).join(' · ');
    const isLlm = String(d.engine_used || '').startsWith('llm');
    const badge = d.status === 'done'
      ? `<span class="ds-badge ${isLlm ? 'llm' : ''}">${isLlm ? 'LLM' : '内置'}</span>` : '';
    return `
    <div class="ds-item ${state.current && state.current.id === d.id ? 'active' : ''} ${checked ? 'sel' : ''}"
         data-id="${esc(d.id)}">
      ${state.selMode ? `<input type="checkbox" class="ds-check" data-id="${esc(d.id)}" ${checked ? 'checked' : ''}>`
                      : `<span class="ds-dot ${esc(d.status)}"></span>`}
      <div class="ds-body">
        <div class="ds-name">${esc(d.category || d.name)}${badge}</div>
        <div class="ds-meta"><span>${meta}</span><span>${n != null ? n + ' 单元' : esc(statusText(d))}</span></div>
      </div>
    </div>`;
  }).join('');

  $$('#dsList .ds-item').forEach(it => {
    it.onclick = (ev) => {
      const id = it.dataset.id;
      if (state.selMode) {
        if (ev.target.classList.contains('ds-check')) return;   // 复选框自己处理
        toggleSelect(id);
      } else {
        openDataset(id);
      }
    };
  });
  $$('#dsList .ds-check').forEach(cb => {
    cb.onclick = (ev) => { ev.stopPropagation(); toggleSelect(cb.dataset.id, cb.checked); };
  });
  updateSelBar();
  if (state.view === 'records') renderRecordsBoard();
}

function toggleSelect(id, force) {
  const has = state.selected.has(id);
  const want = force === undefined ? !has : force;
  if (want) state.selected.add(id); else state.selected.delete(id);
  renderDatasets();
}

function updateSelBar() {
  const n = state.selected.size;
  const sc = $('#dsSelCount');
  if (sc) sc.textContent = `已选 ${n} 条`;
  const all = $('#dsAll');
  if (all) {
    const vis = visibleDatasets();
    all.checked = vis.length > 0 && vis.every(d => state.selected.has(d.id));
    all.indeterminate = n > 0 && !all.checked;
  }
  const del = $('#dsDelBtn');
  if (del) del.textContent = n ? `删除 ${n} 条` : '删除';
  // 记录板块工具条同步（两处共用同一套选择状态）
  const rb = $('#recSelBtn');
  if (rb) {
    rb.textContent = state.selMode ? '退出选择' : '批量选择';
    rb.classList.toggle('active', state.selMode);
  }
  const rd = $('#recDelBtn');
  if (rd) { rd.hidden = !state.selMode; rd.textContent = n ? `删除所选 ${n} 条` : '删除所选'; }
  const rc = $('#recCancelBtn');
  if (rc) rc.hidden = !state.selMode;
}

function setSelMode(on) {
  state.selMode = !!on;
  if (!state.selMode) state.selected.clear();
  renderDatasets();
}

/* ---------------------------------------------------------------------------
   删除的三层保障
   1) 乐观移除：点下删除的瞬间记录就从界面消失，不等网络往返
   2) 服务端真值校准：拿到响应后用服务端列表对齐
   3) DOM 兜底清扫：万一还有残留节点，直接从 DOM 摘掉
   接口失败则整体回滚，界面与数据保持一致。
--------------------------------------------------------------------------- */

function sweepRemoved(ids) {
  (ids || []).forEach(id => {
    $$(`#dsList .ds-item[data-id="${id}"], #recGrid .rec-card[data-id="${id}"]`)
      .forEach(n => n.remove());
  });
}

function hideRecordsNow(ids) {
  const set = new Set(ids);
  state.datasets = state.datasets.filter(d => !set.has(d.id));
  ids.forEach(id => state.selected.delete(id));
  if (state.current && set.has(state.current.id)) {
    state.current = null; state.result = null; state.reportMd = '';
    destroyCharts();
  }
  renderDatasets();
  if (state.view === 'records') renderRecordsBoard();
  sweepRemoved(ids);
}

async function performDelete(ids, label) {
  const list = [...new Set(ids || [])];
  if (!list.length) { toast('请先勾选要删除的记录'); return false; }

  const snapshot = state.datasets.slice();
  const prevView = state.view;
  const wasOpen = state.current && list.includes(state.current.id);

  hideRecordsNow(list);                    // ① 立刻消失
  try {
    const r = await (await api('/api/datasets/delete', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ids: list }),
    })).json();
    await loadDatasets();                  // ② 服务端真值校准
    sweepRemoved(list);                    // ③ 兜底
    if (wasOpen && prevView === 'result') showView('setup');
    state.selMode = false;
    renderDatasets();
    if (state.view === 'records') renderRecordsBoard();
    probeMaintenance(true);
    toast(`已删除 ${r.deleted} 条报告${label ? '（' + label + '）' : ''}，报告与原始文件已彻底清除`);
    return true;
  } catch (e) {
    state.datasets = snapshot;             // 回滚
    renderDatasets();
    if (state.view === 'records') renderRecordsBoard();
    toast('删除失败：' + e.message + '（界面已恢复）', true);
    return false;
  }
}

async function deleteSelected() {
  const ids = [...state.selected];
  if (!ids.length) { toast('请先勾选要删除的记录'); return; }
  const names = ids.map(id => {
    const d = state.datasets.find(x => x.id === id);
    return d ? `${d.platform} · ${d.category}` : id;
  });
  const preview = names.slice(0, 5).join('\n') + (names.length > 5 ? `\n… 等共 ${names.length} 条` : '');
  if (!confirm(`确认删除以下 ${ids.length} 条分析报告？\n\n${preview}\n\n对应的产出目录与上传的原始数据文件会一并清除，且不可撤销。`)) return;
  await performDelete(ids);
}

function bindListToolbar() {
  const f = dsPrefs();
  state.dsSort = f.sort === 'asc' ? 'asc' : 'desc';
  state.dsFilter = f.filter || 'all';

  $('#dsFilter').onchange = (e) => { state.dsFilter = e.target.value; saveDsPrefs(); renderDatasets(); };
  $('#dsSort').onclick = () => {
    state.dsSort = state.dsSort === 'desc' ? 'asc' : 'desc';
    saveDsPrefs(); renderDatasets();
  };
  $('#dsSelBtn').onclick = () => setSelMode(!state.selMode);
  $('#dsCancelBtn').onclick = () => setSelMode(false);
  $('#dsDelBtn').onclick = deleteSelected;
  $('#dsAll').onchange = (e) => {
    const vis = visibleDatasets();
    if (e.target.checked) vis.forEach(d => state.selected.add(d.id));
    else vis.forEach(d => state.selected.delete(d.id));
    renderDatasets();
  };
}

function engineLabel(e) {
  e = String(e || '');
  if (!e || e === 'offline-rule-engine') return '内置离线引擎';
  if (e.startsWith('llm:')) return 'LLM · ' + e.slice(4);
  return e;
}

function statusText(d) {
  if (d.status === 'done') return `${d.overview ? d.overview.units : '?'} 单元`;
  if (d.status === 'running') return `${d.progress}%`;
  if (d.status === 'error') return '失败';
  return '排队中';
}

/* ==================== 磁盘残留清理 ==================== */

function fmtBytes(n) {
  n = Number(n) || 0;
  if (n < 1024) return n + ' B';
  if (n < 1024 * 1024) return (n / 1024).toFixed(0) + ' KB';
  if (n < 1024 * 1024 * 1024) return (n / 1024 / 1024).toFixed(1) + ' MB';
  return (n / 1024 / 1024 / 1024).toFixed(2) + ' GB';
}

/* 探测「已删除但仍留在磁盘上」的产出目录与上传原件 */
async function probeMaintenance(silent) {
  try {
    const r = await (await api('/api/maintenance/status')).json();
    state.maintenance = r;
  } catch (e) {
    if (!silent) toast('无法读取磁盘残留信息：' + e.message, true);
    return;
  }
  const m = state.maintenance || {};
  const dirs = (m.orphan_dirs || []).length;
  const ups = m.orphan_uploads || 0;
  const btn = $('#recCleanBtn');
  if (!btn) return;
  btn.hidden = (dirs + ups) === 0;
  if (btn.hidden) return;
  btn.textContent = `清理残留 ${dirs + ups}`;
  btn.title = `${dirs} 个已删除记录的产出目录、${ups} 个无主上传原件，共 ${fmtBytes((m.orphan_dir_bytes || 0) + (m.orphan_upload_bytes || 0))}`;
}

async function runCleanup() {
  const m = state.maintenance || {};
  const dirs = (m.orphan_dirs || []).length;
  const ups = m.orphan_uploads || 0;
  if (!dirs && !ups) { toast('当前没有需要清理的残留'); return; }
  const bytes = (m.orphan_dir_bytes || 0) + (m.orphan_upload_bytes || 0);
  if (!confirm(
    `即将清理以下磁盘残留（这些内容已不在记录列表中）：\n\n` +
    `· ${dirs} 个产出目录\n· ${ups} 个上传原件\n· 合计约 ${fmtBytes(bytes)}\n\n` +
    `正在排队或运行中的分析不会被删除。确定继续？`)) return;
  try {
    const btn = $('#recCleanBtn');
    if (btn) { btn.disabled = true; btn.textContent = '清理中…'; }
    const r = await (await api('/api/maintenance/cleanup', { method: 'POST' })).json();
    await loadDatasets();
    await probeMaintenance(true);
    renderRecordsBoard();
    toast(`已清理 ${r.removed_dirs.length} 个残留目录、${r.removed_uploads} 个原件，释放 ${fmtBytes(r.freed_bytes)}`);
  } catch (e) {
    toast('清理失败：' + e.message, true);
    probeMaintenance(true);
  }
}

/* 页面重新可见 / 获得焦点时，与服务端重新对齐。
   解决两类问题：
   ① 标签页长时间挂着，别的标签页或服务端变化后界面还是旧的
   ② 服务端被重启并更新过前端文件时，提示刷新，避免"旧代码 + 新界面"的错位 */
async function resyncOnFocus() {
  try {
    const h = await (await api('/api/health')).json();
    if (h.build && state.build && h.build !== state.build) {
      toast('平台已更新到新版本，请按 Ctrl+R 刷新页面', true);
      return;
    }
  } catch (e) { /* 健康检查失败不打扰用户 */ }
  if (!state.datasets.length) return;
  await loadDatasets();
  if (state.view === 'records') renderRecordsBoard();
  probeMaintenance(true);
}

/* ==================== 分析报告记录 · 按平台独立板块 ==================== */

/* 记录板块的可见记录：先按「当前平台 / 全部平台」定范围，再叠加平台筛选、搜索词与时间排序 */
function boardDatasets() {
  const q = (state.recQuery || '').trim().toLowerCase();
  let list = state.datasets.slice();
  if (state.recScope === 'platform' && state.platform) {
    list = list.filter(d => d.platform === state.platform);
  } else if (state.dsFilter && state.dsFilter !== 'all') {
    list = list.filter(d => d.platform === state.dsFilter);
  }
  if (q) {
    list = list.filter(d => [d.name, d.category, d.platform, d.brands]
      .some(x => String(x || '').toLowerCase().includes(q)));
  }
  list.sort((a, b) => {
    const ka = a.created_at || '', kb = b.created_at || '';
    const c = ka < kb ? -1 : ka > kb ? 1 : 0;
    return state.dsSort === 'asc' ? c : -c;
  });
  return list;
}

function platformCount(pid) {
  return state.datasets.filter(d => d.platform === pid).length;
}

/* 平台子板块切换条：AMAZON / WAYFAIR / REDDIT / 全部平台 */
function renderPlatformSubTabs() {
  const box = $('#platformSubTabs');
  if (!box) return;
  const items = state.platforms.map(p => ({ id: p.id, name: p.name, n: platformCount(p.id), color: p.color }));
  items.push({ id: '__all__', name: '全部平台', n: state.datasets.length, color: null });
  box.innerHTML = items.map(it => `
    <button class="psub-tab ${state.recScope === 'platform' ? state.platform === it.id : it.id === '__all__' ? 'active' : ''}"
            data-pid="${esc(it.id)}" ${it.color ? `style="--pc:${esc(it.color)}"` : ''}>
      <span class="pt-name">${esc(it.name)}</span>
      <span class="pt-count">${it.n}</span>
    </button>`).join('');
  $$('#platformSubTabs .psub-tab').forEach(b => {
    b.onclick = () => {
      const pid = b.dataset.pid;
      if (pid === '__all__') state.recScope = 'all';
      else { state.recScope = 'platform'; state.platform = pid; syncGroupFromPlatform(); applyPlatformTexts(pid); }
      state.dsFilter = 'all';
      state.recQuery = '';
      const qi = $('#recQuery'); if (qi) qi.value = '';
      renderDatasets();
      renderRecordsBoard();
    };
  });
}

function recCardHtml(d) {
  const done = d.status === 'done';
  const color = PLAT_COLOR[d.platform] || '#64748b';
  const isLlm = String(d.engine_used || '').startsWith('llm');
  const n = d.overview ? d.overview.units : null;
  const checked = state.selected.has(d.id);
  const chips = done
    ? `<span class="rec-chips-label">六板块</span>` + REC_SECTION_LABELS.map(t => `<span class="rec-chip">${esc(t)}</span>`).join('')
    : `<span class="rec-chip ${esc(d.status)}">${esc(statusText(d))}</span>`;
  const stat = [];
  stat.push(n != null ? `${n} 个讨论单元` : statusText(d));
  if (done && d.overview && d.overview.comments) stat.push(`${d.overview.comments} 条评论`);
  if (done && d.overview && d.overview.avg_rating != null) stat.push(`均 ${d.overview.avg_rating}★`);
  return `
  <article class="rec-card ${checked ? 'sel' : ''} ${state.current && state.current.id === d.id ? 'active' : ''}"
           data-id="${esc(d.id)}">
    <span class="rec-stripe" style="background:${esc(color)}"></span>
    ${state.selMode ? `<label class="rec-check"><input type="checkbox" data-id="${esc(d.id)}" ${checked ? 'checked' : ''}></label>` : ''}
    <div class="rec-top">
      <span class="rec-plat" style="color:${esc(color)}">${esc(String(d.platform || '').toUpperCase())}</span>
      <span class="rec-time">${esc(fmtTime(d.created_at))}</span>
    </div>
    <div class="rec-cat">${esc(d.category || d.name)}</div>
    <div class="rec-sub" title="${esc(d.name)}">${esc(d.name)}</div>
    <div class="rec-stat">
      <span>${esc(stat.join(' · '))}</span>
      ${done ? `<span class="badge ${isLlm ? 'llm' : ''}" title="${esc(engineLabel(d.engine_used))}">${isLlm ? 'LLM 引擎' : '内置引擎'}</span>` : ''}
    </div>
    <div class="rec-chips" title="${esc(REC_SECTION_FULL)}">${chips}</div>
    <div class="rec-foot">
      <button class="rec-btn rec-open" data-id="${esc(d.id)}" ${done ? '' : 'disabled'}
              title="打开该报告并跳转到「报告」板块">${done ? '查看报告' : '尚不可用'}</button>
      <button class="rec-btn rec-pdf" data-id="${esc(d.id)}" ${done ? '' : 'disabled'} title="把该记录的六个板块（含全部图表）导出为 PDF">导出 PDF</button>
      <button class="rec-btn danger rec-del" data-id="${esc(d.id)}" title="删除该报告及其全部产出">删除</button>
    </div>
  </article>`;
}

function renderRecordsBoard() {
  const grid = $('#recGrid');
  if (!grid) return;
  renderPlatformSubTabs();
  const list = boardDatasets();
  const plat = state.platforms.find(p => p.id === state.platform);
  const scoped = state.recScope === 'platform';

  // 标题随范围变化：AMAZON / WAYFAIR / REDDIT 各自成板块
  const title = $('#recTitle');
  if (title) {
    title.textContent = scoped
      ? `${plat ? plat.name : state.platform} · 分析报告记录`
      : '全部平台 · 分析报告记录';
    title.style.color = scoped && plat ? plat.color : '';
  }
  const desc = $('#recDesc');
  if (desc) {
    const scopeTxt = scoped
      ? `这里只显示 <b>${esc(plat ? plat.label_cn : state.platform)}</b> 板块下已保存的报告（共 ${list.length} 条）。`
      : `这里显示三个板块的全部报告（共 ${list.length} 条）。`;
    desc.innerHTML = scopeTxt + `每条报告都完整涵盖
      <b>概览</b> · <b>六维分析</b> · <b>功能体验</b> · <b>人群与交叉</b> · <b>数据原声</b> · <b>报告</b> 六个板块，可随时查看、导出 PDF 或删除。`;
  }
  const sideLabel = $('#dsHeadLabel');
  if (sideLabel) {
    sideLabel.textContent = scoped && plat ? `${plat.name} 报告记录` : '分析报告记录';
  }
  const platBtn = $('#btnPlatRecords');
  if (platBtn) {
    platBtn.textContent = `${plat ? plat.label_cn : state.platform} 报告记录（${platformCount(state.platform)}）`;
  }

  // 平台筛选下拉只在「全部平台」范围下有意义
  const sel = $('#recPlat');
  if (sel) {
    sel.hidden = scoped;
    const plats = [...new Set(state.datasets.map(d => d.platform))].sort();
    sel.innerHTML = `<option value="all">全部平台</option>` +
      plats.map(p => `<option value="${esc(p)}">${esc(p)}</option>`).join('');
    sel.value = (state.dsFilter === 'all' || plats.includes(state.dsFilter)) ? state.dsFilter : 'all';
  }
  const sb = $('#recSort');
  if (sb) sb.textContent = state.dsSort === 'desc' ? '最新在前' : '最早在前';
  const stat = $('#recStat');
  if (stat) stat.textContent = `共 ${list.length} 条${scoped ? ` / 本平台 ${platformCount(state.platform)} 条` : ` / 全部 ${state.datasets.length} 条`}`;
  const qi = $('#recQuery');
  if (qi && qi.value !== state.recQuery) qi.value = state.recQuery;

  if (!list.length) {
    grid.innerHTML = `<div class="rec-empty">${
      state.datasets.length
        ? (state.recQuery
            ? `没有匹配「${esc(state.recQuery)}」的报告<br><span>试试其它关键词，或切换上方平台板块</span>`
            : (scoped
                ? `${esc(plat ? plat.label_cn : state.platform)} 板块下还没有报告<br><span>点击右上角「新建分析」，上传该平台的数据文件即可生成报告</span>`
                : `当前筛选下没有报告<br><span>试试切换平台筛选</span>`))
        : `还没有任何分析报告<br><span>点击右上角「新建分析」，上传数据即可生成第一条报告</span>`
    }</div>`;
    updateSelBar();
    return;
  }

  grid.innerHTML = list.map(recCardHtml).join('');

  $$('#recGrid .rec-card').forEach(card => {
    card.onclick = (ev) => {
      if (ev.target.closest('.rec-btn') || ev.target.closest('.rec-check')) return;
      const id = card.dataset.id;
      if (state.selMode) toggleSelect(id);
      else viewReport(id);
    };
  });
  $$('#recGrid .rec-check input').forEach(cb => {
    cb.onclick = (ev) => { ev.stopPropagation(); toggleSelect(cb.dataset.id, cb.checked); };
  });
  $$('#recGrid .rec-open').forEach(b => {
    b.onclick = (ev) => { ev.stopPropagation(); viewReport(b.dataset.id); };
  });
  $$('#recGrid .rec-pdf').forEach(b => {
    b.onclick = (ev) => { ev.stopPropagation(); exportRecord(b.dataset.id); };
  });
  $$('#recGrid .rec-del').forEach(b => {
    b.onclick = (ev) => { ev.stopPropagation(); deleteOneRecord(b.dataset.id); };
  });
  updateSelBar();
}

/* 查看报告：打开该记录并直接跳到「报告」板块 */
async function viewReport(id) {
  try {
    const cur = state.current;
    if (!cur || cur.id !== id) await openDataset(id);
    if (!state.current || state.current.id !== id) return;
    if (!state.reportMd) {
      state.reportMd = await (await api(`/api/datasets/${id}/report`)).text();
    }
    showView('result');
    switchTab('report');
  } catch (e) {
    toast('打开报告失败：' + e.message, true);
  }
}

/* 从记录板块直接导出：先打开该记录（加载结果与报告），再走标准六板块导出 */
async function exportRecord(id) {
  try {
    const cur = state.current;
    if (!cur || cur.id !== id) await openDataset(id);
    if (!state.result || !state.current || state.current.id !== id) {
      toast('该记录的结果尚未就绪，无法导出', true);
      return;
    }
    showView('result');
    await exportPdf('all');
  } catch (e) {
    toast('导出失败：' + e.message, true);
  }
}

async function deleteOneRecord(id) {
  const d = state.datasets.find(x => x.id === id);
  const label = d ? `${d.platform} · ${d.category}` : id;
  if (!confirm(`确认删除分析报告「${label}」？\n\n该报告的全部产出文件（结果、报告、原声）与上传的原始数据文件会被一并清除，且不可撤销。`)) return;
  await performDelete([id], label);
}

function bindRecordsBoard() {
  const head = $('#btnRecords');
  if (head) head.onclick = () => {
    // 从侧边栏进入时跟随顶部当前平台板块
    state.recScope = 'platform';
    location.hash = 'records';
    showView('records');
    renderRecordsBoard();
  };
  const pb = $('#btnPlatRecords');
  if (pb) pb.onclick = () => {
    state.recScope = 'platform';
    location.hash = 'records';
    showView('records');
    renderRecordsBoard();
  };
  const q = $('#recQuery');
  if (q) q.oninput = debounce(() => { state.recQuery = q.value; renderRecordsBoard(); }, 180);
  const pl = $('#recPlat');
  if (pl) pl.onchange = () => { state.dsFilter = pl.value; saveDsPrefs(); renderDatasets(); };
  const so = $('#recSort');
  if (so) so.onclick = () => {
    state.dsSort = state.dsSort === 'desc' ? 'asc' : 'desc';
    saveDsPrefs(); renderDatasets();
  };
  const sb = $('#recSelBtn');
  if (sb) sb.onclick = () => setSelMode(!state.selMode);
  const cb = $('#recCancelBtn');
  if (cb) cb.onclick = () => setSelMode(false);
  const db = $('#recDelBtn');
  if (db) db.onclick = deleteSelected;
  const cl = $('#recCleanBtn');
  if (cl) cl.onclick = runCleanup;
  const nb = $('#recNewAnalysis');
  if (nb) nb.onclick = () => $('#btnNew').click();
}

/* ============================ 新建 / 上传 ============================ */

function bindSetupEvents() {
  const dz = $('#dropzone'), fi = $('#fileInput');
  dz.onclick = () => fi.click();
  fi.onchange = () => { addFiles(Array.from(fi.files)); fi.value = ''; };
  ['dragenter', 'dragover'].forEach(ev => dz.addEventListener(ev, e => {
    e.preventDefault(); dz.classList.add('over');
  }));
  ['dragleave', 'drop'].forEach(ev => dz.addEventListener(ev, e => {
    e.preventDefault(); dz.classList.remove('over');
  }));
  dz.addEventListener('drop', e => {
    if (e.dataTransfer && e.dataTransfer.files) addFiles(Array.from(e.dataTransfer.files));
  });
  $('#btnStart').onclick = startAnalysis;
  $('#errorClose').onclick = clearError;
  $('#enginePill').onclick = testEngine;
  $('#fCategory').oninput = updateStartState;
}

function addFiles(files) {
  for (const f of files) {
    if (!state.files.some(x => x.name === f.name && x.size === f.size)) state.files.push(f);
  }
  renderFileList();
  updateStartState();
}

function renderFileList() {
  $('#fileList').innerHTML = state.files.map((f, i) => `
    <div class="file-row">
      <span>📄</span>
      <span class="fname">${esc(f.name)}</span>
      <span class="fsize">${(f.size / 1024).toFixed(1)} KB</span>
      <button class="fdel" data-i="${i}" title="移除">✕</button>
    </div>`).join('');
  $$('#fileList .fdel').forEach(b => {
    b.onclick = () => { state.files.splice(+b.dataset.i, 1); renderFileList(); updateStartState(); };
  });
}

function updateStartState() {
  const plat = state.platforms.find(x => x.id === state.platform) || {};
  const needsCat = plat.kind !== 'aftersale';
  const ok = state.files.length > 0 &&
             (!needsCat || $('#fCategory').value.trim().length > 0);
  $('#btnStart').disabled = !ok;
  $('#runNote').className = 'run-note';
  $('#runNote').textContent = state.files.length === 0
    ? '请先上传数据文件'
    : (!$('#fCategory').value.trim() ? '请填写品类名称' : '');
}

async function startAnalysis() {
  const p = state.platforms.find(x => x.id === state.platform) || {};
  const fd = new FormData();
  state.files.forEach(f => fd.append('files', f));
  fd.append('platform', state.platform);
  fd.append('category', $('#fCategory').value.trim());
  fd.append('brands', $('#fBrands').value.trim());
  fd.append('comment_limit', $('#fCommentLimit').value || '20');
  fd.append('dataset_name', $('#fName').value.trim());
  fd.append('engine', ($('#fEngine') && $('#fEngine').value) || 'auto');

  $('#btnStart').disabled = true;
  $('#runNote').textContent = '正在上传…';
  clearError();
  clearWarnings();
  try {
    const { id } = await (await api('/api/upload', { method: 'POST', body: fd })).json();
    // 暂不清空已选文件：若随后分析失败，用户可直接改配置重试，无需重新选文件
    await loadDatasets();
    watchJob(id);
  } catch (e) {
    $('#btnStart').disabled = false;
    $('#runNote').className = 'run-note err';
    $('#runNote').textContent = '上传失败';
    showError('上传失败：' + e.message);
  }
}

/* ============================ 进度 ============================ */

function watchJob(id) {
  showView('progress');
  $('#progTitle').textContent = '正在分析…';
  $('#progFill').style.width = '0%';
  $('#progMsg').textContent = '准备中…';
  $('#progPct').textContent = '0%';

  const es = new EventSource(`/api/datasets/${id}/events`);
  es.onmessage = (ev) => {
    const d = JSON.parse(ev.data);
    $('#progFill').style.width = d.progress + '%';
    $('#progMsg').textContent = d.message || '';
    $('#progPct').textContent = d.progress + '%';
    if (d.status === 'done') {
      es.close();
      $('#progTitle').textContent = '分析完成';
      state.files = []; renderFileList(); updateStartState();   // 成功后才清空待上传列表
      loadDatasets().then(() => openDataset(id));
    } else if (d.status === 'error') {
      es.close();
      showView('setup');
      $('#runNote').className = 'run-note err';
      $('#runNote').textContent = '分析失败';
      showError(d.error);
      toast('分析失败', true);
      loadDatasets();
    }
  };
  es.onerror = () => { es.close(); loadDatasets(); };
}

/* ============================ 结果 ============================ */

function bindResultEvents() {
  $$('#resultTabs button').forEach(b => {
    b.onclick = () => switchTab(b.dataset.tab);
  });
  $('#btnExportMd').onclick = () => {
    if (!state.current) return;
    window.open(`/api/datasets/${state.current.id}/report`, '_blank');
  };
  $('#btnExportPdf').onclick = () => exportPdf('all');
  $('#btnExportTabPdf').onclick = () => exportPdf('tab');
  $('#btnRerun').onclick = async () => {
    if (!state.current) return;
    try {
      await api(`/api/datasets/${state.current.id}/rerun`, { method: 'POST' });
      toast('已开始重新分析');
      watchJob(state.current.id);
    } catch (e) { toast('重跑失败：' + e.message, true); }
  };
  $('#btnDelete').onclick = async () => {
    if (!state.current) return;
    const id = state.current.id;
    const label = `${state.current.platform} · ${state.current.category}`;
    if (!confirm(`确认删除「${label}」的全部分析产出？\n\n报告、原始数据文件与全部图表数据都会被清除，且不可撤销。`)) return;
    await performDelete([id], label);
  };
  $('#drawerClose').onclick = closeDrawer;
  $('#drawerMask').onclick = closeDrawer;
  window.addEventListener('resize', debounce(resizeAllCharts, 160));
}

async function openDataset(id) {
  const rec = await (await api(`/api/datasets/${id}`)).json();
  state.current = rec;
  state.platform = rec.platform;
  syncGroupFromPlatform();
  applyPlatformTexts(rec.platform);
  renderDemos();
  renderDatasets();

  if (rec.status === 'running' || rec.status === 'queued') { watchJob(id); return; }
  if (rec.status === 'error') {
    showView('setup');
    $('#runNote').className = 'run-note err';
    $('#runNote').textContent = '上次分析失败';
    showError(rec.error);
    return;
  }
  state.result = await (await api(`/api/datasets/${id}/result`)).json();
  state.reportMd = await (await api(`/api/datasets/${id}/report`)).text();
  state.tab = 'overview';
  // 必须先让结果视图可见再渲染 —— 否则图表容器尺寸为 0，ECharts 无法初始化
  showView('result');
  if (state.result.report_type === 'aftersale') renderAftersale();
  else renderResult();
}

function renderResult() {
  const r = state.result, c = state.current;
  $('#resTitle').textContent = `${r.category} · ${r.platform}`;
  const ov = r.overview;
  const bits = [
    `${c.created_at}`,
    `${ov.units_analyzed} 个讨论单元`,
    ov.comments_total ? `${ov.comments_total} 条评论` : null,
    ov.avg_rating != null ? `平均 ${ov.avg_rating}★` : null,
    ov.date_min ? `${ov.date_min} ~ ${ov.date_max}` : null,
    engineLabel(r.engine),
    `${state.result.elapsed_sec != null ? state.result.elapsed_sec : (c.elapsed || '-')}s`,
  ].filter(Boolean);
  $('#resMeta').innerHTML = bits.map(b => esc(b)).join('<span class="sep">·</span>');
  showWarnings(c.warnings);

  $('#resultTabs').hidden = false;
  $$('#resultTabs button').forEach(b => b.classList.toggle('active', b.dataset.tab === 'overview'));
  $$('.panel').forEach(p => { p.hidden = p.id !== 'panel-overview'; });
  renderOverview();
  // 布局稳定后再同步一次尺寸；若此时仍无图表实例（容器此前不可见），补渲染一次
  setTimeout(() => {
    if (!Object.keys(state.charts).length) renderOverview();
    else resizeAllCharts();
  }, 60);
}

function renderAftersale() {
  const r = state.result, c = state.current;
  $('#resTitle').textContent = `${r.platform} · 售后分析`;
  const a = r.aftersale;
  const plat = state.platforms.find(x => x.id === r.platform) || {};
  const color = plat.color || '#FF7A00';
  const overall = (a.overall_rate * 100).toFixed(1) + '%';
  $('#resMeta').innerHTML = [
    `${c.created_at}`,
    `${a.total_orders} 单`,
    `${a.aftersale_orders} 笔售后`,
    `整体售后率 ${overall}`,
    `${state.result.elapsed_sec != null ? state.result.elapsed_sec : (c.elapsed || '-')}s`,
  ].map(esc).join('<span class="sep">·</span>');
  showWarnings(c.warnings);
  $('#resultTabs').hidden = true;
  $$('.panel').forEach(p => p.hidden = p.id !== 'panel-overview');

  const html = `
    <div class="as-kpis">
      <div class="as-kpi"><div class="as-kpi-v">${a.total_orders}</div><div class="as-kpi-l">总订单数</div></div>
      <div class="as-kpi"><div class="as-kpi-v">${a.aftersale_orders}</div><div class="as-kpi-l">售后订单数</div></div>
      <div class="as-kpi acc"><div class="as-kpi-v">${overall}</div><div class="as-kpi-l">整体售后率</div></div>
      <div class="as-kpi"><div class="as-kpi-v">${a.products.length}</div><div class="as-kpi-l">涉及产品</div></div>
    </div>
    <div class="as-grid">
      <div class="as-card">
        <h3>各产品售后率</h3>
        <div id="asRateChart" class="chart-380"></div>
      </div>
      <div class="as-card">
        <h3>售后原因分布</h3>
        <div id="asReasonChart" class="chart-380"></div>
      </div>
    </div>
    <div class="as-card">
      <h3>各产品售后明细</h3>
      <table class="as-table">
        <thead><tr><th>产品</th><th>总单数</th><th>售后单数</th><th>售后率</th><th>趋势</th></tr></thead>
        <tbody>
          ${a.products.map(p => `
            <tr>
              <td>${esc(p.product)}</td>
              <td>${p.total}</td>
              <td>${p.aftersale}</td>
              <td>${(p.rate * 100).toFixed(1)}%</td>
              <td><div class="bar"><i style="width:${(p.rate * 100).toFixed(1)}%"></i></div></td>
            </tr>`).join('')}
        </tbody>
      </table>
    </div>`;
  $('#panel-overview').innerHTML = html;
  // 复用主题色
  document.documentElement.style.setProperty('--accent', color);
  setTimeout(() => { renderAsCharts(a, color); resizeAllCharts(); }, 60);
}

function renderAsCharts(a, color) {
  if (typeof echarts === 'undefined') return;
  const barEl = document.getElementById('asRateChart');
  if (barEl) {
    const chart = echarts.init(barEl);
    state.charts['asRateChart'] = chart;
    const top = a.products.slice(0, 15);
    chart.setOption({
      grid: { left: 8, right: 28, top: 16, bottom: 8, containLabel: true },
      tooltip: { trigger: 'axis', formatter: p => `${p[0].name}<br/>售后率 ${(p[0].value * 100).toFixed(1)}%` },
      xAxis: { type: 'value', axisLabel: { formatter: v => (v * 100).toFixed(0) + '%' }, max: 1 },
      yAxis: { type: 'category', data: top.map(p => p.product).reverse(),
               axisLabel: { width: 130, overflow: 'truncate' } },
      series: [{ type: 'bar', data: top.map(p => p.rate).reverse(), itemStyle: { color: color },
                label: { show: true, position: 'right', formatter: p => (p.value * 100).toFixed(1) + '%' } }]
    });
  }
  const pieEl = document.getElementById('asReasonChart');
  if (pieEl) {
    const chart = echarts.init(pieEl);
    state.charts['asReasonChart'] = chart;
    chart.setOption({
      tooltip: { trigger: 'item', formatter: p => `${p.name}<br/>${p.value} 笔 (${p.percent.toFixed(1)}%)` },
      legend: { type: 'scroll', orient: 'vertical', right: 6, top: 8, textStyle: { fontSize: 11 } },
      series: [{ type: 'pie', radius: ['38%', '66%'], center: ['36%', '52%'],
                 data: a.reasons.map(r => ({ name: r.reason, value: r.count })),
                 label: { show: false } }]
    });
  }
}

function bindAftersaleUI() {
  $('#btnAsSummary').onclick = openAftersaleSummary;
  $('#asModalClose').onclick = () => { $('#asModal').hidden = true; };
  $('#asModal').addEventListener('click', e => { if (e.target.id === 'asModal') $('#asModal').hidden = true; });
}

function updateGroupUI() {
  const btn = $('#btnAsSummary');
  if (btn) btn.hidden = (state.group !== '售后VOC');
}

async function openAftersaleSummary() {
  const m = $('#asModal');
  m.hidden = false;
  const body = $('#asModalBody');
  body.classList.add('loading');
  try {
    const d = await (await api('/api/aftersale/summary')).json();
    renderAftersaleSummary(d);
  } catch (e) {
    body.innerHTML = `<p class="err">加载失败：${esc(e.message || e)}</p>`;
  }
}

function renderAftersaleSummary(d) {
  const body = $('#asModalBody');
  body.classList.remove('loading');
  const ov = d.overall;
  $('#asSumKpis').innerHTML = `
    <div class="as-kpi"><div class="as-kpi-v">${ov.total}</div><div class="as-kpi-l">总订单数</div></div>
    <div class="as-kpi"><div class="as-kpi-v">${ov.aftersale}</div><div class="as-kpi-l">售后订单数</div></div>
    <div class="as-kpi acc"><div class="as-kpi-v">${(ov.rate * 100).toFixed(1)}%</div><div class="as-kpi-l">整体售后率</div></div>
    <div class="as-kpi"><div class="as-kpi-v">${d.datasets}</div><div class="as-kpi-l">已分析数据集</div></div>`;
  $('#asSumTable').innerHTML = `<thead><tr><th>平台</th><th>产品</th><th>总单数</th><th>售后单数</th><th>售后率</th><th>趋势</th></tr></thead><tbody>` +
    d.products.map(p => `<tr><td>${esc(p.platform)}</td><td>${esc(p.product)}</td><td>${p.total}</td><td>${p.aftersale}</td><td>${(p.rate * 100).toFixed(1)}%</td><td><div class="bar"><i style="width:${(p.rate * 100).toFixed(1)}%"></i></div></td></tr>`).join('') + `</tbody>`;
  if (typeof echarts !== 'undefined') {
    const bar = echarts.init(document.getElementById('asSumRateChart'));
    state.charts['asSumRateChart'] = bar;
    const top = d.products.slice(0, 20);
    bar.setOption({
      grid: { left: 8, right: 28, top: 16, bottom: 8, containLabel: true },
      tooltip: { trigger: 'axis', formatter: p => `${p[0].name}<br/>售后率 ${(p[0].value * 100).toFixed(1)}%` },
      xAxis: { type: 'value', axisLabel: { formatter: v => (v * 100).toFixed(0) + '%' }, max: 1 },
      yAxis: { type: 'category', data: top.map(p => `${p.platform}·${p.product}`).reverse(),
               axisLabel: { width: 170, overflow: 'truncate' } },
      series: [{ type: 'bar', data: top.map(p => p.rate).reverse(), itemStyle: { color: '#9B27B0' },
                label: { show: true, position: 'right', formatter: p => (p.value * 100).toFixed(1) + '%' } }]
    });
    const pie = echarts.init(document.getElementById('asSumReasonChart'));
    state.charts['asSumReasonChart'] = pie;
    pie.setOption({
      tooltip: { trigger: 'item', formatter: p => `${p.name}<br/>${p.value} 笔 (${p.percent.toFixed(1)}%)` },
      legend: { type: 'scroll', orient: 'vertical', right: 6, top: 8, textStyle: { fontSize: 11 } },
      series: [{ type: 'pie', radius: ['38%', '66%'], center: ['36%', '52%'],
                data: d.reasons.map(r => ({ name: r.reason, value: r.count })), label: { show: false } }]
    });
  }
}

function switchTab(tab) {
  state.tab = tab;
  $$('#resultTabs button').forEach(b => b.classList.toggle('active', b.dataset.tab === tab));
  $$('.panel').forEach(p => { p.hidden = p.id !== 'panel-' + tab; });
  destroyCharts();
  renderByTab(tab);
  resizeAllCharts();
  syncHash(tab);
}

/* 按板块 key 渲染对应面板（供切换页签与导出 PDF 复用） */
function renderByTab(tab) {
  if (tab === 'overview') renderOverview();
  else if (tab === 'dimensions') renderDimensions();
  else if (tab === 'features') renderFeatures();
  else if (tab === 'persona') renderPersona();
  else if (tab === 'voice') renderVoice();
  else if (tab === 'report') renderReport();
}

/* 把当前记录 + 页签写进 URL hash，便于分享与刷新保持位置。
   注意：hash 变化会触发 hashchange，而处理器里用 `target !== state.tab`
   做短路，因此不会递归。 */
function syncHash(tab) {
  if (!state.current) return;
  const next = `#ds=${state.current.id}&tab=${tab || state.tab}`;
  if (location.hash !== next) location.hash = next;
}

/* ============================ 图表工具 ============================ */

/* 图表实例管理：容器尺寸变化时自动 resize（避免初始化早于布局完成导致画布偏小） */
let _chartRO = null;
function ensureChartRO() {
  if (_chartRO || typeof ResizeObserver === 'undefined') return;
  _chartRO = new ResizeObserver((entries) => {
    for (const e of entries) {
      const inst = state.charts[e.target.id];
      if (!inst) continue;
      try { inst.resize(); } catch (_) {}
    }
  });
}

function chart(elId, option, height) {
  const el = document.getElementById(elId);
  if (!el) return null;
  if (state.charts[elId]) {
    try { state.charts[elId].dispose(); } catch (e) {}
    delete state.charts[elId];
  }
  // 容器不可见（宽高为 0）时跳过；等可见后由 ResizeObserver / resizeAllCharts 补画
  if (!el.clientWidth && !el.offsetWidth) return null;
  // 导出 PDF 时关闭动画：图表一次性画到终态，getDataURL 抓到的就是完整画面
  if (state.noAnim) option = Object.assign({ animation: false }, option || {});
  const c = echarts.init(el, null, { renderer: 'canvas' });
  c.setOption(option);
  state.charts[elId] = c;
  ensureChartRO();
  if (_chartRO) { try { _chartRO.observe(el); } catch (e) {} }
  return c;
}

function destroyCharts() {
  Object.entries(state.charts).forEach(([id, c]) => {
    try { if (_chartRO) _chartRO.unobserve(document.getElementById(id)); } catch (e) {}
    try { c.dispose(); } catch (e) {}
  });
  state.charts = {};
}

function resizeAllCharts() {
  requestAnimationFrame(() => {
    Object.entries(state.charts).forEach(([id, c]) => {
      try { c.resize(); } catch (e) {}
    });
  });
}

function debounce(fn, ms) {
  let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}

/* 横向条形图 */
function barH(elId, items, opts = {}) {
  const list = items.slice(0, opts.limit || 12).slice().reverse();
  const color = opts.color || getComputedStyle(document.documentElement).getPropertyValue('--accent').trim();
  ChartClickHandlers[elId] = opts.onClick || null;
  return chart(elId, {
    grid: { left: 8, right: 46, top: 8, bottom: 8, containLabel: true },
    tooltip: Object.assign({}, TOOLTIP, {
      trigger: 'axis', axisPointer: { type: 'shadow' },
      formatter: (ps) => {
        const p = ps[0]; const it = list[p.dataIndex] || {};
        return `<b>${esc(it.label_cn || p.name)}</b><br/>命中 ${it.count} 个单元` +
               (it.mention_rate != null ? `　提及率 ${it.mention_rate}%` : '') +
               (it.percentage != null ? `<br/>占本维度 ${it.percentage}%` : '');
      }
    }),
    xAxis: { type: 'value', axisLine: AXIS_LINE, axisLabel: AXIS_LABEL, splitLine: SPLIT_LINE },
    yAxis: {
      type: 'category',
      data: list.map(x => x.label_cn),
      axisLine: { show: false }, axisTick: { show: false },
      axisLabel: { color: '#4b5563', fontSize: 11.5, width: 96, overflow: 'truncate' }
    },
    series: [{
      type: 'bar', data: list.map(x => x.count),
      barMaxWidth: 17, itemStyle: { color, borderRadius: [0, 4, 4, 0] },
      label: { show: true, position: 'right', color: '#8b93a1', fontSize: 11 }
    }]
  });
}

const ChartClickHandlers = {};
// 统一绑定点击
(function patchChartClick() {
  const orig = chart;
  window.chart = function (elId, option, height) {
    const c = orig(elId, option, height);
    if (c) {
      c.on('click', (p) => {
        const h = ChartClickHandlers[elId];
        if (h) h(p);
      });
    }
    return c;
  };
})();

/* 环形图 */
function donut(elId, data, opts = {}) {
  ChartClickHandlers[elId] = opts.onClick || null;
  return chart(elId, {
    tooltip: Object.assign({}, TOOLTIP, { trigger: 'item', formatter: '{b}<br/>{c} 个单元（{d}%）' }),
    legend: { bottom: 0, icon: 'circle', itemWidth: 8, itemHeight: 8, textStyle: { color: '#4b5563', fontSize: 11.5 } },
    series: [{
      type: 'pie', radius: ['48%', '70%'], center: ['50%', '44%'],
      avoidLabelOverlap: true,
      itemStyle: { borderColor: '#fff', borderWidth: 2 },
      label: { show: false }, labelLine: { show: false },
      data
    }]
  });
}

/* 正/负/中性 堆叠条形 */
function stackedSentiment(elId, items, opts = {}) {
  const list = items.slice(0, opts.limit || 10).slice().reverse();
  return chart(elId, {
    grid: { left: 8, right: 30, top: 26, bottom: 8, containLabel: true },
    tooltip: Object.assign({}, TOOLTIP, { trigger: 'axis', axisPointer: { type: 'shadow' } }),
    legend: { top: 0, right: 0, icon: 'circle', itemWidth: 8, itemHeight: 8, textStyle: { color: '#4b5563', fontSize: 11.5 } },
    xAxis: { type: 'value', axisLine: AXIS_LINE, axisLabel: AXIS_LABEL, splitLine: SPLIT_LINE },
    yAxis: {
      type: 'category', data: list.map(x => x.name),
      axisLine: { show: false }, axisTick: { show: false },
      axisLabel: { color: '#4b5563', fontSize: 11.5, width: 100, overflow: 'truncate' }
    },
    series: [
      { name: '正面', type: 'bar', stack: 's', data: list.map(x => x.positive), itemStyle: { color: C_POS }, barMaxWidth: 16 },
      { name: '负面', type: 'bar', stack: 's', data: list.map(x => x.negative), itemStyle: { color: C_NEG }, barMaxWidth: 16 },
      { name: '中性', type: 'bar', stack: 's', data: list.map(x => x.neutral), itemStyle: { color: C_NEU }, barMaxWidth: 16 },
    ]
  });
}

/* 满意度散点（x=提及数, y=满意度, size=提及数） */
function scatterSatisfaction(elId, items, opts = {}) {
  const list = items.filter(x => x.satisfaction != null && x.total > 0);
  const color = getComputedStyle(document.documentElement).getPropertyValue('--accent').trim();
  ChartClickHandlers[elId] = opts.onClick || null;
  return chart(elId, {
    grid: { left: 8, right: 26, top: 22, bottom: 30, containLabel: true },
    tooltip: Object.assign({}, TOOLTIP, {
      formatter: (p) => {
        const d = p.data;
        return `<b>${esc(d.name)}</b><br/>提及 ${d.value[0]} 个单元<br/>满意度 ${(d.value[1] * 100).toFixed(0)}%` +
               `<br/><span style="color:${C_POS}">正 ${d.pos}</span>　<span style="color:${C_NEG}">负 ${d.neg}</span>`;
      }
    }),
    xAxis: Object.assign({ type: 'value', name: '提及单元数', nameTextStyle: { color: '#8b93a1', fontSize: 11 } }, AXIS_LINE, AXIS_LABEL, SPLIT_LINE),
    yAxis: Object.assign({ type: 'value', min: 0, max: 1, name: '满意度', nameTextStyle: { color: '#8b93a1', fontSize: 11 }, axisLabel: { color: '#8b93a1', fontSize: 11, formatter: v => (v * 100) + '%' } }, AXIS_LINE, SPLIT_LINE),
    series: [{
      type: 'scatter',
      symbolSize: (v) => Math.max(11, Math.min(40, 10 + v[0] * 0.7)),
      data: list.map(x => ({
        name: x.name, value: [x.total, x.satisfaction],
        pos: x.positive, neg: x.negative, total: x.total,
        itemStyle: { color: x.satisfaction >= 0.8 ? C_POS : x.satisfaction >= 0.5 ? '#f59e0b' : C_NEG, opacity: 0.82 }
      })),
      label: {
        show: true, position: 'top', fontSize: 10.5, color: '#4b5563',
        formatter: (p) => p.data.name
      },
      labelLayout: { hideOverlap: true },
      markLine: {
        silent: true, symbol: 'none',
        lineStyle: { color: '#e5e7eb', type: 'dashed' },
        label: { show: false },
        data: [{ yAxis: 0.8 }]
      }
    }]
  });
}

/* 热力图（通用矩阵） */
/* 交叉矩阵转置：把「人群→功能」换成「功能→人群」 */
function transposeMatrix(matrix) {
  const out = {};
  Object.keys(matrix).forEach(r => {
    Object.keys(matrix[r]).forEach(c => {
      (out[c] = out[c] || {})[r] = matrix[r][c];
    });
  });
  return out;
}

/* 热力图
   opts.colorBy: 'satisfaction'（默认，色=满意度，绿系）
                 'total'（色=提及量，蓝系；单元格数字仍是满意度）
   两个页面用同一份矩阵但不同取向与配色，回答的是不同问题。 */
function heatmap(elId, matrix, opts = {}) {
  const rows = Object.keys(matrix);
  if (!rows.length) return chart(elId, { title: { text: '暂无足够交叉样本', left: 'center', top: 'middle', textStyle: { color: '#b3bac5', fontSize: 13, fontWeight: 'normal' } } });
  const cols = Array.from(new Set(rows.flatMap(r => Object.keys(matrix[r]))));
  const colorBy = opts.colorBy || 'satisfaction';
  const data = [];
  let maxTotal = 1;
  /* 满意度配色：用于「色块=提及量」的视图里给数字上色，
     这样低满意的格子即使讨论量少也能被一眼挑出来。 */
  const satColor = (sat) => sat < 0 ? '#94a3b8'
    : sat >= 0.8 ? '#15803d' : sat >= 0.5 ? '#b45309' : '#b91c1c';
  rows.forEach((r, i) => cols.forEach((c, j) => {
    const cell = matrix[r][c];
    if (!cell) return;
    const sat = cell.satisfaction == null ? -1 : cell.satisfaction;
    data.push({
      value: [j, i, colorBy === 'total' ? cell.total : (sat < 0 ? 0 : sat)],
      total: cell.total, sat, name: `${r} × ${c}`,
      ...(colorBy === 'total' ? { label: { color: satColor(sat) } } : {}),
    });
    maxTotal = Math.max(maxTotal, cell.total);
  }));
  const vm = colorBy === 'total'
    ? { min: 0, max: maxTotal, text: ['提及多', '提及少'],
        inRange: { color: ['#DCE9F8', '#A9C9EE', '#5E93D6'] } }
    : { min: 0, max: 1, text: ['高满意', '低满意'],
        inRange: { color: ['#fca5a5', '#fde68a', '#bbf7d0', '#4ade80'] } };
  ChartClickHandlers[elId] = opts.onClick || null;
  return chart(elId, {
    grid: { left: 8, right: 20, top: 10, bottom: 60, containLabel: true },
    tooltip: Object.assign({}, TOOLTIP, {
      formatter: (p) => {
        const d = p.data;
        if (!d || d.total == null) return '';
        return `<b>${esc(d.name)}</b><br/>提及 ${d.total} 个单元<br/>`
          + `满意度 ${d.sat < 0 ? '—' : (d.sat * 100).toFixed(0) + '%'}`
          + `<br/><span style="color:#8b93a1">色块深浅表示${colorBy === 'total' ? '提及量' : '满意度'}</span>`;
      }
    }),
    xAxis: {
      type: 'category', data: cols, splitArea: { show: true },
      axisLabel: { color: '#4b5563', fontSize: 10.5, rotate: 32, interval: 0 },
      axisLine: { show: false }, axisTick: { show: false }
    },
    yAxis: {
      type: 'category', data: rows, splitArea: { show: true },
      axisLabel: { color: '#4b5563', fontSize: 11 },
      axisLine: { show: false }, axisTick: { show: false }
    },
    visualMap: Object.assign({
      orient: 'horizontal', left: 'center', bottom: 0,
      itemWidth: 12, itemHeight: 90,
      textStyle: { color: '#8b93a1', fontSize: 10.5 },
      calculable: false,
    }, vm),
    series: [{
      type: 'heatmap', data,
      label: {
        show: true, fontSize: 10,
        color: colorBy === 'total' ? '#0A3A66' : '#1f242c', fontWeight: 600,
        formatter: (p) => p.data.sat < 0 ? '' : (p.data.sat * 100).toFixed(0)
      },
      itemStyle: { borderColor: '#fff', borderWidth: 2, borderRadius: 3 },
      emphasis: { itemStyle: { shadowBlur: 8, shadowColor: 'rgba(0,0,0,.18)' } }
    }]
  });
}

/* 差异发散条形图 */
function divergingBar(elId, items, opts = {}) {
  const list = items.slice(0, opts.limit || 10).slice().reverse();
  const leftKey = opts.leftKey, rightKey = opts.rightKey;
  const labels = list.map(x => x.label_cn);
  const posColor = opts.posColor || '#2f6df6', negColor = opts.negColor || '#f59e0b';
  return chart(elId, {
    grid: { left: 8, right: 20, top: 26, bottom: 8, containLabel: true },
    tooltip: Object.assign({}, TOOLTIP, { trigger: 'axis', axisPointer: { type: 'shadow' } }),
    legend: { top: 0, right: 0, icon: 'circle', itemWidth: 8, itemHeight: 8, textStyle: { color: '#4b5563', fontSize: 11.5 } },
    xAxis: { type: 'value', axisLabel: Object.assign({}, AXIS_LABEL, { formatter: v => v + '%' }), axisLine: AXIS_LINE, splitLine: SPLIT_LINE },
    yAxis: {
      type: 'category', data: labels,
      axisLine: { show: false }, axisTick: { show: false },
      axisLabel: { color: '#4b5563', fontSize: 11.5, width: 100, overflow: 'truncate' }
    },
    series: [
      { name: opts.leftName, type: 'bar', data: list.map(x => x[leftKey]), itemStyle: { color: posColor, borderRadius: [0, 4, 4, 0] }, barMaxWidth: 13 },
      { name: opts.rightName, type: 'bar', data: list.map(x => x[rightKey]), itemStyle: { color: negColor, borderRadius: [0, 4, 4, 0] }, barMaxWidth: 13 },
    ]
  });
}

/* 星级分布 */
function ratingBar(elId, dist) {
  const keys = ['1', '2', '3', '4', '5'];
  const colors = [C_NEG, '#f97066', '#f59e0b', '#84cc16', C_POS];
  return chart(elId, {
    grid: { left: 8, right: 16, top: 16, bottom: 8, containLabel: true },
    tooltip: Object.assign({}, TOOLTIP, { trigger: 'axis', axisPointer: { type: 'shadow' }, formatter: ps => `${ps[0].name} 星：${ps[0].value} 个单元` }),
    xAxis: { type: 'category', data: keys.map(k => k + '★'), axisLine: AXIS_LINE, axisTick: { show: false }, axisLabel: AXIS_LABEL },
    yAxis: { type: 'value', axisLine: { show: false }, axisLabel: AXIS_LABEL, splitLine: SPLIT_LINE },
    series: [{
      type: 'bar', barMaxWidth: 42,
      data: keys.map((k, i) => ({ value: dist[k] || 0, itemStyle: { color: colors[i], borderRadius: [4, 4, 0, 0] } })),
      label: { show: true, position: 'top', color: '#8b93a1', fontSize: 11 }
    }]
  });
}

/* 时间趋势 */
function timeline(elId, units) {
  const byMonth = {};
  units.forEach(u => {
    if (!u.date) return;
    const m = String(u.date).slice(0, 7);
    if (!m || m.length < 7) return;
    byMonth[m] = (byMonth[m] || 0) + 1;
  });
  const keys = Object.keys(byMonth).sort();
  if (!keys.length) return chart(elId, { title: { text: '暂无日期数据', left: 'center', top: 'middle', textStyle: { color: '#b3bac5', fontSize: 13, fontWeight: 'normal' } } });
  const accent = getComputedStyle(document.documentElement).getPropertyValue('--accent').trim();
  return chart(elId, {
    grid: { left: 8, right: 18, top: 16, bottom: 8, containLabel: true },
    tooltip: Object.assign({}, TOOLTIP, { trigger: 'axis', formatter: ps => `${ps[0].name}<br/>${ps[0].value} 个讨论单元` }),
    xAxis: { type: 'category', data: keys, axisLine: AXIS_LINE, axisTick: { show: false }, axisLabel: Object.assign({}, AXIS_LABEL, { rotate: keys.length > 8 ? 30 : 0 }) },
    yAxis: { type: 'value', axisLine: { show: false }, axisLabel: AXIS_LABEL, splitLine: SPLIT_LINE },
    series: [{
      type: 'line', smooth: true, data: keys.map(k => byMonth[k]),
      lineStyle: { color: accent, width: 2.5 },
      itemStyle: { color: accent },
      areaStyle: { color: { type: 'linear', x: 0, y: 0, x2: 0, y2: 1, colorStops: [{ offset: 0, color: hexA(accent.startsWith('#') ? accent : '#2f6df6', 0.24) }, { offset: 1, color: hexA(accent.startsWith('#') ? accent : '#2f6df6', 0.01) }] } },
      symbolSize: 6
    }]
  });
}

/* ============================ 各面板 ============================ */

function metricCard(label, value, unit, sub) {
  return `<div class="metric">
    <div class="m-label">${esc(label)}</div>
    <div class="m-value">${esc(value)}${unit ? `<small>${esc(unit)}</small>` : ''}</div>
    ${sub ? `<div class="m-sub">${esc(sub)}</div>` : ''}
  </div>`;
}

function chartBox(id, title, sub, h) {
  return `<div class="chart-box">
    <div class="cb-title">${esc(title)}</div>
    ${sub ? `<div class="cb-sub">${esc(sub)}</div>` : '<div style="height:10px"></div>'}
    <div id="${id}" class="chart ${h || ''}"></div>
  </div>`;
}

/* ---------- 概览 ---------- */
function renderOverview() {
  const r = state.result, ov = r.overview, kind = r.platform_kind;
  const pu = ov.purchase;
  const metrics = [
    metricCard('讨论单元', ov.units_analyzed, '', `共解析 ${ov.units_total} 条记录`),
  ];
  if (kind === 'forum') metrics.push(metricCard('纳入评论', ov.comments_total, '条', `${ov.units_with_comments} 个单元含评论`));
  if (ov.avg_rating != null) metrics.push(metricCard('平均星级', ov.avg_rating, '★', `覆盖 ${Object.values(ov.rating_dist).reduce((a, b) => a + b, 0)} 条评分`));
  metrics.push(metricCard('标签命中', ov.tag_hits, '次', '七维度累计'));
  if (kind === 'ecommerce') {
    metrics.push(metricCard('购买人群', ov.units_analyzed, '个单元', '电商评论者全部为购买者'));
  } else {
    metrics.push(metricCard('已购 / 未购', `${pu.purchased} / ${pu.not_purchased}`, '', '按发帖人立场判定'));
  }
  if (ov.experience_units != null) {
    metrics.push(metricCard('有一手使用经验', ov.experience_units, '个单元', '功能满意度评价的分母'));
  }

  const dims = r.dimensions;
  const topDims = DIM_ORDER.map(k => dims[k]).filter(d => d && d.items.length)
    .map(d => `${d.label_cn} ${d.total_hits}`).join('　·　');

  let html = `<div class="metric-grid">${metrics.join('')}</div>`;
  if (kind === 'ecommerce') {
    // 电商全是购买者，「购买状态分布」没有信息量 —— 换成有价值的四张图
    html += `<div class="chart-grid two">
        ${chartBox('chart-rating', '评分分布', '星级分布反映整体满意度结构', 'short')}
        ${chartBox('chart-topdim', '各维度命中量', '七维度累计命中次数', 'short')}
      </div>
      <div class="chart-grid two">
        ${chartBox('chart-timeline', '评论时间分布', '按月统计讨论单元数', 'short')}
        ${chartBox('chart-brands-mini', '高频品牌', '按提及单元数排序', 'short')}
      </div>`;
  } else {
    html += `<div class="chart-grid two">
        ${chartBox('chart-purchase', '购买状态分布', '互斥口径：已购 / 未购 / 状态不明', 'short')}
        ${chartBox('chart-timeline', '讨论时间分布', '按月统计讨论单元数', 'short')}
      </div>
      <div class="chart-grid two">
        ${chartBox('chart-topdim', '各维度命中量', '七维度累计命中次数', 'short')}
        ${chartBox('chart-brands-mini', '高频品牌', '按提及单元数排序', 'short')}
      </div>`;
  }
  html += `<div class="card" style="margin-top:4px">
      <div class="card-head"><div class="card-title">维度命中概览</div>
      <div class="card-sub">${esc(topDims)}</div></div>
      <div class="chart-box" style="border:none;box-shadow:none;padding:0">
        <div id="chart-dimbars" class="chart" style="height:260px"></div>
      </div>
    </div>`;
  $('#panel-overview').innerHTML = html;

  // 购买状态环形（电商全是购买者，该图不渲染）
  if (kind !== 'ecommerce') {
    const pdata = r.purchase_status.items.map((it) => ({
      name: it.label_cn, value: it.count,
      itemStyle: { color: it.key === 'purchased' ? C_PURCHASED : C_NOT_PURCHASED }
    }));
    const unk = pu.unknown;
    if (unk) pdata.push({ name: '状态不明', value: unk, itemStyle: { color: '#cbd5e1' } });
    donut('chart-purchase', pdata, {});
  }

  if (kind === 'ecommerce') ratingBar('chart-rating', ov.rating_dist);
  else timeline('chart-timeline', r.units);

  // 各维度命中量
  const dimItems = ['user_personas', 'usage_scenarios', 'pre_purchase_concerns',
                    'post_purchase_experience', 'features', 'unmet_needs']
    .map(k => ({ label_cn: r.dimensions[k] ? r.dimensions[k].label_cn : k, count: r.dimensions[k] ? r.dimensions[k].total_hits : 0 }));
  barH('chart-topdim', dimItems, { limit: 6 });

  barH('chart-brands-mini', ((r.brands || {}).items || []).map(b => ({ label_cn: b.name, count: b.count })), { limit: 8 });

  if (kind === 'ecommerce') timeline('chart-timeline', r.units);

  barH('chart-dimbars', dimItems, { limit: 6 });
}

/* ---------- 六维分析 ---------- */
function renderDimensions() {
  const r = state.result;
  const dims = DIM_ORDER.filter(k => r.dimensions[k] && r.dimensions[k].items.length);
  let html = `<div class="card" style="margin-bottom:16px">
      <div class="card-head"><div class="card-title">六维度分析框架</div>
      <div class="card-sub">人群画像 → 使用场景 → 购买前关注点 → 购买后体验 → 未满足需求　|　点击图表条目可查看支撑原声</div></div>
    </div>`;
  dims.forEach((k, i) => {
    const d = r.dimensions[k];
    html += `<div class="dim-block">
      <div class="dim-head">
        <h3>${i + 1}. ${esc(d.label_cn)}</h3>
        <span class="dim-note">${d.items.length} 个标签命中，累计 ${d.total_hits} 次</span>
      </div>
      <div class="chart-grid two">
        ${chartBox('chart-dim-' + k, d.label_cn + '分布', '按命中单元数排序', 'tall')}
        <div class="chart-box">
          <div class="cb-title">代表性原声</div>
          <div class="cb-sub">逐字引用，点击查看完整单元</div>
          <div class="ev-list">${
            d.items.slice(0, 4).map(it => (it.evidence || []).slice(0, 1).map(ev => `
              <div class="ev-item" data-uid="${esc(ev.unit_id)}">
                <div class="ev-quote">${esc(trim(ev.quote, 150))}</div>
                <div class="ev-foot">
                  <span class="pill ${ev.sentiment === 'positive' ? 'pos' : ev.sentiment === 'negative' ? 'neg' : 'neu'}">${sentLabel(ev.sentiment)}</span>
                  <span>${esc(it.label_cn)}</span>
                  <span>${esc(srcLabel(ev.source))}</span>
                </div>
              </div>`).join('')).join('')
          }</div>
        </div>
      </div>
    </div>`;
  });
  $('#panel-dimensions').innerHTML = html || '<div class="empty-state">本次数据未命中任何维度标签。</div>';

  dims.forEach(k => {
    const d = r.dimensions[k];
    barH('chart-dim-' + k, d.items, {
      limit: 12,
      onClick: (p) => {
        const it = d.items.slice(0, 12).slice().reverse()[p.dataIndex];
        if (it) openVoiceFor({ dim: k, tag: it.label_cn });
      }
    });
  });
  bindEvidenceClicks('#panel-dimensions');
}

function bindEvidenceClicks(scope) {
  $$(scope + ' .ev-item').forEach(el => {
    el.onclick = () => openUnit(el.dataset.uid);
  });
}

function trim(s, n) { s = String(s || ''); return s.length > n ? s.slice(0, n) + '…' : s; }
function sentLabel(s) { return s === 'positive' ? '正面' : s === 'negative' ? '负面' : s === 'neutral' ? '中性' : '—'; }
function srcLabel(s) { return s === 'comment' ? '💬 评论' : s === 'platform' ? '🏷 平台标注' : s === 'post' ? '📄 正文' : ''; }

/* ---------- 功能体验 ---------- */
function renderFeatures() {
  const r = state.result, f = r.features;
  const items = f.items || [];
  if (!items.length) {
    $('#panel-features').innerHTML = '<div class="empty-state">本次数据未命中功能体验标签。</div>';
    return;
  }
  const sorted = items.slice().sort((a, b) => b.total - a.total);
  const lowSat = items.filter(x => x.satisfaction != null && x.total >= 3)
    .sort((a, b) => a.satisfaction - b.satisfaction).slice(0, 5);

  let html = `<div class="card" style="margin-bottom:16px">
      <div class="card-head"><div class="card-title">功能体验与满意度</div>
      <div class="card-sub">满意度 = 正面 /（正面 + 负面），中性不计入分母　|　点击气泡或表格行可下钻到原声</div></div>
      ${lowSat.length ? `<div style="font-size:12.5px;color:#4b5563">满意度最低：` +
        lowSat.map(x => `<span class="pill neg" style="margin-left:4px">${esc(x.name)} ${(x.satisfaction * 100).toFixed(0)}%</span>`).join('') +
        `</div>` : ''}
    </div>`;

  html += `<div class="chart-grid two">
      ${chartBox('chart-feat-scatter', '满意度 × 提及量', '气泡大小 = 提及单元数；虚线为 80% 满意度参考线', 'tall')}
      ${chartBox('chart-feat-stack', '正负面构成', '堆叠显示每种功能的评价极性', 'tall')}
    </div>`;

  html += `<div class="tbl-wrap"><table class="tbl">
      <thead><tr>
        <th>功能维度</th><th class="num">提及单元</th><th class="num">提及率</th>
        <th class="num">正面</th><th class="num">负面</th><th class="num">中性</th>
        <th style="min-width:150px">满意度</th><th></th>
      </tr></thead><tbody>
      ${sorted.map(x => `<tr class="clickable" data-key="${esc(x.key)}">
        <td>${esc(x.name)}</td>
        <td class="num">${x.total}</td>
        <td class="num">${x.mention_rate}%</td>
        <td class="num" style="color:${C_POS}">${x.positive}</td>
        <td class="num" style="color:${C_NEG}">${x.negative}</td>
        <td class="num" style="color:${C_NEU}">${x.neutral}</td>
        <td>${satBar(x.satisfaction)}</td>
        <td style="color:#cbd5e1">›</td>
      </tr>`).join('')}
      </tbody></table></div>`;

  const pm = f.persona_feature_matrix || {};
  if (Object.keys(pm).length) {
    html += `<div class="chart-grid">
      ${chartBox('chart-persona-heat', '功能 × 人群：讨论热度与满意度',
                 '纵轴：功能　横轴：人群　|　色块深浅 = 提及量（越深讨论越多）　|　数字 = 满意度%，绿 ≥80% / 黄 50–80% / 红 <50%　|　点击单元格下钻', 'tall')}
    </div>`;
  }

  $('#panel-features').innerHTML = html;

  scatterSatisfaction('chart-feat-scatter', items, {
    onClick: (p) => { if (p.data && p.data.name) openVoiceFor({ feature: p.data.name }); }
  });
  stackedSentiment('chart-feat-stack', sorted, { limit: 10 });
  if (Object.keys(pm).length) {
    heatmap('chart-persona-heat', transposeMatrix(pm), {
      colorBy: 'total',
      onClick: (p) => {
        if (p.data && p.data.name) {
          const [feat, persona] = p.data.name.split(' × ');
          openVoiceFor({ persona, feature: feat });
        }
      }
    });
  }
  $$('#panel-features tr.clickable').forEach(tr => {
    tr.onclick = () => openVoiceFor({ feature: sorted.find(x => x.key === tr.dataset.key).name });
  });
}

function satBar(sat) {
  if (sat == null) return '<span class="sat-bar"><span style="color:#b3bac5;font-size:12px">样本不足</span></span>';
  const pct = Math.round(sat * 100);
  const color = pct >= 80 ? C_POS : pct >= 50 ? '#f59e0b' : C_NEG;
  return `<span class="sat-bar"><span class="sat-track"><span class="sat-fill" style="width:${pct}%;background:${color}"></span></span><span class="sat-num" style="color:${color}">${pct}%</span></span>`;
}

/* ---------- 人群与交叉 ---------- */
function renderPersona() {
  const r = state.result, kind = r.platform_kind;
  const pp = r.purchaser_personas || [], np_ = r.non_purchaser_personas || [];
  const gap = r.persona_gap || [], rgap = r.rating_gap || [];
  const isEcom = kind === 'ecommerce';

  // 电商（Amazon / Wayfair）的评论者全部都是购买者：
  // 若旧数据里还残留被判为「未购买」的单元，它们的画像也要并回购买人群，否则会丢样本。
  let personas = pp, mergedNote = '';
  if (isEcom && np_.length) {
    const acc = new Map(pp.map(x => [x.label_cn, { ...x }]));
    for (const x of np_) {
      const cur = acc.get(x.label_cn);
      if (cur) { cur.count += x.count; }
      else acc.set(x.label_cn, { ...x });
    }
    personas = [...acc.values()].sort((a, b) => b.count - a.count);
    mergedNote = `　|　含 ${np_.reduce((a, b) => a + b.count, 0)} 次早期判定的「未购」提及，已并入购买人群`;
  }

  let html = '';
  html += `<div class="card" style="margin-bottom:16px">
      <div class="card-head"><div class="card-title">${isEcom ? '购买人群画像' : '人群画像拆分'}</div>
      <div class="card-sub">${isEcom
        ? `电商评论者全部为购买者，因此这里只呈现<b>购买人群</b>画像；可结合下方「评分分层人群差异」区分好评与差评人群${mergedNote}`
        : 'Reddit 帖子多为选购者、评论区为老用户，两类画像差异最具参考价值'}</div></div>
    </div>`;

  if (isEcom) {
    html += `<div class="chart-grid">
      ${chartBox('chart-pp', '购买人群画像', `命中 ${personas.reduce((a, b) => a + b.count, 0)} 次　|　基于全部已分析单元`, 'tall')}
    </div>`;
  } else {
    html += `<div class="chart-grid two">
      ${chartBox('chart-pp', '已购买人群画像', `命中 ${pp.reduce((a, b) => a + b.count, 0)} 次`, '')}
      ${chartBox('chart-np', '未购买人群画像', np_.length ? `命中 ${np_.reduce((a, b) => a + b.count, 0)} 次` : '本次数据无明确未购画像', '')}
    </div>`;
    if (gap.length) {
      html += `<div class="chart-grid">
        ${chartBox('chart-gap', '两类人群画像差异', '正值为已购人群更突出，负值为未购人群更突出', 'tall')}
      </div>`;
    }
  }

  if (isEcom && rgap.length) {
    html += `<div class="chart-grid">
      ${chartBox('chart-rgap', '评分分层人群差异（好评 4-5★ vs 差评 1-2★）', '正值为好评人群更突出，负值为差评人群更突出', 'tall')}
    </div>`;
  }

  const pm = (r.features || {}).persona_feature_matrix || {};
  if (Object.keys(pm).length) {
    html += `<div class="chart-grid">
      ${chartBox('chart-cross-heat', '人群 × 功能：满意度视角',
                 '纵轴：人群　横轴：功能　|　色块深浅 = 满意度（越绿越满意）　|　单元格数字 = 满意度%　|　点击单元格下钻', 'tall')}
    </div>`;
  }

  const bm = (r.features || {}).brand_feature_matrix || {};
  if (Object.keys(bm).length) {
    html += `<div class="chart-grid">
      ${chartBox('chart-brand-heat', '品牌 × 功能满意度', '点击单元格下钻到原声', 'tall')}
    </div>`;
  }

  const bd = (r.brands || {}).items || [];
  if (bd.length) {
    html += `<div class="tbl-wrap"><table class="tbl">
      <thead><tr><th>品牌</th><th class="num">提及单元</th><th class="num">提及率</th>
      <th class="num">正面</th><th class="num">负面</th><th style="min-width:150px">满意度</th>
      <th class="num">已购占比</th></tr></thead><tbody>
      ${bd.slice(0, 20).map(b => `<tr class="clickable" data-brand="${esc(b.name)}">
        <td><b>${esc(b.name)}</b></td>
        <td class="num">${b.count}</td>
        <td class="num">${b.percentage}%</td>
        <td class="num" style="color:${C_POS}">${b.positive}</td>
        <td class="num" style="color:${C_NEG}">${b.negative}</td>
        <td>${satBar(b.satisfaction)}</td>
        <td class="num">${b.purchased_share}%</td>
      </tr>`).join('')}
      </tbody></table></div>`;
  } else {
    html += `<div class="card"><div class="card-head">
      <div class="card-title">品牌分布</div>
      <div class="card-sub">本次数据未识别出品牌提及。${
        r.platform_kind === 'ecommerce'
          ? '单商品评论集通常不含品牌词 —— 品牌维度更适合用商品名称或 SKU 分组。'
          : '可在新建分析时补充品牌清单（支持用 | 合并同一品牌的多种拼写）以提升识别率。'}</div>
    </div></div>`;
  }

  $('#panel-persona').innerHTML = html;

  // 统一配色语义：蓝 = 已购/购买人群，琥珀 = 未购人群（与下方差异图一致）
  barH('chart-pp', personas.map(x => ({ label_cn: x.label_cn, count: x.count, percentage: x.percentage })), { limit: 12, color: C_PURCHASED });
  if (!isEcom) {
    if (np_.length) barH('chart-np', np_.map(x => ({ label_cn: x.label_cn, count: x.count, percentage: x.percentage })), { limit: 10, color: C_NOT_PURCHASED });
    else chart('chart-np', { title: { text: '本次数据未识别出明确的未购买人群画像', left: 'center', top: 'middle', textStyle: { color: '#b3bac5', fontSize: 13, fontWeight: 'normal' } } });
  }

  if (!isEcom && gap.length) {
    divergingBar('chart-gap', gap, { limit: 10, leftKey: 'purchased_pct', rightKey: 'not_purchased_pct', leftName: '已购占比', rightName: '未购占比', posColor: C_PURCHASED, negColor: C_NOT_PURCHASED });
  }
  if (isEcom && rgap.length) {
    divergingBar('chart-rgap', rgap, { limit: 10, leftKey: 'high_pct', rightKey: 'low_pct', leftName: '好评占比', rightName: '差评占比', posColor: C_POS, negColor: C_NEG });
  }
  if (Object.keys(pm).length) {
    heatmap('chart-cross-heat', pm, {
      onClick: (p) => {
        if (p.data && p.data.name) {
          const [persona, feat] = p.data.name.split(' × ');
          openVoiceFor({ persona, feature: feat });
        }
      }
    });
  }
  if (Object.keys(bm).length) {
    heatmap('chart-brand-heat', bm, {
      onClick: (p) => {
        if (p.data && p.data.name) {
          const [brand, feat] = p.data.name.split(' × ');
          openVoiceFor({ brand, feature: feat });
        }
      }
    });
  }
  $$('#panel-persona tr.clickable').forEach(tr => {
    tr.onclick = () => openVoiceFor({ brand: tr.dataset.brand });
  });
}

/* ---------- 数据原声 ---------- */
function renderVoice() {
  const r = state.result;
  const allTags = [...new Set(r.units.flatMap(u => u.tags.map(t => t.label_cn)))].sort();
  const f = state.voiceFilter;

  let html = `<div class="card" style="margin-bottom:14px">
    <div class="card-head"><div class="card-title">数据原声</div>
    <div class="card-sub">全部讨论单元与命中的标签证据。点击卡片查看完整内容。</div></div>
    <div class="voice-toolbar">
      <input type="text" id="vq" placeholder="搜索标题 / 正文 / 产品…" value="${esc(f.q)}">
      <select id="vp">
        <option value="">全部购买状态</option>
        <option value="purchased"${f.purchase === 'purchased' ? ' selected' : ''}>已购买</option>
        <option value="not_purchased"${f.purchase === 'not_purchased' ? ' selected' : ''}>未购买</option>
      </select>
      <select id="vd">
        <option value="">全部维度</option>
        ${['user_personas', 'usage_scenarios', 'pre_purchase_concerns', 'post_purchase_experience', 'features', 'unmet_needs']
          .map(k => `<option value="${k}"${f.dim === k ? ' selected' : ''}>${esc(r.dimensions[k] ? r.dimensions[k].label_cn : k)}</option>`).join('')}
      </select>
      <select id="vt">
        <option value="">全部标签</option>
        ${allTags.map(t => `<option value="${esc(t)}"${f.tag === t ? ' selected' : ''}>${esc(t)}</option>`).join('')}
      </select>
      <button class="btn-ghost" id="vreset">重置</button>
    </div>
    <div class="voice-count" id="vcount"></div>
  </div>
  <div id="vlist"></div>`;

  $('#panel-voice').innerHTML = html;
  $('#vq').oninput = debounce(e => { f.q = e.target.value; drawVoiceList(); }, 200);
  $('#vp').onchange = e => { f.purchase = e.target.value; drawVoiceList(); };
  $('#vd').onchange = e => { f.dim = e.target.value; drawVoiceList(); };
  $('#vt').onchange = e => { f.tag = e.target.value; drawVoiceList(); };
  $('#vreset').onclick = () => { state.voiceFilter = emptyVoiceFilter(); renderVoice(); };
  drawVoiceList();
}

function drawVoiceList() {
  const r = state.result, f = state.voiceFilter;
  let list = r.units;
  if (f.purchase) list = list.filter(u => u.purchase_status === f.purchase);
  if (f.dim) list = list.filter(u => u.tags.some(t => t.dim === f.dim));
  if (f.tag) list = list.filter(u => u.tags.some(t => t.label_cn === f.tag));
  if (f.persona) list = list.filter(u => u.tags.some(t => t.dim === 'user_personas' && t.label_cn === f.persona));
  if (f.brand) list = list.filter(u => (u.brands || []).includes(f.brand));
  if (f.q) {
    const q = f.q.toLowerCase();
    list = list.filter(u => (u.title + ' ' + u.body + ' ' + u.product).toLowerCase().includes(q));
  }
  const active = [];
  if (f.persona) active.push(`人群「${f.persona}」`);
  if (f.tag) active.push(`标签「${f.tag}」`);
  if (f.brand) active.push(`品牌「${f.brand}」`);
  $('#vcount').innerHTML = `共 ${list.length} 个讨论单元`
    + (active.length ? `　·　已聚焦：${active.map(esc).join(' + ')}` : '')
    + (list.length !== r.units.length && !active.length ? `（已从 ${r.units.length} 个中筛选）` : '');
  if (!list.length) { $('#vlist').innerHTML = '<div class="empty-state">没有符合条件的讨论单元</div>'; return; }

  $('#vlist').innerHTML = list.slice(0, 200).map(u => {
    const shownTags = u.tags.filter(t => !f.dim || t.dim === f.dim).slice(0, 8);
    const rating = u.rating != null ? `${u.rating}★` : '';
    const sub = [u.platform, u.product ? trim(u.product, 34) : '', u.sku, u.subreddit, rating, u.date].filter(Boolean).join(' · ');
    return `<div class="voice-card" data-uid="${esc(u.unit_id)}">
      <div class="vc-head">
        <div class="vc-title">${esc(trim(u.title, 130) || '(无标题)')}</div>
        <div class="vc-side">${esc(sub)}</div>
      </div>
      ${u.body ? `<div class="vc-body">${esc(trim(u.body, 240))}</div>` : ''}
      <div class="vc-tags">
        <span class="pill ${u.purchase_status === 'purchased' ? 'dim' : 'neu'}">${u.purchase_status === 'purchased' ? '已购买' : u.purchase_status === 'not_purchased' ? '未购买' : '状态不明'}</span>
        ${shownTags.map(t => `<span class="vc-tag" data-uid="${esc(u.unit_id)}">${esc(t.label_cn)}</span>`).join('')}
        ${u.comment_count ? `<span class="vc-tag">💬 ${u.comment_count} 条评论</span>` : ''}
      </div>
    </div>`;
  }).join('');

  $$('#vlist .voice-card').forEach(c => { c.onclick = () => openUnit(c.dataset.uid); });
}

function openVoiceFor({ dim, tag, feature, brand, persona }) {
  state.voiceFilter = { q: '', purchase: '', dim: dim || '', platform: '',
                        tag: tag || feature || '', persona: persona || '', brand: brand || '' };
  switchTab('voice');
  const bits = [persona, feature || tag].filter(Boolean);
  toast(bits.length ? `已聚焦：${bits.join(' × ')}` : (brand ? `已聚焦品牌：${brand}` : ''));
}

/* ---------- 报告 ---------- */
function renderReport() {
  $('#panel-report').innerHTML = `
    <div class="report-toolbar">
      <button class="btn-ghost" id="btnCopyMd">复制 Markdown</button>
      <button class="btn-ghost" id="btnPrint">打印 / 存为 PDF</button>
    </div>
    <div class="report-md" id="reportBody">${mdToHtml(state.reportMd)}</div>`;
  $('#btnCopyMd').onclick = () => {
    navigator.clipboard.writeText(state.reportMd).then(() => toast('已复制到剪贴板'));
  };
  $('#btnPrint').onclick = () => window.print();
}

/* ---------- 极简 Markdown 渲染 ---------- */
function mdToHtml(md) {
  const lines = String(md || '').split('\n');
  let out = [], i = 0;
  const inline = (s) => esc(s)
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');

  while (i < lines.length) {
    const ln = lines[i];
    if (/^\s*$/.test(ln)) { i++; continue; }
    // 表格
    if (/^\|/.test(ln) && i + 1 < lines.length && /^\|[\s:|-]+\|$/.test(lines[i + 1])) {
      const cells = (s) => s.trim().replace(/^\||\|$/g, '').split('|').map(x => x.trim());
      const head = cells(ln);
      i += 2;
      const rows = [];
      while (i < lines.length && /^\|/.test(lines[i])) { rows.push(cells(lines[i])); i++; }
      out.push('<table><thead><tr>' + head.map(h => `<th>${inline(h)}</th>`).join('') + '</tr></thead><tbody>' +
        rows.map(r => '<tr>' + r.map(c => `<td>${inline(c)}</td>`).join('') + '</tr>').join('') + '</tbody></table>');
      continue;
    }
    let m;
    if ((m = ln.match(/^(#{1,6})\s+(.*)$/))) {
      const lvl = m[1].length;
      out.push(`<h${lvl}>${inline(m[2])}</h${lvl}>`); i++; continue;
    }
    if (/^(-{3,}|\*{3,})$/.test(ln.trim())) { out.push('<hr>'); i++; continue; }
    if (/^>\s?/.test(ln)) {
      const buf = [];
      while (i < lines.length && /^>\s?/.test(lines[i])) { buf.push(lines[i].replace(/^>\s?/, '')); i++; }
      out.push('<blockquote>' + buf.map(b => inline(b)).join('<br>') + '</blockquote>');
      continue;
    }
    if (/^\s*[-*+]\s+/.test(ln)) {
      const buf = [];
      while (i < lines.length && /^\s*[-*+]\s+/.test(lines[i])) { buf.push(inline(lines[i].replace(/^\s*[-*+]\s+/, ''))); i++; }
      out.push('<ul>' + buf.map(b => `<li>${b}</li>`).join('') + '</ul>');
      continue;
    }
    const buf = [];
    while (i < lines.length && !/^\s*$/.test(lines[i]) && !/^[|>#>]/.test(lines[i]) && !/^\s*[-*+]\s+/.test(lines[i].trim() ? lines[i] : 'x')) {
      if (/^\s*$/.test(lines[i]) || /^[|>#]/.test(lines[i]) || /^#{1,6}\s/.test(lines[i]) || /^\s*[-*+]\s+/.test(lines[i])) break;
      buf.push(inline(lines[i])); i++;
    }
    if (buf.length) out.push('<p>' + buf.join(' ') + '</p>');
    else i++;
  }
  return out.join('\n');
}

/* ---------- 抽屉：单元详情 ---------- */
function openUnit(uid) {
  const u = state.result.units.find(x => x.unit_id === uid);
  if (!u) return;
  $('#drawerTitle').textContent = '讨论单元详情';
  const meta = [u.platform, u.product || null, u.sku || null, u.subreddit ? 'r/' + u.subreddit : null,
                u.rating != null ? u.rating + '★' : null, u.date || null].filter(Boolean);
  const grouped = {};
  u.tags.forEach(t => { (grouped[t.dim_label] = grouped[t.dim_label] || []).push(t); });

  $('#drawerBody').innerHTML = `
    <div class="dw-section">
      <div class="dw-label">来源</div>
      <div class="dw-text">${meta.map(esc).join('　·　')}</div>
      ${u.url ? `<div style="margin-top:6px"><a href="${esc(u.url)}" target="_blank" rel="noopener" style="color:var(--accent);font-size:12.5px">查看原始链接 ↗</a></div>` : ''}
    </div>
    <div class="dw-section">
      <div class="dw-label">标题</div>
      <div class="dw-text">${esc(u.title || '(无)')}</div>
    </div>
    ${u.body ? `<div class="dw-section"><div class="dw-label">正文</div><div class="dw-text">${esc(u.body)}</div></div>` : ''}
    <div class="dw-section">
      <div class="dw-label">购买状态</div>
      <div class="dw-text">${u.purchase_status === 'purchased' ? '已购买' : u.purchase_status === 'not_purchased' ? '未购买' : '状态不明'}</div>
    </div>
    ${Object.keys(grouped).length ? `<div class="dw-section">
      <div class="dw-label">命中标签与证据（${u.tags.length}）</div>
      ${Object.entries(grouped).map(([dim, tags]) => `
        <div style="margin-bottom:12px">
          <div style="font-size:12px;color:var(--text-3);margin-bottom:7px">${esc(dim)}</div>
          ${tags.map(t => `<div class="dw-tag-row">
            <span class="pill ${t.sentiment === 'positive' ? 'pos' : t.sentiment === 'negative' ? 'neg' : 'neu'}">${esc(t.label_cn)}</span>
            <div style="flex:1">
              <div class="dw-quote">${esc(t.quote || '')}</div>
              <div style="font-size:11px;color:var(--text-3)">${esc(srcLabel(t.source))}${t.comment_id ? '　' + esc(t.comment_id) : ''}</div>
            </div>
          </div>`).join('')}
        </div>`).join('')}
    </div>` : ''}
    ${u.comments && u.comments.length ? `<div class="dw-section">
      <div class="dw-label">评论（${u.comments.length}${u.comment_total ? ' / 共 ' + u.comment_total + ' 条' : ''}）</div>
      ${u.comments.map(c => `<div class="dw-comment">
        <div class="au">${esc(c.author || '匿名')}</div>
        <div class="bd">${esc(c.body)}</div>
      </div>`).join('')}
    </div>` : ''}`;
  $('#drawer').hidden = false;
  $('#drawerMask').hidden = false;
}

function closeDrawer() {
  $('#drawer').hidden = true;
  $('#drawerMask').hidden = true;
}

/* ==================== 导出 PDF（六个板块） ==================== */

const EXPORT_CSS = `
html, body { background: #fff !important; }
/* 打印默认会省掉背景色，满意度色条 / 标签底色 / 卡片底纹会全部变白 —— 强制保留 */
html, body, * { -webkit-print-color-adjust: exact !important; print-color-adjust: exact !important; }
body.export-body { margin: 0; padding: 0; color: #15181d; }
.export-cover { padding: 4px 0 16px; border-bottom: 2px solid #15181d; margin-bottom: 20px; }
.export-brand { font-size: 11px; font-weight: 700; letter-spacing: 0.16em; color: #2f6df6; margin-bottom: 9px; }
.export-cover h1 { font-size: 22px; margin: 0 0 9px; letter-spacing: -0.01em; }
.export-cover p { font-size: 12.5px; color: #4b5563; line-height: 1.7; }
.export-toc { font-size: 12px; color: #4b5563; margin-top: 10px; }
.export-toc span { display: inline-block; margin-right: 12px; }
.export-section { page-break-before: always; padding: 0; }
.export-section:first-of-type { page-break-before: avoid; }
.export-h2 {
  font-size: 16px; padding-bottom: 8px; margin: 0 0 16px;
  border-bottom: 1px solid #e5e7eb; color: #15181d;
}
/* 打印时页面宽度远小于 1180px，会触发站点的单栏折叠；导出件强制保持双栏，
   否则图表被拉成一长条，页数翻倍且与屏幕所见不一致。 */
.export-section .chart-grid.two { grid-template-columns: 1fr 1fr !important; }
.export-section .chart-grid { gap: 12px; }
@media print {
  .export-section .chart-grid.two { grid-template-columns: 1fr 1fr !important; }
  .export-section .chart-grid { gap: 12px; }
}
.export-section .card, .export-section .chart-box, .export-section .tbl-wrap,
.export-section .metric, .export-section .voice-card { box-shadow: none !important; }
.export-section .chart { height: auto !important; min-height: 0 !important; }
.export-chart-img { width: 100%; height: auto; display: block; }
.export-section .chart-box { break-inside: avoid; }
.export-section .tbl-wrap { break-inside: auto; }
.export-section .card { break-inside: avoid; }
.export-section .ev-item { break-inside: avoid; }
/* 交互控件在纸质件里无意义 */
.voice-toolbar, .report-toolbar { display: none !important; }
.voice-card { break-inside: avoid; }
.export-foot {
  margin-top: 26px; padding-top: 12px; border-top: 1px solid #e5e7eb;
  font-size: 11px; color: #8b93a1; text-align: center;
}
@page { size: A4; margin: 12mm 11mm; }
`;

function nextFrame() {
  // 双 rAF 保证布局与绘制都已提交；再加超时兜底 —— 标签页被节流时 rAF 可能长时间不触发
  return new Promise(resolve => {
    let done = false;
    const fin = () => { if (!done) { done = true; resolve(); } };
    requestAnimationFrame(() => requestAnimationFrame(fin));
    setTimeout(fin, 150);
  });
}

/* 把若干板块渲染成一份**自包含** HTML：图表已转成内嵌 PNG，样式已内联，
   因此后端用无头浏览器打开即可直接出图，无需再执行任何脚本。 */
async function buildExportHtml(tabs) {
  const prevTab = state.tab;
  const panels = $$('.panel');
  const prevHidden = {};
  panels.forEach(p => { prevHidden[p.id] = p.hidden; });

  // 导出必须发生在结果视图可见时 —— 否则图表容器宽度为 0，ECharts 无法初始化，
  // 导出的 PDF 会出现「有标题没图形」的空白框。这里强制保证可见，结束后还原。
  const view = $('#viewResult');
  const viewWasHidden = !!(view && view.hidden);
  if (viewWasHidden) view.hidden = false;

  state.noAnim = true;                 // 关动画：一次画到终态
  destroyCharts();
  panels.forEach(p => { p.hidden = true; });

  const frags = [];
  let missed = 0;
  for (const t of tabs) {
    const p = document.getElementById('panel-' + t);
    if (!p) continue;
    p.hidden = false;
    renderByTab(t);
    await nextFrame();
    // 图表 → PNG（内嵌 dataURL）
    p.querySelectorAll('.chart').forEach(el => {
      const inst = state.charts[el.id];
      if (!inst) { missed++; return; }
      try {
        const url = inst.getDataURL({ type: 'png', pixelRatio: 2, backgroundColor: '#ffffff' });
        el.innerHTML = `<img class="export-chart-img" src="${url}" alt="${esc(el.id)}">`;
      } catch (e) { missed++; }
    });
    frags.push(`<section class="export-section">
      <h2 class="export-h2">${esc(TAB_TITLES[t] || t)}</h2>
      ${p.innerHTML}
    </section>`);
  }

  // 还原现场
  destroyCharts();
  panels.forEach(p => { p.hidden = prevHidden[p.id]; });
  if (viewWasHidden) view.hidden = true;
  state.noAnim = false;
  state.tab = prevTab;
  $$('#resultTabs button').forEach(b => b.classList.toggle('active', b.dataset.tab === prevTab));
  renderByTab(prevTab);
  resizeAllCharts();
  if (missed) console.warn(`[export] 有 ${missed} 个图表未能截图（容器不可见？）`);

  const css = await fetch('/style.css').then(r => r.text()).catch(() => '');
  const r = state.result, c = state.current;
  const meta = [
    `${c.created_at}`,
    `${r.overview.units_analyzed} 个讨论单元`,
    r.overview.comments_total ? `${r.overview.comments_total} 条评论` : null,
    r.overview.avg_rating != null ? `平均 ${r.overview.avg_rating}★` : null,
    engineLabel(r.engine),
  ].filter(Boolean).map(esc).join('　·　');
  const toc = tabs.map(t => `<span>${esc(TAB_TITLES[t] || t)}</span>`).join('');

  return `<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>${esc(r.category)} · ${esc(r.platform)} VOC 分析报告</title>
<style>${css}</style>
<style>${EXPORT_CSS}</style>
</head><body class="export-body">
<header class="export-cover">
  <div class="export-brand">VOC 洞察平台 · VOICE OF CUSTOMER</div>
  <h1>${esc(r.category)} · ${esc(r.platform)} VOC 分析报告</h1>
  <p>${meta}</p>
  <div class="export-toc">包含板块：${toc}</div>
</header>
${frags.join('\n')}
<footer class="export-foot">由 VOC 洞察平台自动生成 · ${new Date().toLocaleString('zh-CN')}</footer>
</body></html>`;
}

function sanitizeFile(s) {
  return String(s || '').replace(/[\\/:*?"<>|\s]+/g, '_').replace(/_+/g, '_').slice(0, 80) || 'VOC分析';
}

function exportFileName(scope) {
  const r = state.result, c = state.current;
  const d = new Date();
  const p2 = (n) => String(n).padStart(2, '0');
  const ts = `${d.getFullYear()}${p2(d.getMonth() + 1)}${p2(d.getDate())}_${p2(d.getHours())}${p2(d.getMinutes())}`;
  const tail = scope === 'all' ? '六板块' : String(TAB_TITLES[state.tab] || state.tab).replace(/^[一二三四五六]、/, '');
  return sanitizeFile(`${r.category || c.category}_${r.platform || c.platform}_${tail}_${ts}`);
}

function downloadBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url; a.download = filename;
  document.body.appendChild(a);
  a.click();
  setTimeout(() => { URL.revokeObjectURL(url); a.remove(); }, 2000);
}

async function exportPdf(scope = 'all') {
  if (!state.result || !state.current) { toast('请先打开一份分析结果'); return; }
  if (state.exporting) { toast('正在导出中，请稍候'); return; }
  state.exporting = true;
  const btns = [$('#btnExportPdf'), $('#btnExportTabPdf')].filter(Boolean);
  const labels = btns.map(b => b.textContent);
  btns.forEach(b => { b.disabled = true; b.textContent = '生成中…'; });
  const scopeAll = scope === 'all';
  if (btns[scopeAll ? 0 : 1]) btns[scopeAll ? 0 : 1].textContent = '正在生成 PDF…';
  try {
    const tabs = scopeAll ? ALL_TABS : [state.tab];
    const html = await buildExportHtml(tabs);
    const fname = exportFileName(scope);
    const resp = await fetch('/api/export/pdf', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ html, name: fname }),
    });
    if (!resp.ok) {
      let m = resp.statusText;
      try { m = (await resp.json()).detail || m; } catch (e) {}
      throw new Error(m);
    }
    downloadBlob(await resp.blob(), fname + '.pdf');
    toast(scopeAll ? '六个板块已导出为 PDF' : '当前板块已导出为 PDF');
  } catch (e) {
    console.warn('[export] 服务端 PDF 失败，回退浏览器打印：', e);
    toast('本地 PDF 引擎不可用，已改用浏览器打印', true);
    try { await fallbackPrint(scope); } catch (_) {}
  } finally {
    state.exporting = false;
    btns.forEach((b, i) => { b.disabled = false; b.textContent = labels[i]; });
  }
}

/* 兜底：把同样内容的 HTML 写进新窗口并唤起打印（可另存为 PDF） */
async function fallbackPrint(scope) {
  const tabs = scope === 'all' ? ALL_TABS : [state.tab];
  const html = await buildExportHtml(tabs);
  const w = window.open('', '_blank');
  if (!w) { toast('浏览器拦截了新窗口，请允许弹窗后重试', true); return; }
  w.document.open();
  w.document.write(html);
  w.document.close();
  setTimeout(() => { try { w.focus(); w.print(); } catch (e) {} }, 700);
}

async function probePdfEngine() {
  try {
    const j = await (await api('/api/export/available')).json();
    state.pdfEngine = !!j.pdf_engine;
  } catch (e) { state.pdfEngine = false; }
  const b = $('#btnExportPdf');
  if (b && !state.pdfEngine) {
    b.title = '未检测到本地 Chrome/Edge，将改用浏览器打印（在打印对话框中选「另存为 PDF」）';
  }
}

/* ---------- 演示数据 ---------- */
async function renderDemos() {
  try {
    const { demos } = await (await api('/api/demo')).json();
    const mine = (demos || []).filter(d => d.platform === state.platform);
    if (!mine.length) {
      $('#demoCard').hidden = false;
      const p = state.platforms.find(x => x.id === state.platform) || {};
      $('#demoList').innerHTML = `<div style="font-size:12.5px;color:var(--text-3);line-height:1.8">
        该平台暂无内置示例数据。<br>
        上传你自己的 ${esc(p.label_cn || '')} 数据文件即可开始分析。${
          state.platform === 'Amazon'
            ? '<br>可用列名示例：<code style="font-family:var(--mono);font-size:11.5px">ASIN / Product name / Rating / Review text / Review date</code>'
            : ''}
      </div>`;
      return;
    }
    $('#demoCard').hidden = false;
    $('#demoList').innerHTML = mine.map(d => `
      <button class="demo-chip" data-id="${esc(d.id)}">
        <span class="dc-plat">${esc(d.platform)}</span>${esc(d.label)}
        <span style="color:#b3bac5;font-size:11px"> ${esc(d.desc)}</span>
      </button>`).join('');
    $$('#demoList .demo-chip').forEach(b => {
      b.onclick = async () => {
        b.disabled = true;
        try {
          const { id } = await (await api(`/api/demo/${encodeURIComponent(b.dataset.id)}`,
                                          { method: 'POST' })).json();
          await loadDatasets();
          watchJob(id);
        } catch (e) { toast('启动失败：' + e.message, true); b.disabled = false; }
      };
    });
  } catch (e) { $('#demoCard').hidden = true; }
}

/* ---------- 新建 ---------- */
$('#btnNew').addEventListener('click', () => {
  state.current = null; state.files = [];
  state.voiceFilter = emptyVoiceFilter();
  if (location.hash) history.replaceState(null, '', location.pathname + location.search);
  renderFileList(); updateStartState();
  clearError();
  clearWarnings();
  $('#fCategory').value = ''; $('#fBrands').value = ''; $('#fName').value = '';
  $('#runNote').textContent = '';
  showView('setup');
  renderDatasets();
  destroyCharts();
});

init().catch(e => { console.error(e); toast('初始化失败：' + e.message, true); });
