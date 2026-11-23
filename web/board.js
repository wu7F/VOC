(function () {
  'use strict';

  function fmtUnits(v) {
    if (v === null || v === undefined || v === '') return '—';
    var n = Number(v);
    if (!isFinite(n)) return '—';
    return n.toLocaleString('zh-CN');
  }

  function fmtRate(v) {
    if (v === null || v === undefined || v === '') return '—';
    var n = Number(v);
    if (!isFinite(n)) return '—';
    return n.toFixed(2) + '%';
  }

  function esc(s) {
    return String(s === null || s === undefined ? '' : s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function monthLabel(m) {
    if (!m) return '';
    var p = m.split('-');
    return p.length === 2 ? (parseInt(p[1], 10) + ' 月') : m;
  }

  function isoClock(s) {
    var m = /T(\d{2}:\d{2}:\d{2})/.exec(s || '');
    return m ? m[1] : '--:--:--';
  }

  function color(i) {
    var colors = ['#2f5d8a', '#d97706', '#2f7d5b', '#8b5cf6', '#0891b2', '#be185d'];
    return colors[(i || 0) % colors.length];
  }

  function teamShops(snapshot, view) {
    if (!snapshot || !snapshot.teams) return [];
    if (!view || view === 'all') return snapshot.teams;
    if (view === 'Wayfair') return [];
    return snapshot.teams.filter(function (t) { return t.name === view; });
  }

  function matchesSkuSearch(sku, query) {
    var q = String(query || '').trim().toLocaleLowerCase();
    if (!q) return true;
    var text = [sku && sku.display_name, sku && sku.sku, sku && sku.wf_part_number]
      .join(' ').toLocaleLowerCase();
    return text.indexOf(q) >= 0;
  }

  function findShop(snapshot, account) {
    var teams = snapshot.teams || [];
    for (var i = 0; i < teams.length; i++) {
      var shops = teams[i].shops || [];
      for (var j = 0; j < shops.length; j++) {
        if (shops[j].account === account) return shops[j];
      }
    }
    return null;
  }

  function teamNameOfShop(snapshot, account) {
    var teams = snapshot.teams || [];
    for (var i = 0; i < teams.length; i++) {
      var shops = teams[i].shops || [];
      for (var j = 0; j < shops.length; j++) {
        if (shops[j].account === account) return teams[i].name;
      }
    }
    return '';
  }

  function amazonKnown(snapshot) {
    var total = 0;
    (snapshot.teams || []).forEach(function (t) {
      (t.shops || []).forEach(function (s) {
        total += s.units_known || 0;
      });
    });
    return total;
  }

  function gapShopCount(snapshot) {
    var c = 0;
    (snapshot.teams || []).forEach(function (t) {
      (t.shops || []).forEach(function (s) {
        var months = s.months || {};
        var keys = Object.keys(months);
        for (var i = 0; i < keys.length; i++) {
          if (months[keys[i]] === null) { c += 1; break; }
        }
      });
    });
    return c;
  }

  function heatStyle(v, maxVal) {
    if (v === null || v === undefined) return '';
    if (v === 0) return ' style="background:var(--fill)"';
    var pct = maxVal > 0 ? Math.round(Math.max(0, Math.min(1, v / maxVal)) * 80) + 5 : 5;
    return ' style="background:color-mix(in srgb, var(--accent) ' + pct + '%, var(--fill))"';
  }

  function aftersaleStoreMonth(snapshot, account, month) {
    var aftersale = snapshot && snapshot.aftersale;
    if (!aftersale || !aftersale.connected) return null;
    var rows = aftersale.store_month || [];
    for (var i = 0; i < rows.length; i++) {
      if (rows[i] && (rows[i].account || rows[i].store) === account && rows[i].month === month) {
        if (Object.prototype.hasOwnProperty.call(rows[i], 'rate')) return rows[i];
        var n = Number(rows[i].numerator), d = Number(rows[i].denominator);
        var rate = isFinite(n) && isFinite(d) && d > 0 ? Math.round(n * 10000 / d) / 100 : null;
        return { rate: rate, numerator: rows[i].numerator, denominator: rows[i].denominator,
          account: rows[i].account || rows[i].store, month: rows[i].month };
      }
    }
    return null;
  }

  function renderBannerHtml(snapshot) {
    if (!snapshot) return '<span class="live"><i></i><span>暂无数据</span></span>';
    var time = isoClock(snapshot.fetched_at || snapshot.last_success_at);
    return '<span class="live"><i></i><span>数据读取于 ' + esc(time) + '</span> · 当前月 ' +
      esc(snapshot.current_month || '') +
      (snapshot.current_month_partial ? ' 截至读取时点' : '') + '</span>';
  }

  function renderStaleHtml(lastSuccess, error) {
    return '<div class="note warn">读取失败，当前显示 ' + esc(lastSuccess || '') +
      ' 的数据：' + esc(error || '') + '</div>';
  }

  function renderKpis(snapshot, view) {
    var known = amazonKnown(snapshot);
    var gap = gapShopCount(snapshot);
    var aftersale = snapshot.aftersale || {};
    var aftersaleRate = '—';
    if (aftersale.connected) {
      var teamName = view && view !== 'all' && view !== 'Wayfair' ? view : null;
      if (teamName) {
        var teamRow = null;
        (aftersale.teams || []).forEach(function (t) {
          if (t && t.name === teamName) teamRow = t;
        });
        if (teamRow && teamRow.rate !== null && teamRow.rate !== undefined) {
          aftersaleRate = fmtRate(teamRow.rate);
        }
      } else if (aftersale.total && aftersale.total.rate !== null && aftersale.total.rate !== undefined) {
        aftersaleRate = fmtRate(aftersale.total.rate);
      }
    }
    var aftersaleMessage = (snapshot.aftersale && snapshot.aftersale.message) ||
      '售后分子和原因尚未接入多维表格,售后率显示“—”,不按 0% 计算';
    return '<div class="kpis">' +
      '<div class="kpi"><small>Amazon 已知销量合计</small><strong>' + fmtUnits(known) +
      '</strong><span>仅已归属 Team 的已知销量</span></div>' +
      '<div class="kpi"><small>Amazon 售后率</small><strong>' + esc(aftersaleRate) + '</strong><span>' +
      esc(aftersaleMessage) + '</span></div>' +
      '<div class="kpi"><small>有缺口的店铺数</small><strong>' + gap +
      '</strong><span>存在缺失月份或不完整格</span></div></div>';
  }

  function renderNavHtml(state) {
    state = state || {};
    var sections = [
      ['总览', [['all', '全部']]],
      ['AMAZON', [['Team 1', 'Team 1'], ['Team 2', 'Team 2'], ['Team 3', 'Team 3']]],
      ['WAYFAIR', [['Wayfair', '未接入']]]
    ];
    return sections.map(function (sec) {
      var h = '<small>' + esc(sec[0]) + '</small>';
      sec[1].forEach(function (it) {
        var on = state.view === it[0] && !state.shop;
        h += '<button data-bind="nav" data-view="' + esc(it[0]) + '" class="' +
          (on ? 'on' : '') + '">' + esc(it[1]) + '</button>';
      });
      return h;
    }).join('');
  }

  function renderMatrixHtml(snapshot, state) {
    state = state || {};
    if (!snapshot || snapshot.ok === false) return '<div class="warnbox">没有可用数据</div>';
    if (state.view === 'Wayfair') {
      var wayfairMessage = (snapshot.wayfair && snapshot.wayfair.message) || 'Wayfair 销量尚未接入';
      return '<div class="warnbox">Wayfair 销量尚未接入</div><div class="card"><p>' +
        esc(wayfairMessage) + '</p></div>';
    }
    var months = snapshot.months || [];
    if (!months.length) return '<div class="warnbox">暂无月份数据</div>';
    var teams = teamShops(snapshot, state.view);
    if (!teams.length) return '<div class="warnbox">该视图暂无店铺数据</div>';

    var maxRate = 0;
    if (snapshot.aftersale && snapshot.aftersale.connected) {
      (snapshot.aftersale.store_month || []).forEach(function (r) {
        if (r && r.rate !== null && r.rate !== undefined && isFinite(Number(r.rate))) {
          maxRate = Math.max(maxRate, Number(r.rate));
        }
      });
    }

    // 汇总统计
    var totalKnown = 0;
    var totalUnpaired = 0;
    var hasUnknownUnpaired = false;
    teams.forEach(function (t) {
      (t.shops || []).forEach(function (s) { totalKnown += s.units_known || 0; });
      var up = t.unpaired_units;
      if (up === null || up === undefined) {
        hasUnknownUnpaired = true;
      } else {
        totalUnpaired += up || 0;
      }
    });
    var totalUnpairedDisplay = hasUnknownUnpaired ? null : totalUnpaired;

    var h = renderKpis(snapshot, state.view);
    h += '<div class="matrix-summary"><strong>' + (state.view === 'all' ? '全部' : esc(state.view)) +
      '</strong> · 产品销量分母：' + fmtUnits(totalKnown) +
      ' · 未配对数量：' + fmtUnits(totalUnpairedDisplay) +
      (snapshot.unpaired ? '（全局未配对 ' + fmtUnits(snapshot.unpaired.total_units) + '）' : '') +
      '</div>';

    h += '<table class="hm"><thead><tr><th></th>';
    h += months.map(function (m) { return '<th>' + monthLabel(m) + '</th>'; }).join('');
    h += '<th>累计销量（已知）</th></tr></thead><tbody>';
    teams.forEach(function (team) {
      var teamKnown = 0;
      (team.shops || []).forEach(function (s) { teamKnown += s.units_known || 0; });
      h += '<tr class="g"><td colspan="' + (months.length + 2) + '">Amazon · ' +
        esc(team.name) + ' · 分母 ' + fmtUnits(teamKnown) +
        ' · 未配对 ' + fmtUnits(team.unpaired_units) + '</td></tr>';
      (team.shops || []).forEach(function (shop) {
        var missing = 0;
        months.forEach(function (m) {
          var v = shop.months ? shop.months[m] : null;
          if (v === null) missing += 1;
        });
        h += '<tr><td class="n" title="' + esc(shop.account) + '">' +
          esc(shop.display_name || shop.account) + '</td>';
        months.forEach(function (m) {
          var v = shop.months ? shop.months[m] : null;
          if (v === null || v === undefined) {
            h += '<td class="na" title="销量缺失或范围待确认">—</td>';
          } else {
            var rateRow = aftersaleStoreMonth(snapshot, shop.account, m);
            var rate = rateRow ? rateRow.rate : null;
            var rateKnown = rate !== null && rate !== undefined && isFinite(Number(rate));
            h += '<td' + (rateKnown ? heatStyle(Number(rate), maxRate) : ' class="rate-na"') +
              '><button data-bind="matrix-cell" data-shop="' + esc(shop.account) +
              '" data-month="' + esc(m) + '" aria-label="' +
              esc(shop.account + ' ' + m + ' 售后率 ' + fmtRate(rate) + '，销量 ' + fmtUnits(v)) +
              '" title="售后率 ' + fmtRate(rate) + '；销量 ' + fmtUnits(v) + '">' +
              fmtRate(rate) + '</button></td>';
          }
        });
        h += '<td style="font-weight:600">' + fmtUnits(shop.units_known) +
          (missing > 0 ? ' <span class="warn">缺 ' + missing + ' 个月</span>' : '') +
          '</td></tr>';
      });
    });
    h += '</tbody></table><div class="leg"><span>低售后率</span>' +
      '<b style="background:var(--fill)"></b>' +
      '<b style="background:color-mix(in srgb, var(--accent) 45%, var(--fill))"></b>' +
      '<b style="background:color-mix(in srgb, var(--accent) 85%, var(--fill))"></b>' +
      '<span>高售后率 · “—”表示售后率无有效快照或销量缺失 · 点击格子看销量明细</span></div>';
    return h;
  }

  function renderAftersaleHtml(snapshot, state) {
    var aftersale = snapshot && snapshot.aftersale;
    if (!aftersale || !aftersale.connected) {
      return '<div class="card"><h3>售后率（确认快照）</h3>' +
        '<div class="warnbox">' +
        esc((aftersale && aftersale.message) ||
          '售后分子和原因尚未接入多维表格,售后率显示“—”,不按 0% 计算') +
        '</div></div>';
    }
    var h = '<div class="card"><h3>售后率（只读实时聚合）</h3>' +
      '<p>' + esc(aftersale.message || '') +
      (aftersale.source_as_of ? ' 数据截至 ' + esc(aftersale.source_as_of) + '。' : '') +
      ' 分母来自当前同一批只读销量快照，过滤未配对SKU和T01。</p>';

    // Team 汇总
    h += '<table class="sk"><thead><tr><th>Team</th><th>售后分子</th><th>销量分母</th><th>售后率</th></tr></thead><tbody>';
    (aftersale.teams || []).forEach(function (t) {
      h += '<tr><td>' + esc(t.name) + '</td><td>' + fmtUnits(t.numerator) +
        '</td><td>' + fmtUnits(t.denominator) + '</td><td>' + fmtRate(t.rate) + '</td></tr>';
    });
    if (aftersale.total) {
      h += '<tr style="font-weight:600"><td>合计</td><td>' + fmtUnits(aftersale.total.numerator) +
        '</td><td>' + fmtUnits(aftersale.total.denominator) + '</td><td>' +
        fmtRate(aftersale.total.rate) + '</td></tr>';
    }
    h += '</tbody></table>';

    // 店铺汇总
    h += '<h3>店铺售后率</h3><table class="sk"><thead><tr><th>店铺</th><th>Team</th><th>售后分子</th><th>销量分母</th><th>售后率</th></tr></thead><tbody>';
    (aftersale.stores || []).forEach(function (s) {
      h += '<tr><td>' + esc(s.account) + '</td><td>' + esc(s.team || '') +
        '</td><td>' + fmtUnits(s.numerator) + '</td><td>' + fmtUnits(s.denominator) +
        '</td><td>' + fmtRate(s.rate) + '</td></tr>';
    });
    h += '</tbody></table>';

    // 店铺月度
    h += '<h3>店铺月度售后率</h3><table class="sk"><thead><tr><th>店铺</th><th>月份</th><th>售后分子</th><th>销量分母</th><th>售后率</th></tr></thead><tbody>';
    (aftersale.store_month || []).forEach(function (r) {
      h += '<tr><td>' + esc(r.account) + '</td><td>' + esc(r.month) +
        '</td><td>' + fmtUnits(r.numerator) + '</td><td>' + fmtUnits(r.denominator) +
        '</td><td>' + fmtRate(r.rate) + '</td></tr>';
    });
    h += '</tbody></table>';

    // 选中 SKU 月度售后率
    if (state && state.shop) {
      var shop = findShop(snapshot, state.shop);
      var skus = (shop && shop.skus) || [];
      var selectedSkuId = null;
      if (typeof state.selectedSku === 'string') {
        selectedSkuId = state.selectedSku;
      } else if (skus.length) {
        selectedSkuId = skus[0].sku;
      }
      if (selectedSkuId) {
        var rows = (aftersale.sku_store_month || []).filter(function (r) {
          return r.account === state.shop && r.sku === selectedSkuId;
        });
        var selectedSkuRecord = skus.filter(function (sku) { return sku && sku.sku === selectedSkuId; })[0];
        var selectedSkuLabel = selectedSkuRecord && selectedSkuRecord.display_name
          ? selectedSkuRecord.display_name + '（' + selectedSkuId + '）'
          : selectedSkuId;
        h += '<h3>选中 SKU 月度售后率：' + esc(selectedSkuLabel) + '</h3>';
        if (!rows.length) {
          h += '<div class="warnbox">该 SKU 在确认快照中无售后数据（未知，不按 0% 计算）。</div>';
        } else {
          h += '<table class="sk"><thead><tr><th>月份</th><th>售后分子</th><th>销量分母</th><th>售后率</th></tr></thead><tbody>';
          rows.forEach(function (r) {
            h += '<tr><td>' + esc(r.month) + '</td><td>' + fmtUnits(r.numerator) +
              '</td><td>' + fmtUnits(r.denominator) + '</td><td>' + fmtRate(r.rate) + '</td></tr>';
          });
          h += '</tbody></table>';
        }
      }
    }

    // 售后原因占位
    h += '<h3>售后原因占比</h3><div class="warnbox">已发现售后表“原文”字段，但当前只读访问拿不到记录字段值；暂不生成占比，避免把缺失数据当作没有原因。</div>';
    h += '</div>';
    return h;
  }

  function renderLineChartHtml(series, months, opts) {
    opts = opts || {};
    var W = 620, H = 250, L = 46, R = 14, T = 16, B = 30;
    var pw = W - L - R, ph = H - T - B;
    var mark = typeof opts.mark === 'number' ? opts.mark : -1;
    var allVals = [];
    series.forEach(function (s) {
      (s.values || []).forEach(function (v) {
        if (v !== null && v !== undefined) allVals.push(v);
      });
    });
    var maxY = Math.max(1, allVals.length ? Math.max.apply(null, allVals) : 1);
    maxY = Math.ceil(maxY * 1.15) || 1;
    function x(i) {
      return months.length > 1 ? L + pw * i / (months.length - 1) : L + pw / 2;
    }
    function y(v) {
      return T + ph * (1 - (v === null || v === undefined ? 0 : v) / maxY);
    }
    var h = '<svg viewBox="0 0 ' + W + ' ' + H + '" role="img" aria-label="' +
      esc(opts.label || 'chart') + '">';
    for (var g = 0; g <= 4; g++) {
      var gv = maxY * g / 4;
      h += '<line x1="' + L + '" x2="' + (W - R) + '" y1="' + y(gv) + '" y2="' +
        y(gv) + '" stroke="var(--line)"/>';
      h += '<text x="' + (L - 6) + '" y="' + (y(gv) + 4) +
        '" text-anchor="end" font-size="11" fill="var(--muted)">' +
        opts.fmt(gv) + '</text>';
    }
    months.forEach(function (m, i) {
      h += '<text x="' + x(i) + '" y="' + (H - 8) +
        '" text-anchor="middle" font-size="11" fill="' +
        (i === mark ? 'var(--ink)' : 'var(--muted)') + '" font-weight="' +
        (i === mark ? 700 : 400) + '">' + monthLabel(m) + '</text>';
    });
    if (mark >= 0) {
      h += '<line x1="' + x(mark) + '" x2="' + x(mark) + '" y1="' + T + '" y2="' +
        (T + ph) + '" stroke="var(--muted)" stroke-dasharray="4 4"/>';
    }
    series.forEach(function (s) {
      var d = '', pen = false;
      (s.values || []).forEach(function (v, i) {
        if (v === null || v === undefined) { pen = false; return; }
        d += (pen ? ' L' : ' M') + x(i).toFixed(1) + ' ' + y(v).toFixed(1);
        pen = true;
      });
      if (d) {
        h += '<path d="' + d + '" fill="none" stroke="' + esc(s.color) +
          '" stroke-width="' + (s.dim ? 1.5 : 2.5) + '" opacity="' +
          (s.dim ? 0.35 : 1) + '" stroke-linejoin="round"/>';
      }
      (s.values || []).forEach(function (v, i) {
        if (v === null || v === undefined) return;
        h += '<circle cx="' + x(i) + '" cy="' + y(v) + '" r="' +
          (s.dim ? 2.5 : 4) + '" fill="' + esc(s.color) + '" opacity="' +
          (s.dim ? 0.5 : 1) + '" data-series="' + esc(s.name) + '" data-i="' + i +
          '" data-month="' + esc(months[i]) + '" data-value="' + v + '"/>';
      });
    });
    h += '</svg><div class="tip"></div>';
    return h;
  }

  function getSkuIdentity(shop, skuIndexOrId) {
    if (!shop || !shop.skus || shop.skus.length === 0) return null;
    if (typeof skuIndexOrId === 'number') {
      if (skuIndexOrId >= 0 && skuIndexOrId < shop.skus.length) {
        return shop.skus[skuIndexOrId].sku;
      }
      return null;
    }
    // string identity
    var found = shop.skus.some(function (s) { return s.sku === skuIndexOrId; });
    return found ? skuIndexOrId : null;
  }

  function getSkuIndexByIdentity(shop, skuId) {
    if (!shop || !shop.skus || !skuId) return -1;
    for (var i = 0; i < shop.skus.length; i++) {
      if (shop.skus[i].sku === skuId) return i;
    }
    return -1;
  }

  function renderDetailHtml(snapshot, state) {
    state = state || {};
    var months = snapshot.months || [];
    if (!months.length) return '<div class="warnbox">暂无月份数据</div>';
    var shop = findShop(snapshot, state.shop);
    if (!shop) return '<div class="warnbox">未找到店铺</div>';
    var month = state.month || snapshot.current_month || months[months.length - 1];
    if (months.indexOf(month) < 0) month = months[0];
    var skus = shop.skus || [];
    if (!skus.length) return '<div class="warnbox">该店铺暂无 SKU 数据</div>';

    // selectedSku 始终是稳定的 SKU 编码身份。
    var selectedSkuId;
    if (typeof state.selectedSku === 'number') {
      selectedSkuId = getSkuIdentity(shop, state.selectedSku) || (skus.length ? skus[0].sku : null);
    } else {
      selectedSkuId = getSkuIdentity(shop, state.selectedSku) || (skus.length ? skus[0].sku : null);
    }
    var selectedSkuIndex = getSkuIndexByIdentity(shop, selectedSkuId);
    if (selectedSkuIndex < 0) selectedSkuIndex = 0;
    selectedSkuId = skus[selectedSkuIndex].sku;



    var monthVal = shop.months ? shop.months[month] : null;
    var sku = skus[selectedSkuIndex];
    var teamName = teamNameOfShop(snapshot, shop.account);
    var skuDisplayName = sku ? (sku.display_name || '') : '';
    var skuWfPart = sku ? (sku.wf_part_number || '') : '';

    var h = '<div class="crumb"><button data-bind="action" data-action="back">← 返回矩阵</button>' +
      '<span>/</span><span>Amazon · ' + esc(teamName || '') + '</span><span>/</span><b>' +
      esc(shop.account) + '</b><span>/</span><b>' + esc(month) + '</b></div>';
    h += '<div class="chips">' + months.map(function (m) {
      return '<button data-bind="month-chip" data-month="' + esc(m) + '" class="' +
        (m === month ? 'on' : '') + '">' + esc(m) + '</button>';
    }).join('') + '</div>';
    h += '<div class="kpis"><div class="kpi"><small>' + esc(shop.account) + ' · ' +
      esc(month) + ' 销量</small><strong>' + fmtUnits(monthVal) +
      '</strong><span>' + (monthVal === null ? '该月销量缺失，范围待确认' : '已知销量（SKU 求和）') +
      '</span></div><div class="kpi"><small>已选 SKU中文名称</small><strong class="selected-sku-name">' +
      esc(sku ? (sku.display_name || '配对中文名称缺失') : '—') + '</strong><span class="selected-sku-code">' +
      esc(sku ? sku.sku : '') + ' · 点击左侧表格切换产品</span></div>' +
      '<div class="kpi"><small>SKU 数</small><strong>' + skus.length +
      '</strong><span>本店铺</span></div></div>';

    // 模糊检索当前店铺的 SKU 中文名、编码及 WF Part Number。
    var skuSearch = String(state.skuSearch || '');
    var searchNorm = skuSearch.trim().toLocaleLowerCase();
    var visibleSkuIndices = [];
    skus.forEach(function (s, i) {
      if (matchesSkuSearch(s, searchNorm)) visibleSkuIndices.push(i);
    });

    // SKU 明细表。点击行选择 SKU；模糊搜索只筛选本店表格行。
    h += '<div class="cols"><div class="card"><h3>' + esc(month) +
      ' SKU 明细</h3><p>点击产品行查看该 SKU 的售后历史与月度销量；搜索支持中文名称、SKU编码和 WF Part Number。售后列缺失显示 —。</p>' +
      '<div class="flt sku-table-controls"><label for="sku-search">搜索 SKU</label>' +
      '<input id="sku-search" type="search" data-bind="sku-search" value="' + esc(skuSearch) +
      '" placeholder="模糊搜索中文名称、SKU编码或 WF Part Number" autocomplete="off" aria-label="模糊搜索本店SKU">' +
      '<span class="sku-search-count">' + visibleSkuIndices.length + ' / ' + skus.length + ' 个SKU</span></div>' +
      '<div class="sku-table-scroll"><table class="sk"><thead><tr><th>SKU中文名称</th><th>SKU编码</th><th>WF Part Number</th><th>销量</th><th>售后</th><th>售后率</th>' +
      '</tr></thead><tbody>';
    var aftersaleConnected = !!(snapshot.aftersale && snapshot.aftersale.connected);
    var skuStoreMonth = (snapshot.aftersale && snapshot.aftersale.sku_store_month) || [];
    h += visibleSkuIndices.map(function (i) { var s = skus[i];
      var v = s.sold ? s.sold[month] : null;
      var isSelected = i === selectedSkuIndex;
      var asRow = null;
      if (aftersaleConnected) {
        for (var k = 0; k < skuStoreMonth.length; k++) {
          var r = skuStoreMonth[k];
          if (r && r.account === shop.account && r.month === month && r.sku === s.sku) {
            asRow = r;
            break;
          }
        }
      }
      var asNumCell = asRow && asRow.numerator !== null && asRow.numerator !== undefined
        ? fmtUnits(asRow.numerator) : '—';
      var asRateCell = asRow && asRow.rate !== null && asRow.rate !== undefined
        ? fmtRate(asRow.rate) : '—';
      return '<tr data-bind="sku-row" data-i="' + i + '" class="' +
        (isSelected ? 'on' : '') + '"><td><span class="dot" style="background:' +
        color(i) + '"></span>' + esc(s.display_name || '（未配对名称）') + '</td><td class="sku-code">' +
        esc(s.sku) + '</td><td>' + esc(s.wf_part_number || '') +
        '</td><td>' + (v === null || v === undefined ? '—' : fmtUnits(v)) +
        '</td><td>' + asNumCell + '</td><td>' + asRateCell + '</td></tr>';
    }).join('') + (visibleSkuIndices.length ? '' : '<tr class="sku-search-empty-row"><td colspan="6" class="nac">没有匹配的 SKU</td></tr>') +
      '</tbody></table></div></div>';

    // 右侧卡片区域
    h += '<div>';

    // ① 月度售后率：显示该店该 SKU 的完整确认历史，当前所选月份若无记录仍保持未知。
    h += '<div class="card"><h3>① ' + esc(skuDisplayName || '') + ' (' + esc(sku.sku) + ') 月度售后率</h3>';
    if (aftersaleConnected) {
      var skuHistoryRows = skuStoreMonth.filter(function (r) {
        return r && r.account === shop.account && r.sku === sku.sku;
      }).slice().sort(function (a, b) { return String(a.month).localeCompare(String(b.month)); });
      var selectedAsRow = null;
      skuHistoryRows.forEach(function (r) { if (r.month === month) selectedAsRow = r; });
      if (selectedAsRow) {
        h += '<p>' + esc(month) + '：售后率 <b>' + fmtRate(selectedAsRow.rate) + '</b>' +
          '（售后 ' + fmtUnits(selectedAsRow.numerator) + ' / 销量 ' +
          fmtUnits(selectedAsRow.denominator) + '）</p>';
      } else {
        h += '<p class="nac">所选月份 ' + esc(month) + ' 无匹配记录；以下列出本店该 SKU 的已确认历史数据，所选月份仍为未知。</p>';
      }
      if (skuHistoryRows.length) {
        h += '<div class="sku-table-scroll"><table class="sk"><thead><tr><th>月份</th><th>售后</th><th>销量</th><th>售后率</th></tr></thead><tbody>';
        skuHistoryRows.forEach(function (r) {
          h += '<tr' + (r.month === month ? ' class="on"' : '') + '><td>' + esc(r.month) +
            '</td><td>' + fmtUnits(r.numerator) + '</td><td>' + fmtUnits(r.denominator) +
            '</td><td>' + fmtRate(r.rate) + '</td></tr>';
        });
        h += '</tbody></table></div>';
      } else {
        h += '<div class="warnbox">该店铺该 SKU 暂无可显示的确认历史售后率。</div>';
      }
      h += '<p>' + esc((snapshot.aftersale && snapshot.aftersale.message) || '') +
        (snapshot.aftersale && snapshot.aftersale.source_as_of
          ? ' 数据截至 ' + esc(snapshot.aftersale.source_as_of) + '。' : '') + '</p>';
    } else {
      h += '<p>售后分子和原因尚未接入多维表格，售后率显示“—”，不按 0% 计算。</p>' +
        '<div class="warnbox">' +
        esc((snapshot.aftersale && snapshot.aftersale.message) ||
          '售后分子和原因尚未接入多维表格,售后率显示“—”,不按 0% 计算') +
        '</div>';
    }
    h += '</div>';

    // ② 售后原因占比
    h += '<div class="card"><h3>② ' + esc(skuDisplayName || '') + ' (' + esc(sku.sku) + ') 售后原因占比</h3>';
    var reasons = snapshot.aftersale && snapshot.aftersale.reasons;
    var reasonItems = reasons && Array.isArray(reasons.items) ? reasons.items.filter(function (r) {
      return r && r.account === shop.account && r.sku === sku.sku && r.month === month;
    }) : [];
    if (aftersaleConnected && reasons && reasons.connected && reasonItems.length) {
      h += '<p>' + esc(shop.account) + ' · ' + esc(month) + ' · 当前SKU 的售后原因占比；仅显示服务端汇总类别，不显示原文。</p>';
      h += '<table class="sk"><thead><tr><th>归因类别</th><th>数量</th><th>占比</th></tr></thead><tbody>';
      reasonItems.forEach(function (r) {
        h += '<tr><td>' + esc(r.category) + '</td><td>' + fmtUnits(r.count) + '</td><td>' + fmtRate(r.share) + '</td></tr>';
      });
      h += '</tbody></table>';
    } else if (aftersaleConnected && reasons && reasons.connected) {
      h += '<p class="nac">该店铺、SKU、月份没有可归因的售后记录；这不代表缺失记录被按 0 处理。</p>';
    } else {
      h += '<div class="warnbox">' + esc((reasons && reasons.message) || (snapshot.aftersale && snapshot.aftersale.message) ||
        '售后原因数据暂不可用；未生成占比。') + '</div>';
    }
    h += '</div>';

    // ③ 真实销量折线
    h += '<div class="card"><h3>③ ' + esc(skuDisplayName || '') + ' (' + esc(sku.sku) + ') 月度销量</h3>' +
      '<p>显示当前选中 SKU 的月度销量。虚线为当前所选月份，缺销量的月份断线，不补 0。</p>';

    var series = [];
    var selectedSeries = skus[selectedSkuIndex];
    if (selectedSeries) {
      series.push({ name: selectedSeries.display_name ? selectedSeries.display_name + '（' + selectedSeries.sku + '）' : selectedSeries.sku,
        color: color(selectedSkuIndex), dim: false,
        values: months.map(function (m) { return selectedSeries.sold ? selectedSeries.sold[m] : null; }) });
    }
    if (!series.length) {
      series.push({ name: '—', color: color(0), dim: false, values: months.map(function () { return null; }) });
    }
    var chartTitle = (skuDisplayName ? skuDisplayName + ' / ' : '') + sku.sku + (skuWfPart ? ' / ' + skuWfPart : '') + ' 月度销量折线图';
    h += '<div class="chart" data-chart="sku">' +
      renderLineChartHtml(series, months, {
        mark: months.indexOf(month),
        fmt: fmtUnits,
        label: chartTitle
      }) + '</div></div>';

    h += '</div></div>'; // 结束 cols 和右侧 div
    // 售后率（确认快照）
    h += renderAftersaleHtml(snapshot, state);
    return h;
  }

  function renderDiagnosticsHtml(snapshot) {
    if (!snapshot) return '';
    var lines = [];
    var unmapped = snapshot.unmapped || [];
    if (unmapped.length) {
      lines.push('<li class="warn">未归属账号：' + unmapped.map(function (u) {
        return esc(u.account) + '（行 ' + u.rows + '，已知销量 ' + fmtUnits(u.units) + '）';
      }).join('；') + '</li>');
    }
    (snapshot.diagnostics || []).forEach(function (d) {
      var cls = d.level === 'error' ? 'bad' : (d.level === 'warning' ? 'warn' : 'nac');
      lines.push('<li class="' + cls + '">[' + esc(d.level) + '] ' + esc(d.message) + '</li>');
    });
    // 未配对 SKU 诊断
    var unpairedSkus = [];
    (snapshot.teams || []).forEach(function (t) {
      if (t.unpaired_skus && t.unpaired_skus.length) unpairedSkus = unpairedSkus.concat(t.unpaired_skus);
    });
    if (unpairedSkus.length) {
      lines.push('<li class="warn">未配对 SKU：' + unpairedSkus.map(function (sku) {
        return esc(sku.sku) + '（' + fmtUnits(sku.units) + ' 件）';
      }).join('；') + '</li>');
    }
    if (!lines.length) return '';
    var count = (snapshot.diagnostics || []).length + unmapped.length + unpairedSkus.length;
    return '<details class="diag"><summary>数据诊断（' + count + '）</summary><ul>' +
      lines.join('') + '</ul></details>';
  }

  function initBoard() {
    var state = {
      view: 'all',
      shop: null,
      month: null,
      selectedSku: null,      // 字符串 SKU ID
      skuSearch: '',          // 本店 SKU 名称/编码模糊搜索
      snapshot: null,
      error: null,
      fetchSeq: 0
    };
    var navEl = document.getElementById('nav');
    var mainEl = document.getElementById('main');
    var bannerEl = document.getElementById('banner');
    var staleEl = document.getElementById('stale-banner');

    function attachChartHover(root) {
      root.querySelectorAll('[data-chart="sku"] circle').forEach(function (c) {
        c.addEventListener('mouseenter', function () {
          var chart = c.closest('[data-chart="sku"]');
          if (!chart) return;
          var tip = chart.querySelector('.tip');
          if (!tip) return;
          tip.style.display = 'block';
          tip.innerHTML = '<b>' + esc(c.getAttribute('data-series')) + '</b><br>' +
            esc(c.getAttribute('data-month')) + ' · ' + fmtUnits(c.getAttribute('data-value'));
        });
        c.addEventListener('mousemove', function (e) {
          var chart = c.closest('[data-chart="sku"]');
          var tip = chart && chart.querySelector('.tip');
          if (!tip) return;
          var r = chart.getBoundingClientRect();
          tip.style.left = (e.clientX - r.left + 12) + 'px';
          tip.style.top = (e.clientY - r.top - 40) + 'px';
        });
        c.addEventListener('mouseleave', function () {
          var chart = c.closest('[data-chart="sku"]');
          var tip = chart && chart.querySelector('.tip');
          if (tip) tip.style.display = 'none';
        });
      });
    }

    function render() {
      navEl.innerHTML = renderNavHtml(state);
      if (!state.snapshot) {
        bannerEl.innerHTML = '<span class="live"><i></i><span>暂无数据</span></span>';
        staleEl.innerHTML = state.error ?
          renderStaleHtml(null, (state.error && state.error.message) || String(state.error)) : '';
        if (state.error) {
          mainEl.innerHTML = '<div class="warnbox">加载失败：' +
            esc((state.error && state.error.message) || String(state.error)) +
            '</div><div class="warnbox"><button data-bind="action" data-action="retry">重试</button></div>';
        } else {
          mainEl.innerHTML = '<div class="warnbox">正在加载多维表格…</div>';
        }
        return;
      }
      bannerEl.innerHTML = renderBannerHtml(state.snapshot);
      if (state.error) {
        staleEl.innerHTML = renderStaleHtml(
          (state.error.last_success_at || state.snapshot.last_success_at || state.snapshot.fetched_at),
          (state.error.message || String(state.error))
        );
      } else {
        staleEl.innerHTML = '';
      }
      var content = state.shop ?
        renderDetailHtml(state.snapshot, state) :
        renderMatrixHtml(state.snapshot, state);
      mainEl.innerHTML = content + renderDiagnosticsHtml(state.snapshot);
      attachChartHover(mainEl);
    }

    function validateSnapshotShape(snap) {
      if (!snap || typeof snap !== 'object') {
        throw new Error('看板数据不是对象');
      }
      if (snap.ok !== true) {
        throw new Error(snap.error || '看板数据状态异常');
      }
      if (!Array.isArray(snap.teams)) {
        throw new Error('看板 teams 必须是数组');
      }
      if (!Array.isArray(snap.months)) {
        throw new Error('看板 months 必须是数组');
      }
      snap.teams.forEach(function (team) {
        if (!team || typeof team !== 'object') {
          throw new Error('看板团队数据无效');
        }
        if (!Array.isArray(team.shops)) {
          throw new Error('看板店铺数据必须是数组');
        }
        team.shops.forEach(function (shop) {
          if (!shop || typeof shop !== 'object') {
            throw new Error('看板店铺数据无效');
          }
          if (!Array.isArray(shop.skus)) {
            throw new Error('看板 SKU 数据必须是数组');
          }
        });
      });
      return true;
    }

    function reconcileSelectedSku(newSnapshot, currentState) {
      if (!currentState.shop) return;
      var shop = findShop(newSnapshot, currentState.shop);
      var validIds = [];
      if (shop && Array.isArray(shop.skus)) {
        shop.skus.forEach(function (sku) {
          if (sku && typeof sku === 'object' && typeof sku.sku === 'string') validIds.push(sku.sku);
        });
      }
      var selected = getSkuIdentity(shop, currentState.selectedSku);
      if (!selected || validIds.indexOf(selected) < 0) selected = validIds[0] || null;
      currentState.selectedSku = selected;
    }

    function fetchBoard(refresh) {
      var seq = ++state.fetchSeq;
      fetch(refresh ? '/api/bitable/board?refresh=1' : '/api/bitable/board')
        .then(function (r) {
          if (!r.ok) {
            return r.json().then(function (data) {
              var msg = data && (data.error || data.detail || data.message);
              throw new Error(msg ? 'HTTP ' + r.status + ': ' + msg : 'HTTP ' + r.status);
            }, function () {
              throw new Error('HTTP ' + r.status);
            });
          }
          return r.json().catch(function () {
            throw new Error('看板响应不是 JSON');
          });
        })
        .then(function (snap) {
          // 只有最新请求的结果才被处理
          if (seq !== state.fetchSeq) return;
          // 在写入 state.snapshot 前完成基本结构验证，避免把坏数据放入渲染路径
          validateSnapshotShape(snap);
          state.snapshot = snap;

          // 成功刷新后，按当前店铺的 SKU 身份清理选中状态，防止幽灵 ID
          reconcileSelectedSku(snap, state);

          // 如果当前没有打开店铺，且没有选中 SKU，用第一个店铺的第一个 SKU 作为默认
          if (!state.shop && !state.selectedSku && snap.teams.length) {
            var firstShop = snap.teams[0].shops && snap.teams[0].shops[0];
            if (firstShop && firstShop.skus && firstShop.skus.length) {
              state.selectedSku = firstShop.skus[0].sku;
            }
          }

          // 如果后端标记了 stale 或返回 error，必须保留错误提示，不能当作完全成功
          if (snap.stale || snap.error) {
            state.error = {
              message: snap.error || '数据已过期，显示上次成功数据',
              last_success_at: snap.last_success_at || snap.fetched_at || null
            };
          } else {
            state.error = null;
          }
          render();
        })
        .catch(function (err) {
          if (seq !== state.fetchSeq) return;
          var errorObj = { message: (err && err.message) ? err.message : String(err) };
          if (state.snapshot) {
            errorObj.last_success_at = state.snapshot.last_success_at || state.snapshot.fetched_at;
          }
          state.error = errorObj;
          // 保留旧快照，不清空
          render();
        });
    }

    document.addEventListener('input', function (e) {
      var target = e.target;
      if (!target || !target.getAttribute || target.getAttribute('data-bind') !== 'sku-search') return;
      state.skuSearch = target.value || '';
      var shop = state.snapshot ? findShop(state.snapshot, state.shop) : null;
      var visible = [];
      var q = state.skuSearch.trim().toLocaleLowerCase();
      if (shop && Array.isArray(shop.skus)) {
        shop.skus.forEach(function (s, i) {
          if (matchesSkuSearch(s, q)) visible.push(i);
        });
      }
      var rows = mainEl.querySelectorAll('[data-bind="sku-row"]');
      rows.forEach(function (row) {
        var index = Number(row.getAttribute('data-i'));
        row.hidden = visible.indexOf(index) < 0;
      });
      var count = mainEl.querySelector('.sku-search-count');
      if (count && shop) count.textContent = visible.length + ' / ' + shop.skus.length + ' 个SKU';
      var emptyRow = mainEl.querySelector('.sku-search-empty-row');
      if (emptyRow) emptyRow.hidden = visible.length > 0;
      if (!visible.length && !emptyRow) {
        var tbody = mainEl.querySelector('.sku-table-scroll tbody');
        if (tbody) {
          emptyRow = document.createElement('tr');
          emptyRow.className = 'sku-search-empty-row';
          emptyRow.innerHTML = '<td colspan="6" class="nac">没有匹配的 SKU</td>';
          tbody.appendChild(emptyRow);
        }
      } else if (visible.length && emptyRow && emptyRow.parentNode) {
        emptyRow.parentNode.removeChild(emptyRow);
      }
    });

    document.addEventListener('click', function (e) {
      var el = e.target.closest ? e.target.closest('[data-bind]') : null;
      if (!el) return;
      var bind = el.getAttribute('data-bind');

      if (bind === 'nav') {
        state.view = el.getAttribute('data-view') || 'all';
        state.shop = null;
        state.month = null;
        render();
        return;
      }
      if (bind === 'matrix-cell') {
        state.shop = el.getAttribute('data-shop');
        state.month = el.getAttribute('data-month');
        state.skuSearch = '';
        // 设置默认选中第一个 SKU
        var shop = state.snapshot ? findShop(state.snapshot, state.shop) : null;
        if (shop && shop.skus && shop.skus.length) {
          state.selectedSku = shop.skus[0].sku;
        } else {
          state.selectedSku = null;
        }
        render();
        return;
      }
      if (bind === 'month-chip') {
        state.month = el.getAttribute('data-month');
        render();
        return;
      }
      if (bind === 'sku-row') {
        var idx = Number(el.getAttribute('data-i'));
        var shop2 = state.snapshot ? findShop(state.snapshot, state.shop) : null;
        if (shop2 && shop2.skus && idx >= 0 && idx < shop2.skus.length) {
          state.selectedSku = shop2.skus[idx].sku;
        }
        render();
        return;
      }
      if (bind === 'action') {
        var action = el.getAttribute('data-action');
        if (action === 'back') {
          state.shop = null;
          state.month = null;
          state.skuSearch = '';
          render();
        } else if (action === 'refresh' || action === 'retry') {
          fetchBoard(true);
        }
        return;
      }
    });

    fetchBoard(false);
    setInterval(function () {
      if (!document.hidden) fetchBoard(false);
    }, 30000);
  }

  if (typeof document !== 'undefined') {
    initBoard();
  }

  if (typeof module !== 'undefined') {
    module.exports = {
      fmtUnits: fmtUnits,
      esc: esc,
      matchesSkuSearch: matchesSkuSearch,
      monthLabel: monthLabel,
      renderBannerHtml: renderBannerHtml,
      renderStaleHtml: renderStaleHtml,
      renderDiagnosticsHtml: renderDiagnosticsHtml,
      renderMatrixHtml: renderMatrixHtml,
      renderDetailHtml: renderDetailHtml,
      renderLineChartHtml: renderLineChartHtml,
      renderAftersaleHtml: renderAftersaleHtml
    };
  }
})();
