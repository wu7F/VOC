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
    if (view === 'Wayfair') return snapshot.teams.filter(function (t) { return t.name === 'Wayfair'; });
    return snapshot.teams.filter(function (t) { return t.name === view; });
  }

  function currentSnapshot(state) {
    if (!state) return null;
    return state.view === 'Wayfair' ? state.wayfairSnapshot : state.snapshot;
  }

  function snapshotPlatform(snapshot) {
    return snapshot && snapshot.platform === 'Wayfair' ? 'Wayfair' : 'Amazon';
  }

  function isWayfairSnapshot(snapshot) {
    return snapshotPlatform(snapshot) === 'Wayfair';
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

  function knownUnits(snapshot) {
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
    var platform = snapshotPlatform(snapshot);
    var isWf = platform === 'Wayfair';
    var known = knownUnits(snapshot);
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
    var aftersaleMessage;
    if (isWf) {
      aftersaleMessage = (snapshot.aftersale && snapshot.aftersale.message) ||
        '售后率和原因来自已批准归并，未接入时显示—，不按 0% 计算';
    } else {
      aftersaleMessage = (snapshot.aftersale && snapshot.aftersale.message) ||
        '售后分子和原因尚未接入多维表格,售后率显示“—”,不按 0% 计算';
    }
    if (isWf) {
      return '<div class="kpis">' +
        '<div class="kpi"><small>Wayfair 已知销量合计</small><strong>' + fmtUnits(known) +
        '</strong><span>仅已归属 Wayfair玺家 的已知销量</span></div>' +
        '<div class="kpi"><small>Wayfair 售后率</small><strong>' + esc(aftersaleRate) +
        '</strong><span>' + esc(aftersaleMessage) + '</span></div>' +
        '<div class="kpi"><small>有缺口的店铺数</small><strong>' + gap +
        '</strong><span>存在缺失月份或不完整格</span></div></div>';
    }
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
      ['概览', [['all', 'Amazon 概览'], ['Wayfair', 'Wayfair 概览'], ['Independent', '独立站概览']]],
      ['AMAZON', [['Team 1', 'Team 1'], ['Team 2', 'Team 2'], ['Team 3', 'Team 3']]]
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

  function aggregateProductMonths(snapshot, teamName) {
    var connected = !!(snapshot && snapshot.aftersale && snapshot.aftersale.connected);
    var catalogAvailable = Array.isArray(snapshot && snapshot.products);
    var names = Object.create(null);
    var skuMap = Object.create(null);
    var order = [];

    function initSku(sku) {
      if (!skuMap[sku]) {
        var rec = { sku: sku, months: {}, sales: {} };
        skuMap[sku] = rec;
        order.push(sku);
      }
      return skuMap[sku];
    }

    var wayfair = snapshotPlatform(snapshot) === 'Wayfair';
    var allowedTeams = wayfair ? { 'Wayfair': true } : { 'Team 1': true, 'Team 2': true, 'Team 3': true };
    var allowedMonths = (snapshot && Array.isArray(snapshot.months) && snapshot.months.length)
      ? snapshot.months.slice().sort()
      : (wayfair ? [] : ['2026-04', '2026-05', '2026-06', '2026-07', '2026-08', '2026-09', '2026-10', '2026-11']);
    function monthAllowed(month) {
      if (wayfair) return allowedMonths.indexOf(month) >= 0;
      return month >= '2026-04' && month <= '2026-11';
    }

    if (catalogAvailable) {
      snapshot.products.forEach(function (product) {
        if (!product || product.t01 === true || !product.sku) return;
        var sku = String(product.sku);
        initSku(sku);
        names[sku] = product.display_name || '';
      });
    }

    if (Array.isArray(snapshot && snapshot.teams)) {
      snapshot.teams.forEach(function (t) {
        if (!t || !allowedTeams[t.name]) return;
        var shops = Array.isArray(t.shops) ? t.shops : [];
        shops.forEach(function (shop) {
          var skus = Array.isArray(shop && shop.skus) ? shop.skus : [];
          skus.forEach(function (s) {
            if (!s || !s.sku) return;
            var sku = String(s.sku);
            if (!skuMap[sku]) return;
            var skuRec = skuMap[sku];
            if (!s.sold || typeof s.sold !== 'object') return;
            Object.keys(s.sold).forEach(function (month) {
              if (!monthAllowed(month)) return;
              var val = Number(s.sold[month]);
              if (typeof s.sold[month] === 'number' && isFinite(val) && val >= 0) {
                skuRec.sales[month] = (skuRec.sales[month] || 0) + val;
              }
            });
          });
        });
      });
    }

    if (!connected) {
      return { connected: connected, catalog_available: catalogAvailable, items: order.map(function (sku) { return skuMap[sku]; }), names: names };
    }

    var rows = (snapshot && snapshot.aftersale && snapshot.aftersale.sku_store_month) || [];
    function isValidCount(val) {
      return typeof val === 'number' && isFinite(val) && val >= 0;
    }
    rows.forEach(function (r) {
      if (!r) return;
      if (wayfair) {
        if (r.team !== 'Wayfair') return;
      } else {
        if (r.team !== 'Team 1' && r.team !== 'Team 2' && r.team !== 'Team 3') return;
      }
      var sku = r.sku;
      var month = r.month;
      if (!sku || !month) return;
      sku = String(sku);
      month = String(month);
      if (!monthAllowed(month)) return;
      if (!skuMap[sku]) return;
      if (!isValidCount(r.numerator) || !isValidCount(r.denominator)) return;
      var n = Number(r.numerator);
      var d = Number(r.denominator);
      var skuRec = skuMap[sku];
      if (!skuRec.months[month]) {
        skuRec.months[month] = { numerator: 0, denominator: 0, rate: null };
      }
      var m = skuRec.months[month];
      m.numerator += n;
      m.denominator += d;
      if (m.denominator > 0) {
        m.rate = m.numerator / m.denominator * 100;
      } else {
        m.rate = null;
      }
    });

    var items = order.map(function (sku) {
      return skuMap[sku];
    });

    return { connected: connected, catalog_available: catalogAvailable, items: items, names: names };
  }

  function aggregateProductReasons(snapshot, sku, month) {
    var connected = !!(snapshot && snapshot.aftersale && snapshot.aftersale.connected &&
      snapshot.aftersale.reasons && snapshot.aftersale.reasons.connected);
    if (!connected || !snapshot || !sku || !month) {
      return { connected: connected, items: [], subcategory_items: [] };
    }
    var wayfair = snapshotPlatform(snapshot) === 'Wayfair';
    var allowedTeams = wayfair ? { 'Wayfair': true } : { 'Team 1': true, 'Team 2': true, 'Team 3': true };
    var teamAccounts = Object.create(null);
    if (Array.isArray(snapshot.teams)) {
      snapshot.teams.forEach(function (t) {
        if (t && allowedTeams[t.name]) {
          (t.shops || []).forEach(function (s) {
            if (s && s.account) teamAccounts[s.account] = true;
          });
        }
      });
    }
    var primary = Object.create(null);
    var secondary = Object.create(null);
    var items = Array.isArray(snapshot.aftersale.reasons.items) ? snapshot.aftersale.reasons.items : [];
    var subcategoryItems = Array.isArray(snapshot.aftersale.reasons.subcategory_items) ? snapshot.aftersale.reasons.subcategory_items : [];
    items.forEach(function (r) {
      if (!r || !teamAccounts[r.account] || String(r.sku) !== String(sku) || String(r.month) !== String(month)) return;
      if (!r.category || typeof r.count !== 'number' || !isFinite(r.count) || r.count <= 0) return;
      primary[r.category] = (primary[r.category] || 0) + r.count;
    });
    subcategoryItems.forEach(function (r) {
      if (!r || !teamAccounts[r.account] || String(r.sku) !== String(sku) || String(r.month) !== String(month)) return;
      if (!r.category || !r.subcategory || typeof r.count !== 'number' || !isFinite(r.count) || r.count <= 0) return;
      var k = JSON.stringify([r.category, r.subcategory]);
      secondary[k] = (secondary[k] || 0) + r.count;
    });
    var total = 0;
    Object.keys(primary).forEach(function (category) {
      var count = primary[category];
      if (typeof count === 'number' && isFinite(count) && count > 0) total += count;
    });
    var primaryOut = Object.keys(primary).map(function (category) {
      var count = primary[category];
      var share = total > 0 ? count / total * 100 : 0;
      return { category: category, count: count, share: share };
    }).sort(function (a, b) { return b.share - a.share; });
    var secondaryOut = Object.keys(secondary).map(function (k) {
      var parts = JSON.parse(k);
      var category = parts[0];
      var subcategory = parts[1];
      var count = secondary[k];
      var parentCount = primary[category] || 0;
      var share = parentCount > 0 ? count / parentCount * 100 : 0;
      return { category: category, subcategory: subcategory, count: count, share: share };
    }).sort(function (a, b) {
      if (a.category !== b.category) return a.category.localeCompare(b.category);
      return b.share - a.share;
    });
    return { connected: true, items: primaryOut, subcategory_items: secondaryOut };
  }

  function renderProductReasonTooltipHtml(snapshot, sku, month) {
    var aggregated = aggregateProductReasons(snapshot, sku, month);
    var productName = '';
    if (snapshot && Array.isArray(snapshot.products)) {
      snapshot.products.forEach(function (p) {
        if (p && String(p.sku) === String(sku)) productName = p.display_name || '';
      });
    }
    var displayName = productName && productName !== sku ? productName + '（' + sku + '）' : sku;
    var rateVal = null;
    var productMonths = aggregateProductMonths(snapshot);
    productMonths.items.forEach(function (item) {
      if (String(item.sku) === String(sku) && item.months[month]) rateVal = item.months[month].rate;
    });
    var h = '<div class="product-tip-content"><b>' + esc(displayName) + '</b><br>' +
      esc(month) + ' · 售后率 ' + fmtRate(rateVal) + '<br>';
    if (!aggregated.connected || !aggregated.items.length) {
      h += '<span class="nac">—</span>';
      if (!aggregated.connected && snapshot && snapshot.aftersale && snapshot.aftersale.reasons) {
        h += '<div class="tip-note">' + esc(snapshot.aftersale.reasons.message || '售后原因未接入') + '</div>';
      }
      h += '<button type="button" class="tip-close" aria-label="关闭">×</button></div>';
      return h;
    }
    h += '<div class="tip-reasons">';
    aggregated.items.forEach(function (r) {
      h += '<div>' + esc(r.category) + '：' + fmtRate(r.share) + '</div>';
      var children = aggregated.subcategory_items.filter(function (c) {
        return c.category === r.category;
      });
      if (children.length) {
        h += '<details class="tip-child"><summary>二级原因（占该一级类别）</summary><div class="tip-child-list">';
        children.forEach(function (c) {
          h += '<div>' + esc(c.subcategory) + '：' + fmtRate(c.share) + '（' + fmtUnits(c.count) + ' 条）</div>';
        });
        h += '</div></details>';
      } else {
        h += '<div class="tip-child nac">二级原因未接入</div>';
      }
    });
    h += '</div><button type="button" class="tip-close" aria-label="关闭">×</button></div>';
    return h;
  }

  function renderProductMonthsHtml(snapshot, state) {
    state = state || {};
    var wayfair = snapshotPlatform(snapshot) === 'Wayfair';
    var months = wayfair
      ? ((snapshot && snapshot.months) || []).slice().sort()
      : ['2026-04', '2026-05', '2026-06', '2026-07', '2026-08', '2026-09', '2026-10', '2026-11'];
    if (!months.length) return '<div class="warnbox">暂无月份数据</div>';
    var aggregated = aggregateProductMonths(snapshot);
    var allItems = aggregated.items || [];
    var query = state.productQuery || '';
    var filtered = allItems.filter(function (item) {
      if (!query || !query.trim()) return true;
      var q = query.trim().toLocaleLowerCase();
      var label = ((aggregated.names[item.sku] || '') + ' ' + item.sku).toLocaleLowerCase();
      return label.indexOf(q) >= 0;
    });
    var perPage = 6;
    var total = filtered.length;
    var pages = Math.max(1, Math.ceil(total / perPage));
    var page = Math.max(1, Math.min(state.productPage || 1, pages));
    state.productPage = page;
    var sliceStart = (page - 1) * perPage;
    var pageItems = filtered.slice(sliceStart, sliceStart + perPage);
    if (!aggregated.catalog_available) {
      return '<div class="warnbox">产品目录尚未接入</div>';
    }
    var controls = '<div class="product-controls"><form data-bind="product-search">' +
      '<label for="product-query">搜索产品</label>' +
      '<input id="product-query" type="search" data-bind="product-query" value="' + esc(query || '') + '" placeholder="产品名称或 SKU 编码" autocomplete="off">' +
      '<button type="submit">搜索</button>' +
      '</form>' +
      '<div class="product-page-nav"><button type="button" data-bind="product-page" data-delta="-1"' +
      (page <= 1 ? ' disabled' : '') + '>上一页</button>' +
      '<span>第 ' + page + ' / ' + pages + ' 页 · 共 ' + total + ' 个产品</span>' +
      '<button type="button" data-bind="product-page" data-delta="1"' +
      (page >= pages ? ' disabled' : '') + '>下一页</button></div></div>';
    if (!allItems.length) {
      return controls + (wayfair ? '<div class="warnbox">配对表没有可展示的 Wayfair 产品</div>' : '<div class="warnbox">配对表没有可展示的亚马逊产品</div>');
    }
    if (!pageItems.length) {
      return controls + '<div class="warnbox">搜索没有匹配的 SKU，请清除搜索条件后重试</div>';
    }
    var intro = wayfair
      ? '<div class="product-intro">产品×月份：Wayfair/玺家；月份随 Wayfair 快照明细动态展示；销量0保留0，分母为0或未知时售后率为—且折线留空；有销量且无售后才是0%。悬停或点击数据点查看售后原因，按 Esc 关闭悬停提示。</div>'
      : '<div class="product-intro">产品×月份：全部亚马逊，不随 Team 筛选；固定2026年4–11月；销量0保留0，分母为0或未知时售后率为—且折线留空；有销量且无售后才是0%。悬停或点击数据点查看售后原因，按 Esc 关闭悬停提示。</div>';
    var h = intro;
    if (!aggregated.connected) {
      h += '<div class="warnbox">' + (wayfair
        ? '售后率和原因来自已批准归并，未接入时显示—，不按 0% 计算'
        : '售后分子和原因尚未接入多维表格，售后率显示“—”，不按 0% 计算') + '</div>';
    }
    h += controls;
    h += '<div class="product-cards">';
    pageItems.forEach(function (item, idx) {
      var values = months.map(function (m) {
        var rec = item.months[m];
        return rec ? rec.rate : null;
      });
      var series = [{
        name: item.sku,
        color: color(idx),
        values: values
      }];
      var metadataName = aggregated.names[item.sku];
      var displayName = (metadataName && metadataName !== item.sku) ? metadataName + '（' + item.sku + '）' : item.sku;
      var label = (wayfair ? 'Wayfair ' : '全部亚马逊 ') + displayName + ' 售后率折线图';
      h += '<div class="card product-card"><h3>' + esc(displayName) + '</h3>' +
        '<div class="chart" data-chart="rate" data-product-chart="1" data-sku="' + esc(item.sku) + '">' +
        renderLineChartHtml(series, months, { fmt: fmtRate, label: label, product: true }) +
        '</div>' +
        '<details class="diag product-details"><summary>查看月度明细</summary>' +
        '<div class="product-table-wrap"><table class="product-month-table"><thead><tr><th>月份</th><th>售后件数</th><th>销量（已知）</th><th>售后率</th></tr></thead><tbody>' +
        months.map(function (m) {
          var rec = item.months[m];
          var num = rec && rec.numerator != null ? rec.numerator : '—';
          var den;
          if (rec && rec.denominator != null) {
            den = rec.denominator;
          } else {
            var saleVal = item.sales[m];
            den = (typeof saleVal === 'number' && isFinite(saleVal)) ? saleVal : '—';
          }
          var rate = rec && rec.rate != null ? fmtRate(rec.rate) : '—';
          return '<tr><td>' + m + '</td><td>' + num + '</td><td>' + den + '</td><td>' + rate + '</td></tr>';
        }).join('') +
        '</tbody></table></div>' +
        '</details>' +
        (values.every(function (v) { return v === null; }) ? '<p class="all-gap">暂无可计算的月度售后率，缺失月份保留空档</p>' : '') +
        '</div>';
    });
    h += '</div>';
    return h;
  }

  function renderMatrixHtml(snapshot, state) {
    state = state || {};
    if (!snapshot || snapshot.ok === false) return '<div class="warnbox">没有可用数据</div>';
    var wayfair = snapshotPlatform(snapshot) === 'Wayfair';
    if (state.view === 'Wayfair' && !wayfair) {
      return '<div class="warnbox">Wayfair数据未接入</div>';
    }
    var platform = wayfair ? 'Wayfair' : 'Amazon';
    if (state.view === 'Independent') {
      return '<div class="warnbox">独立站尚未接入</div>';
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
    if (state.view === 'all' || state.view === 'Wayfair') {
      h += '<div class="matrix-mode"><button type="button" data-bind="matrix-mode" data-mode="store" class="' +
        (state.matrixMode !== 'product' ? 'on' : '') + '">店铺×月份</button>' +
        '<button type="button" data-bind="matrix-mode" data-mode="product" class="' +
        (state.matrixMode === 'product' ? 'on' : '') + '">产品×月份</button></div>';
    }
    if ((state.view === 'all' || state.view === 'Wayfair') && state.matrixMode === 'product') {
      h += renderProductMonthsHtml(snapshot, state);
      return h;
    }
    var summaryTitle = state.view === 'all' ? 'Amazon 概览' : (wayfair ? 'Wayfair 概览' : esc(state.view));
    h += '<div class="matrix-summary"><strong>' + summaryTitle +
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
      var teamLabel = (wayfair && team.shops && team.shops.length)
        ? (team.shops[0].display_name || team.shops[0].account)
        : team.name;
      h += '<tr class="g"><td colspan="' + (months.length + 2) + '">' + esc(platform) + ' · ' +
        esc(teamLabel) + ' · 分母 ' + fmtUnits(teamKnown) +
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
    var wayfair = snapshotPlatform(snapshot) === 'Wayfair';
    if (!aftersale || !aftersale.connected) {
      return '<div class="card"><h3>售后率（确认快照）</h3>' +
        '<div class="warnbox">' +
        esc((aftersale && aftersale.message) || (wayfair
          ? '售后率和原因来自已批准归并，未接入时显示—，不按 0% 计算'
          : '售后分子和原因尚未接入多维表格,售后率显示“—”,不按 0% 计算')) +
        '</div></div>';
    }
    var h = '<div class="card"><h3>售后率（只读实时聚合）</h3>' +
      '<p>' + esc(aftersale.message || '') +
      (aftersale.source_as_of ? ' 数据截至 ' + esc(aftersale.source_as_of) + '。' : '') +
      (wayfair ? ' 分母来自当前同一批只读 Wayfair 销量快照，过滤未配对 SKU，仅保留 Wayfair 已配对产品。' :
        ' 分母来自当前同一批只读销量快照，过滤未配对SKU和T01。') + '</p>';

    if (!wayfair) {
      // Team 汇总 (Amazon only)
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
    } else {
      h += '<table class="sk"><thead><tr><th>Wayfair 汇总</th><th>售后分子</th><th>销量分母</th><th>售后率</th></tr></thead><tbody>';
      var wfTot = aftersale.total || aftersale.summary;
      if (wfTot) {
        h += '<tr><td>Wayfair</td><td>' + fmtUnits(wfTot.numerator) +
          '</td><td>' + fmtUnits(wfTot.denominator) + '</td><td>' + fmtRate(wfTot.rate) + '</td></tr>';
      } else {
        h += '<tr><td>Wayfair</td><td>—</td><td>—</td><td>—</td></tr>';
      }
      h += '</tbody></table>';
    }

    // 店铺汇总
    h += '<h3>店铺售后率</h3><table class="sk"><thead><tr><th>店铺</th><th>' +
      (wayfair ? '平台' : 'Team') + '</th><th>售后分子</th><th>销量分母</th><th>售后率</th></tr></thead><tbody>';
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

    // 售后原因占位 / Wayfair 来源说明
    if (wayfair) {
      var reasonsMsg = (aftersale.reasons && aftersale.reasons.message) || aftersale.message ||
        '一级和二级均来自售后原因字段的已批准归并';
      h += '<h3>售后原因占比</h3><p>' + esc(reasonsMsg) + '</p>';
    } else {
      h += '<h3>售后原因占比</h3><div class="warnbox">已发现售后表“原文”字段，但当前只读访问拿不到记录字段值；暂不生成占比，避免把缺失数据当作没有原因。</div>';
    }
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
        var isProduct = opts.product;
        h += '<circle cx="' + x(i) + '" cy="' + y(v) + '" r="' +
          (s.dim ? 2.5 : 4) + '" fill="' + esc(s.color) + '" opacity="' +
          (s.dim ? 0.5 : 1) + '" data-series="' + esc(s.name) + '" data-i="' + i +
          '" data-month="' + esc(months[i]) + '" data-value="' + v + '"' +
          (isProduct ? ' tabindex="0" role="button" aria-label="' + esc(s.name + ' ' + months[i] + ' 售后率 ' + fmtRate(v)) + '"' : '') + '/>';
      });
    });
    h += '</svg><div class="tip' + (opts.product ? ' product-tip' : '') + '"></div>';
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

  function renderReasonsHtml(snapshot, account, sku, month) {
    var reasons = snapshot.aftersale && snapshot.aftersale.reasons;
    var connected = !!(snapshot.aftersale && snapshot.aftersale.connected && reasons && reasons.connected);
    var subcategoryItems = (reasons && Array.isArray(reasons.subcategory_items))
      ? reasons.subcategory_items : [];

    var parentItems = (reasons && Array.isArray(reasons.items) ? reasons.items : []).filter(function (r) {
      return r && r.account === account && r.sku === sku && r.month === month;
    });

    return renderReasonSectionHtml({
      snapshot: snapshot,
      account: account,
      sku: sku,
      month: month,
      reasons: reasons,
      connected: connected,
      parentItems: parentItems,
      subcategoryItems: subcategoryItems
    });
  }

  function renderReasonSectionHtml(ctx) {
    var account = ctx.account, sku = ctx.sku, month = ctx.month;
    var connected = ctx.connected;
    var reasons = ctx.reasons;
    var parentItems = ctx.parentItems || [];
    var subcategoryItems = ctx.subcategoryItems || [];

    var message = (reasons && reasons.message) || (ctx.snapshot && ctx.snapshot.aftersale && ctx.snapshot.aftersale.message) ||
      '售后原因数据暂不可用；未生成占比。';

    if (!connected) {
      return '<p class="nac">—</p>' +
        '<div class="warnbox">' + esc(message) + '</div>';
    }

    if (!parentItems.length) {
      return '<p class="nac">— 该店铺、SKU、月份没有可归因的售后记录；这不代表缺失记录被按 0 处理。</p>';
    }

    var h = '<div class="reason-layout">';
    h += renderReasonPieAndPrimaryTableHtml(parentItems, '售后原因占比');
    h += '<div class="reason-secondary">';
    h += '<h4>二级原因明细</h4>';
    if (ctx.snapshot && snapshotPlatform(ctx.snapshot) === 'Wayfair') {
      h += '<p>一级和二级均来自售后原因字段的已批准归并。二级占比为占该一级类别的百分比。</p>';
    } else {
      h += '<p>二级原因按关键词归类；未命中不猜测。二级占比为占该一级类别的百分比。</p>';
    }
    h += '<div class="reason-secondary-list">';
    h += parentItems.map(function (parent) {
      var children = subcategoryItems.filter(function (child) {
        return child && child.account === account && child.sku === sku && child.month === month &&
          child.category === parent.category;
      });
      var title = '<h5>' + esc(parent.category) + '（' + fmtUnits(parent.count) + ' 条 · ' + fmtRate(parent.share) + '）</h5>';
      if (!children.length) {
        return '<details class="reason-child"><summary>' + title + '</summary>' +
          '<p class="nac">二级原因未接入</p>' +
          '</details>';
      }
      return '<details class="reason-child"><summary>' + title + '</summary>' +
        '<div class="reason-child-table-wrap"><table class="reason-table reason-child-table">' +
        '<thead><tr><th>二级类别</th><th>数量</th><th>占一级类别</th></tr></thead><tbody>' +
        children.map(function (child) {
          var childShareText = (child.share !== null && child.share !== undefined && isFinite(Number(child.share)))
            ? fmtRate(child.share)
            : '—';
          return '<tr><td>' + esc(child.subcategory) + '</td><td>' + fmtUnits(child.count) + '</td><td>' + childShareText + '</td></tr>';
        }).join('') +
        '</tbody></table></div></details>';
    }).join('');
    h += '</div></div></div>';
    return h;
  }

  function renderReasonPieAndPrimaryTableHtml(allItems, label) {
    var retained = [];
    var originalIndexes = [];
    (allItems || []).forEach(function (r, i) {
      if (!r || !r.category) return;
      var countValid = typeof r.count === 'number' && isFinite(r.count) && r.count > 0;
      var shareValid = typeof r.share === 'number' && isFinite(r.share) && r.share > 0;
      if (countValid && shareValid) {
        retained.push(r);
        originalIndexes.push(i);
      }
    });

    var total = 0;
    retained.forEach(function (r) {
      total += Number(r.count);
    });

    var sum = total || 0;
    var svg = '';
    if (retained.length && isFinite(sum) && sum > 0) {
      var W = 220, H = 220, cx = W / 2, cy = H / 2, radius = Math.min(W, H) / 2 - 8;
      var startAngle = -Math.PI / 2;
      svg = '<svg class="reason-pie-svg" viewBox="0 0 ' + W + ' ' + H + '" role="img" aria-label="' + esc(label || 'Reason pie') + '">' +
        '<title>' + esc(label || 'Reason pie') + '</title>' +
        '<desc>' + esc(label || '') + '，' + retained.length + ' 个类别，共 ' + fmtUnits(sum) + ' 条</desc>';
      retained.forEach(function (r, idx) {
        var count = Number(r.count);
        var share = Number(r.share);
        var angle = count / sum * Math.PI * 2;
        var endAngle = startAngle + angle;
        var largeArc = angle > Math.PI ? 1 : 0;
        var x1 = cx + radius * Math.cos(startAngle);
        var y1 = cy + radius * Math.sin(startAngle);
        var x2 = cx + radius * Math.cos(endAngle);
        var y2 = cy + radius * Math.sin(endAngle);
        var colorIndex = originalIndexes[idx];
        var pathD;
        if (retained.length === 1) {
          pathD = 'M ' + cx.toFixed(2) + ' ' + (cy - radius).toFixed(2) + ' A ' + radius.toFixed(2) + ' ' + radius.toFixed(2) + ' 0 1 1 ' + cx.toFixed(2) + ' ' + (cy + radius).toFixed(2) + ' A ' + radius.toFixed(2) + ' ' + radius.toFixed(2) + ' 0 1 1 ' + cx.toFixed(2) + ' ' + (cy - radius).toFixed(2) + ' Z';
        } else {
          pathD = 'M ' + cx.toFixed(2) + ' ' + cy.toFixed(2) + ' L ' + x1.toFixed(2) + ' ' + y1.toFixed(2) + ' A ' + radius.toFixed(2) + ' ' + radius.toFixed(2) + ' 0 ' + largeArc + ' 1 ' + x2.toFixed(2) + ' ' + y2.toFixed(2) + ' Z';
        }
        svg += '<path d="' + pathD + '" fill="' + esc(color(colorIndex)) + '" stroke="var(--bg)" stroke-width="2">' +
          '<title>' + esc(r.category) + ' ' + fmtUnits(count) + ' 条（' + fmtRate(share) + '）</title>' +
          '</path>';
        startAngle = endAngle;
      });
      svg += '</svg>';
    } else {
      svg = '<div class="reason-pie-empty">—</div>';
    }

    var table = '<table class="reason-table reason-primary-table"><thead><tr><th>一级类别</th><th>数量</th><th>占售后原因</th></tr></thead><tbody>';
    (allItems || []).forEach(function (r, i) {
      if (!r || !r.category) return;
      table += '<tr><td><span class="dot" style="background:' + esc(color(i)) + '"></span>' + esc(r.category) + '</td>' +
        '<td>' + fmtUnits(r.count) + '</td><td>' + fmtRate(r.share) + '</td></tr>';
    });
    table += '</tbody></table>';

    return '<div class="reason-pie-and-table">' +
      '<div class="reason-pie-wrap">' + svg + '</div>' +
      '<div class="reason-primary-table-wrap">' + table + '</div>' +
      '</div>';
  }

  function renderReasonPieHtml(items, label) {
    // Kept for compatibility; new render path uses renderReasonPieAndPrimaryTableHtml.
    return renderReasonPieAndPrimaryTableHtml(items, label);
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

    var platform = snapshotPlatform(snapshot);
    var h = '<div class="crumb"><button data-bind="action" data-action="back">← 返回矩阵</button>' +
      '<span>/</span><span>' + esc(platform) + ' · ' + esc(teamName || '') + '</span><span>/</span><b>' +
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
      if (snapshotPlatform(snapshot) === 'Wayfair') {
        var wfMsg = (snapshot.aftersale && snapshot.aftersale.message) ||
          '售后率和原因来自已批准归并，未接入时显示—，不按 0% 计算';
        h += '<p>' + esc(wfMsg) + '</p>' +
          '<div class="warnbox">' + esc(wfMsg) + '</div>';
      } else {
        h += '<p>售后分子和原因尚未接入多维表格，售后率显示“—”，不按 0% 计算。</p>' +
          '<div class="warnbox">' +
          esc((snapshot.aftersale && snapshot.aftersale.message) ||
            '售后分子和原因尚未接入多维表格,售后率显示“—”,不按 0% 计算') +
          '</div>';
      }
    }
    h += '</div>';

    // ② 售后原因占比
    h += '<div class="card"><h3>② ' + esc(skuDisplayName || '') + ' (' + esc(sku.sku) + ') 售后原因占比</h3>';
    var reasons = snapshot.aftersale && snapshot.aftersale.reasons;
    var reasonItems = reasons && Array.isArray(reasons.items) ? reasons.items.filter(function (r) {
      return r && r.account === shop.account && r.sku === sku.sku && r.month === month;
    }) : [];
    var reasonsConnected = !!(aftersaleConnected && reasons && reasons.connected);
    h += '<p>' + esc(shop.account) + ' · ' + esc(month) + ' · 当前SKU 的售后原因占比；仅显示服务端汇总类别，不显示原文。</p>';
    h += renderReasonSectionHtml({
      snapshot: snapshot,
      account: shop.account,
      sku: sku.sku,
      month: month,
      reasons: reasons,
      connected: reasonsConnected,
      parentItems: reasonItems,
      subcategoryItems: (reasons && Array.isArray(reasons.subcategory_items)) ? reasons.subcategory_items : [],
      scopeLabel: shop.account + ' · ' + month + ' · 当前SKU'
    });
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
    if (snapshotPlatform(snapshot) === 'Wayfair') {
      var diag = (snapshot.diagnostics || []).slice();
      var diagItems = diag.filter(function (d) { return d && typeof d === 'object'; });
      var lineHtml = diagItems.map(function (d) {
        var countText = (typeof d.count === 'number' && isFinite(d.count)) ? fmtUnits(d.count) : '';
        var text = esc(d.message || d.code || '');
        if (countText) text += '：' + countText;
        var cls = 'nac';
        if (d.level === 'error') cls = 'bad';
        else if (d.level === 'warning') cls = 'warn';
        return '<li class="' + cls + '">' + text + '</li>';
      }).join('');
      var unpairedSales = (snapshot.unpaired && snapshot.unpaired.sales) || [];
      var unpairedAftersale = (snapshot.unpaired && snapshot.unpaired.aftersale) || [];
      var salesHtml = '';
      var aftersaleHtml = '';
      if (unpairedSales.length) {
        var salesTotalUnits = 0;
        unpairedSales.forEach(function (r) { salesTotalUnits += r.units || 0; });
        var salesSorted = unpairedSales.slice().sort(function (a, b) {
          if (a.units !== b.units) return b.units - a.units;
          return String(a.sku).localeCompare(String(b.sku));
        });
        var salesLis = salesSorted.map(function (r) {
          return '<li>' + esc(r.sku) + '（' + fmtUnits(r.units) + ' 件）</li>';
        }).join('');
        salesHtml = '<details><summary>未配对销量 SKU（' + fmtUnits(salesTotalUnits) + ' 件）</summary><ul>' +
          salesLis + '</ul></details>';
      }
      if (unpairedAftersale.length) {
        var aftersaleTotalRows = 0;
        unpairedAftersale.forEach(function (r) { aftersaleTotalRows += r.rows || 0; });
        var aftersaleSorted = unpairedAftersale.slice().sort(function (a, b) {
          if (a.rows !== b.rows) return b.rows - a.rows;
          return String(a.sku).localeCompare(String(b.sku));
        });
        var aftersaleLis = aftersaleSorted.map(function (r) {
          return '<li>' + esc(r.sku) + '（' + fmtUnits(r.rows) + ' 行）</li>';
        }).join('');
        aftersaleHtml = '<details><summary>未配对售后 SKU（' + fmtUnits(aftersaleTotalRows) + ' 行）</summary><ul>' +
          aftersaleLis + '</ul></details>';
      }
      var wfCount = diagItems.length;
      if (!lineHtml && !salesHtml && !aftersaleHtml) return '';
      return '<details class="diag"><summary>Wayfair 数据诊断（' + wfCount + '）</summary>' +
        (lineHtml ? '<ul>' + lineHtml + '</ul>' : '') + salesHtml + aftersaleHtml + '</details>';
    }
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
    // 未配对 SKU 明细（按 Team 分组，作为附加说明）
    var detailGroups = [];
    (snapshot.teams || []).forEach(function (t) {
      var items = [];
      if (Array.isArray(t.unpaired_sku_totals)) {
        items = t.unpaired_sku_totals;
      } else if ((t.unpaired_skus || []).length) {
        items = t.unpaired_skus.slice().sort().map(function (sku) {
          return { sku: sku, units: null, known_qty: 0, has_unknown: true };
        });
      }
      if (!items.length) return;
      var sorted = items.slice().sort(function (a, b) {
        if (a.has_unknown !== b.has_unknown) return a.has_unknown ? 1 : -1;
        if (a.units !== null && b.units !== null && a.units !== b.units) return b.units - a.units;
        return a.sku < b.sku ? -1 : (a.sku > b.sku ? 1 : 0);
      });
      var lis = sorted.map(function (item) {
        if (item.has_unknown) {
          var note = item.known_qty > 0 ? '，已知 ' + fmtUnits(item.known_qty) + ' 件' : '';
          var text = Array.isArray(t.unpaired_sku_totals) ? '—' : '件数未提供';
          return '<li>' + esc(item.sku) + '（' + text + note + '）</li>';
        }
        return '<li>' + esc(item.sku) + '（' + fmtUnits(item.units) + ' 件）</li>';
      }).join('');
      detailGroups.push('<li>' + esc(t.name) + '：<ul>' + lis + '</ul></li>');
    });
    var mergedGroups = [];
    (snapshot.teams || []).forEach(function (t) {
      var items = t.merged_sku_totals || [];
      if (!items.length) return;
      var sorted = items.slice().sort(function (a, b) {
        if (a.has_unknown !== b.has_unknown) return a.has_unknown ? 1 : -1;
        if (a.units !== null && b.units !== null && a.units !== b.units) return b.units - a.units;
        return a.sku < b.sku ? -1 : (a.sku > b.sku ? 1 : 0);
      });
      var lis = sorted.map(function (item) {
        var label = item.has_unknown
          ? (item.known_qty > 0 ? '已并入 ' + fmtUnits(item.known_qty) + ' 件（有未知）' : '已并入（件数未知）')
          : '已并入 ' + fmtUnits(item.units) + ' 件';
        return '<li>' + esc(item.sku) + ' → ' + esc(item.target_sku) + '（' + label + '）</li>';
      }).join('');
      mergedGroups.push('<li>' + esc(t.name) + '：<ul>' + lis + '</ul></li>');
    });
    var mergedHtml = '';
    if (mergedGroups.length) {
      mergedHtml = '<details><summary>已并入 SKU（组件原始件数）</summary><ul>' +
        mergedGroups.join('') + '</ul></details>';
    }
    var detailHtml = '';
    if (detailGroups.length) {
      detailHtml = '<details><summary>未配对 SKU 明细（按 Team）</summary><ul>' +
        detailGroups.join('') + '</ul></details>';
    }
    if (!lines.length && !detailHtml && !mergedHtml) return '';
    var count = (snapshot.diagnostics || []).length + (unmapped.length ? 1 : 0);
    return '<details class="diag"><summary>数据诊断（' + count + '）</summary><ul>' +
      lines.join('') + '</ul>' + detailHtml + mergedHtml + '</details>';
  }

  function initBoard() {
    var state = {
      view: 'all',
      shop: null,
      month: null,
      selectedSku: null,
      skuSearch: '',
      matrixMode: 'store',
      productPage: 1,
      productQuery: '',
      snapshot: null,
      error: null,
      wayfairSnapshot: null,
      wayfairError: null,
      fetchSeq: 0
    };
    var navEl = document.getElementById('nav');
    var mainEl = document.getElementById('main');
    var bannerEl = document.getElementById('banner');
    var staleEl = document.getElementById('stale-banner');

    var pinnedProductTip = null;

    function hideProductTip(chart) {
      if (!chart) return;
      var tip = chart.querySelector('.tip.product-tip');
      if (!tip) return;
      tip.style.display = 'none';
      tip.classList.remove('pinned');
      if (pinnedProductTip === tip) pinnedProductTip = null;
    }

    function pinProductTip(tip) {
      if (!tip) return;
      unpinProductTip();
      tip.style.display = 'block';
      tip.classList.add('pinned');
      pinnedProductTip = tip;
    }

    function unpinProductTip() {
      if (pinnedProductTip) {
        pinnedProductTip.classList.remove('pinned');
        if (pinnedProductTip.style.display !== 'none') {
          pinnedProductTip.style.display = 'none';
        }
      }
      pinnedProductTip = null;
    }

    function showProductTipFromCircle(circle, pin) {
      var chart = circle.closest('[data-product-chart]');
      if (!chart) return;
      var tip = chart.querySelector('.tip.product-tip');
      if (!tip) return;
      if (pinnedProductTip && pinnedProductTip !== tip) {
        unpinProductTip();
      }
      var sku = chart.getAttribute('data-sku');
      var month = circle.getAttribute('data-month');
      tip.innerHTML = renderProductReasonTooltipHtml(activeSnapshot(), sku, month);
      tip.style.display = 'block';
      if (pin) {
        pinProductTip(tip);
      } else if (pinnedProductTip !== tip) {
        tip.classList.remove('pinned');
      }
      var chartRect = chart.getBoundingClientRect();
      var circleRect = circle.getBoundingClientRect();
      var tipRect = tip.getBoundingClientRect();
      var left = circleRect.left - chartRect.left + circleRect.width + 12;
      if (left + tipRect.width > chartRect.width - 8) {
        left = circleRect.left - chartRect.left - tipRect.width - 12;
        if (left < 8) left = Math.max(8, chartRect.width - tipRect.width - 8);
      }
      tip.style.left = left + 'px';
      tip.style.top = Math.max(0, circleRect.top - chartRect.top - 10) + 'px';
    }

    function attachChartHover(root) {
      root.querySelectorAll('[data-chart] circle').forEach(function (c) {
        c.addEventListener('mouseenter', function () {
          var chart = c.closest('[data-chart]');
          if (!chart) return;
          var isProduct = chart.hasAttribute('data-product-chart');
          if (isProduct) {
            showProductTipFromCircle(c, false);
            return;
          }
          var tip = chart.querySelector('.tip');
          if (!tip) return;
          var chartType = chart.getAttribute('data-chart');
          var fmt = chartType === 'rate' ? fmtRate : fmtUnits;
          tip.style.display = 'block';
          tip.innerHTML = '<b>' + esc(c.getAttribute('data-series')) + '</b><br>' +
            esc(c.getAttribute('data-month')) + ' · ' + fmt(Number(c.getAttribute('data-value')));
        });
        c.addEventListener('mousemove', function (e) {
          var chart = c.closest('[data-chart]');
          if (!chart || chart.hasAttribute('data-product-chart')) return;
          var tip = chart.querySelector('.tip');
          if (!tip) return;
          var r = chart.getBoundingClientRect();
          tip.style.left = (e.clientX - r.left + 12) + 'px';
          tip.style.top = (e.clientY - r.top - 40) + 'px';
        });
        c.addEventListener('mouseleave', function () {
          var chart = c.closest('[data-chart]');
          if (!chart) return;
          if (chart.hasAttribute('data-product-chart')) return;
          var tip2 = chart.querySelector('.tip');
          if (tip2) tip2.style.display = 'none';
        });
        c.addEventListener('focus', function () {
          if (c.closest('[data-product-chart]')) showProductTipFromCircle(c, false);
        });
        c.addEventListener('click', function (e) {
          if (!c.closest('[data-product-chart]')) return;
          e.preventDefault();
          e.stopPropagation();
          showProductTipFromCircle(c, true);
        });
        c.addEventListener('keydown', function (e) {
          if (!c.closest('[data-product-chart]')) return;
          if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault();
            showProductTipFromCircle(c, true);
          } else if (e.key === 'Escape') {
            var chart = c.closest('[data-product-chart]');
            hideProductTip(chart);
          }
        });
      });
      root.querySelectorAll('[data-product-chart]').forEach(function (chart) {
        chart.addEventListener('mouseleave', function () {
          var tip = chart.querySelector('.tip.product-tip');
          if (tip && pinnedProductTip !== tip && !tip.matches(':hover')) hideProductTip(chart);
        });
        chart.addEventListener('focusout', function (e) {
          if (pinnedProductTip) return;
          if (chart.contains(e.relatedTarget)) return;
          var tip = chart.querySelector('.tip.product-tip');
          if (tip) tip.style.display = 'none';
        });
        chart.addEventListener('click', function (e) {
          if (e.target.closest && e.target.closest('.tip-close')) {
            e.preventDefault();
            e.stopPropagation();
            hideProductTip(chart);
          }
        });
      });
    }

    document.addEventListener('keydown', function (e) {
      if (e.key !== 'Escape') return;
      unpinProductTip();
      mainEl.querySelectorAll('.tip.product-tip').forEach(function (tip) {
        tip.style.display = 'none';
        tip.classList.remove('pinned');
      });
    });

    function activeSnapshot() {
      return currentSnapshot(state);
    }

    function activeError() {
      return state.view === 'Wayfair' ? state.wayfairError : state.error;
    }

    function render() {
      unpinProductTip();
      navEl.innerHTML = renderNavHtml(state);
      if (state.view === 'Independent') {
        bannerEl.innerHTML = '';
        staleEl.innerHTML = '';
        mainEl.innerHTML = '<div class="warnbox">独立站尚未接入</div>';
        return;
      }
      var snap = activeSnapshot();
      var err = activeError();
      if (!snap) {
        bannerEl.innerHTML = '<span class="live"><i></i><span>暂无数据</span></span>';
        staleEl.innerHTML = err ?
          renderStaleHtml(null, (err && err.message) || String(err)) : '';
        if (err) {
          mainEl.innerHTML = '<div class="warnbox">加载失败：' +
            esc((err && err.message) || String(err)) +
            '</div><div class="warnbox"><button data-bind="action" data-action="retry">重试</button></div>';
        } else {
          mainEl.innerHTML = '<div class="warnbox">正在加载多维表格…</div>';
        }
        return;
      }
      bannerEl.innerHTML = renderBannerHtml(snap);
      if (err) {
        staleEl.innerHTML = renderStaleHtml(
          (err.last_success_at || snap.last_success_at || snap.fetched_at),
          (err.message || String(err))
        );
      } else {
        staleEl.innerHTML = '';
      }
      var content = state.shop ?
        renderDetailHtml(snap, state) :
        renderMatrixHtml(snap, state);
      mainEl.innerHTML = content + renderDiagnosticsHtml(snap);
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

    function handleResponse(r) {
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
    }

    function fetchAmazon(refresh) {
      var seq = ++state.fetchSeq;
      fetch(refresh ? '/api/bitable/board?refresh=1' : '/api/bitable/board')
        .then(handleResponse)
        .then(function (snap) {
          if (seq !== state.fetchSeq) return;
          if (state.view === 'Wayfair' || state.view === 'Independent') return;
          validateSnapshotShape(snap);
          if (snap.platform && snap.platform !== 'Amazon') {
            var amazonError = { message: '平台数据不匹配：仅能接入Amazon数据' };
            if (state.snapshot) {
              amazonError.last_success_at = state.snapshot.last_success_at || state.snapshot.fetched_at;
            }
            state.error = amazonError;
            render();
            return;
          }
          state.snapshot = snap;
          reconcileSelectedSku(snap, state);
          if (!state.shop && !state.selectedSku && snap.teams.length) {
            var firstShop = snap.teams[0].shops && snap.teams[0].shops[0];
            if (firstShop && firstShop.skus && firstShop.skus.length) {
              state.selectedSku = firstShop.skus[0].sku;
            }
          }
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
          if (state.view === 'Wayfair' || state.view === 'Independent') return;
          var errorObj = { message: (err && err.message) ? err.message : String(err) };
          if (state.snapshot) {
            errorObj.last_success_at = state.snapshot.last_success_at || state.snapshot.fetched_at;
          }
          state.error = errorObj;
          render();
        });
    }

    function fetchWayfair(refresh) {
      var seq = ++state.fetchSeq;
      fetch(refresh ? '/api/bitable/wayfair?refresh=1' : '/api/bitable/wayfair')
        .then(handleResponse)
        .then(function (snap) {
          if (seq !== state.fetchSeq) return;
          if (state.view !== 'Wayfair') return;
          validateSnapshotShape(snap);
          if (snap.platform !== 'Wayfair') {
            var wayfairError = { message: '平台数据不匹配：仅能接入Wayfair数据' };
            if (state.wayfairSnapshot) {
              wayfairError.last_success_at = state.wayfairSnapshot.last_success_at || state.wayfairSnapshot.fetched_at;
            }
            state.wayfairError = wayfairError;
            render();
            return;
          }
          state.wayfairSnapshot = snap;
          reconcileSelectedSku(snap, state);
          if (!state.shop && !state.selectedSku && snap.teams.length) {
            var firstShop = snap.teams[0].shops && snap.teams[0].shops[0];
            if (firstShop && firstShop.skus && firstShop.skus.length) {
              state.selectedSku = firstShop.skus[0].sku;
            }
          }
          if (snap.stale || snap.error) {
            state.wayfairError = {
              message: snap.error || '数据已过期，显示上次成功数据',
              last_success_at: snap.last_success_at || snap.fetched_at || null
            };
          } else {
            state.wayfairError = null;
          }
          render();
        })
        .catch(function (err) {
          if (seq !== state.fetchSeq) return;
          if (state.view !== 'Wayfair') return;
          var errorObj = { message: (err && err.message) ? err.message : String(err) };
          if (state.wayfairSnapshot) {
            errorObj.last_success_at = state.wayfairSnapshot.last_success_at || state.wayfairSnapshot.fetched_at;
          }
          state.wayfairError = errorObj;
          render();
        });
    }

    function fetchActive(refresh) {
      if (state.view === 'Wayfair') return fetchWayfair(refresh);
      if (state.view === 'Independent') return;
      return fetchAmazon(refresh);
    }

    document.addEventListener('input', function (e) {
      var target = e.target;
      if (!target || !target.getAttribute) return;
      if (target.getAttribute('data-bind') !== 'sku-search') return;
      state.skuSearch = target.value || '';
      var shop = activeSnapshot() ? findShop(activeSnapshot(), state.shop) : null;
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

    document.addEventListener('submit', function (e) {
      var form = e.target && e.target.closest ? e.target.closest('[data-bind="product-search"]') : null;
      if (!form) return;
      e.preventDefault();
      var input = form.querySelector('[data-bind="product-query"]');
      state.productQuery = input ? input.value.trim() : '';
      state.productPage = 1;
      render();
    });

    document.addEventListener('click', function (e) {
      var el = e.target.closest ? e.target.closest('[data-bind]') : null;
      if (!el) return;
      var bind = el.getAttribute('data-bind');

      if (bind === 'matrix-mode') {
        var newMode = el.getAttribute('data-mode') === 'product' ? 'product' : 'store';
        if (newMode === 'product' && state.view !== 'all' && state.view !== 'Wayfair') return;
        state.matrixMode = newMode;
        state.shop = null;
        state.month = null;
        state.productPage = 1;
        state.productQuery = '';
        unpinProductTip();
        render();
        return;
      }
      if (bind === 'product-page') {
        var delta = Number(el.getAttribute('data-delta')) || 0;
        state.productPage = (state.productPage || 1) + delta;
        render();
        return;
      }
      if (bind === 'nav') {
        var newView = el.getAttribute('data-view') || 'all';
        if (newView !== 'all') state.matrixMode = 'store';
        state.view = newView;
        state.shop = null;
        state.month = null;
        state.selectedSku = null;
        state.skuSearch = '';
        state.productPage = 1;
        state.productQuery = '';
        unpinProductTip();
        render();
        fetchActive(false);
        return;
      }
      if (bind === 'matrix-cell') {
        state.shop = el.getAttribute('data-shop');
        state.month = el.getAttribute('data-month');
        state.skuSearch = '';
        // 设置默认选中第一个 SKU
        var shop = activeSnapshot() ? findShop(activeSnapshot(), state.shop) : null;
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
        var shop2 = activeSnapshot() ? findShop(activeSnapshot(), state.shop) : null;
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
          fetchActive(true);
        }
        return;
      }
    });

    fetchActive(false);
    setInterval(function () {
      if (!document.hidden) fetchActive(false);
    }, 30000);
  }

  if (typeof document !== 'undefined') {
    initBoard();
  }

  if (typeof module !== 'undefined') {
    module.exports = {
      fmtUnits: fmtUnits,
      fmtRate: fmtRate,
      esc: esc,
      matchesSkuSearch: matchesSkuSearch,
      monthLabel: monthLabel,
      renderBannerHtml: renderBannerHtml,
      renderStaleHtml: renderStaleHtml,
      renderDiagnosticsHtml: renderDiagnosticsHtml,
      renderMatrixHtml: renderMatrixHtml,
      renderDetailHtml: renderDetailHtml,
      renderLineChartHtml: renderLineChartHtml,
      renderAftersaleHtml: renderAftersaleHtml,
      renderReasonsHtml: renderReasonsHtml,
      renderReasonPieHtml: renderReasonPieHtml,
      aggregateProductMonths: aggregateProductMonths,
      renderProductMonthsHtml: renderProductMonthsHtml,
      aggregateProductReasons: aggregateProductReasons,
      renderProductReasonTooltipHtml: renderProductReasonTooltipHtml
    };
  }
})();
