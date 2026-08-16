# -*- coding: utf-8 -*-
"""
生成"实时自刷新"看板 dashboard.html
=====================================
机制:
  - 页面打开后, 浏览器每 20 秒通过 <script> 标签(JSONP 方式, 无 CORS/Referer 限制)
    直接请求公开行情接口, 实时刷新:
      主通道: 腾讯 qt.gtimg.cn (股票/ETF/指数, 含涨跌幅与昨收)
      备通道: 东财 push2 ulist.np (cb= JSONP)
  - 持仓(2026Q2披露)与官方基线由 fund_tracker.py 生成的 data/latest.json 内嵌
    (每日收盘后由自动化任务刷新)
  - 混合基金"基金本身"实时值 = 浏览器内用持仓实时行情加权计算(用户要求的方法论);
    ETF"基金本身" = 场内实时价
  - 行情源故障时自动回退: 实时通道 -> 最近一次快照, 并提示
"""
import json, os
import fund_db

BASE = os.path.dirname(os.path.abspath(__file__))
# 数据真相源为数据库(app_kv); 兼容: DB 无数据时回退磁盘 JSON
_DATA = fund_db.kv_get("latest.json")
if _DATA is None:
    try:
        with open(os.path.join(BASE, "data", "latest.json"), encoding="utf-8") as f:
            _DATA = json.load(f)
    except Exception:
        _DATA = {}
DATA = _DATA

# ECharts 内联(彻底绕开外部脚本加载被缓存/扩展干扰的问题; 文件已验证无 </script>/<!-- 破坏序列)
try:
    with open(os.path.join(BASE, "static", "echarts.v2.min.js"), encoding="utf-8") as f:
        ECHARTS_INLINE = f.read()
except Exception:
    ECHARTS_INLINE = ""

# 基金配置(取估算锚等元信息, 供页面实时通道判断)
try:
    _fc = fund_db.kv_get("funds.json")
    if _fc is None:
        with open(os.path.join(BASE, "config", "funds.json"), encoding="utf-8") as f:
            _fc = json.load(f)
    FUND_CFG = _fc.get("funds", {}) if isinstance(_fc, dict) else {}
except Exception:
    FUND_CFG = {}

# 标签知识库(含义/特征/注意/参考链接)
try:
    _td = fund_db.kv_get("tag_defs.json")
    if _td is None:
        with open(os.path.join(BASE, "config", "tag_defs.json"), encoding="utf-8") as f:
            _td = json.load(f)
    TAG_DEFS = _td.get("tags", {}) if isinstance(_td, dict) else {}
except Exception:
    TAG_DEFS = {}

# 历史修正记录(最近若干条, 留作"预估vs实际"判断)
try:
    _, corr_rows = fund_db.query("corrections", limit=20, order="DESC")
    CORRECTIONS = [{"date": r[0], "code": r[1], "official": r[3], "last_model": r[4],
                    "min_model": r[5], "max_model": r[6], "snaps": r[7], "bias": r[8]} for r in corr_rows]
except Exception:
    CORRECTIONS = []

# 历史净值(曲线)数据: 不再内嵌全量到页面(页面体积大), 改为页面打开时通过接口 /api/navs/<code> 从数据库异步拉取
NAVS = {}

# 提取页面实时引擎所需配置(轻量, 不含历史快照)
CFG = {"funds": {}, "indices": {}}
for code, meta in (DATA.get("funds") or {}).items():
    CFG["funds"][code] = {
        "name": meta["fund_name"], "type": meta["type"],
        "self_sina": meta.get("self_sina", ""),
        "tags": meta.get("tags") or [],
        "anchor_tencent": (FUND_CFG.get(meta["fund_code"]) or {}).get("anchor_tencent") or "",
        "anchor_name": (FUND_CFG.get(meta["fund_code"]) or {}).get("anchor_name") or "",
        "buy_fee_rate": (FUND_CFG.get(meta["fund_code"]) or {}).get("buy_fee_rate") or 0,
        "sell_fee_rate": (FUND_CFG.get(meta["fund_code"]) or {}).get("sell_fee_rate") or 0,
        "quarter": meta["holdings_quarter"], "report_date": meta["holdings_report_date"],
        "holdings_penetrated": bool(meta.get("holdings_penetrated")),
        "penetrated_via": meta.get("penetrated_via") or [],
        "holdings_direct": meta.get("holdings_direct") or [],
        "position": meta.get("position") or {},
        "trades": meta.get("trades") or [],
        "trade_summary": meta.get("trade_summary") or {},
        "realtime": bool(meta.get("realtime")),
        "baseline_desc": (meta.get("baseline") or {}).get("desc", "未获取"),
        "baseline_source": "",  # 脱敏: 不向前端注入数据来源信息
        "coverage": meta["est"].get("disclosed_coverage_pct"),
        "model_snapshot": meta["est"].get("model_change_pct"),
        "adjusted_snapshot": meta["est"].get("adjusted_model_change_pct"),
        "official_snapshot": meta["official"].get("chg_pct") if meta["official"] else None,
        "official_snapshot_desc": (meta["official"].get("type") + " " + str(meta["official"].get("chg_pct")) + "%") if meta["official"] else "",
        "stocks": [{"code": s["code"], "name": s["name"], "weight": s["weight_pct"],
                    "sina": s["sina"]} for s in meta["stocks"]],
    }
    # 最近官方净值(用于"非实时"基金的历史兜底展示, 以及页面回退)
    off = meta.get("official") or {}
    if off.get("type") == "最近官方净值(非实时)":
        CFG["funds"][meta["fund_code"]]["nav_latest"] = {"date": off.get("nav_date"), "chg_pct": off.get("chg_pct")}
    else:
        lo = off.get("last_official") or {}
        CFG["funds"][meta["fund_code"]]["nav_latest"] = {"date": lo.get("date"), "chg_pct": lo.get("chg_pct")}
# 指数: ETF 余量用中证消费(sh000932)近似, 混合用中证医疗(sz399989)
CFG["indices"] = {"516670": {"tencent": "sh000932", "em": "1.000932", "name": "中证消费(近似)"},
                  "003095": {"tencent": "sz399989", "em": "0.399989", "name": "中证医疗"}}
CFG["generated_at"] = DATA.get("generated_at") or ""
CFG["market_status"] = DATA.get("market_status") or ""
CFG["corrections"] = CORRECTIONS
CFG["tag_defs"] = TAG_DEFS

JS = r"""
var CFG = __CFG__;
var UP = "#d93025", DOWN = "#0a9a5a", FLAT = "#6b7280";
/* 金额总览明细: 折叠状态 + 排序(列/方向) */
var sumFold = "0", sumSortKey = "", sumSortDir = -1;
var POLL_MS = 60000, pollTimer = null;
var state = { source: "快照", ok: false, fail: 0, last: null };

function sv(v, d){ if(v == null || isNaN(v)) return "—"; d = d || 2; return (v > 0 ? "+" : "") + Number(v).toFixed(d); }
function col(v){ return (v == null || isNaN(v)) ? "#9ca3af" : (v > 0 ? UP : (v < 0 ? DOWN : FLAT)); }
function fmtTs(ts){ if(!ts) return "—"; var s = String(ts); if(s.length >= 14) return s.slice(0,4)+"-"+s.slice(4,6)+"-"+s.slice(6,8)+" "+s.slice(8,10)+":"+s.slice(10,12)+":"+s.slice(12,14); if(s.length >= 8) return s.slice(0,4)+"-"+s.slice(4,6)+"-"+s.slice(6,8); return s; }

/* ---------------- 实时通道 ---------------- */
function tencentSymbols(){ var a = []; var funds = CFG.funds;
  Object.keys(funds).forEach(function(k){ var f = funds[k];
    f.stocks.forEach(function(s){ if(s.sina) a.push(s.sina); });
    if (f.self_sina) a.push(f.self_sina);
    if (CFG.indices[k] && CFG.indices[k].tencent) a.push(CFG.indices[k].tencent);
  }); return a.filter(function(v,i,self){ return v && self.indexOf(v) === i; }); }
function emSecids(){ var a = []; var funds = CFG.funds;
  Object.keys(funds).forEach(function(k){ var f = funds[k];
    f.stocks.forEach(function(s){ if(s.code) a.push(codeToEM(s.code)); });
    if (f.self_sina && f.self_sina.length > 2) a.push(codeToEM(f.self_sina.slice(2)));
    if (CFG.indices[k] && CFG.indices[k].em) a.push(CFG.indices[k].em);
  }); return a.filter(function(v,i,self){ return v && self.indexOf(v) === i; }); }
function codeToEM(code){ var c = String(code); return (c.charAt(0) === "6" || c.charAt(0) === "9" || c.charAt(0) === "5") ? "1." + c : "0." + c; }

function loadScript(src, charset, onload, onerror){
  var s = document.createElement("script");
  s.src = src; s.charset = charset || "utf-8";
  s.onload = function(){ onload && onload(); s.parentNode && s.parentNode.removeChild(s); };
  s.onerror = function(){ onerror && onerror(); s.parentNode && s.parentNode.removeChild(s); };
  document.head.appendChild(s);
}
/* 腾讯通道: qt.gtimg.cn/q=... 返回 v_shxxx="..." 全局变量 */
function pollTencent(cb){
  var syms = tencentSymbols();
  loadScript("https://qt.gtimg.cn/q=" + syms.join(",") + "&_=" + Date.now(), "gbk", function(){
    var out = {};
    syms.forEach(function(sym){
      var raw = window["v_" + sym]; if(!raw) return;
      var p = raw.split("~");
      if(p.length < 33) return;
      out[sym] = { price: parseFloat(p[3]), prev: parseFloat(p[4]), chg: parseFloat(p[32]), ts: p[30], name: p[1] };
    });
    cb(Object.keys(out).length ? out : null);
  }, function(){ cb(null); });
}
/* 东财备通道: push2 ulist JSONP */
function pollEM(cb){
  var secids = emSecids();
  var cbName = "emq" + Date.now();
  window[cbName] = function(d){
    delete window[cbName];
    var out = {};
    if(d && d.data && d.data.diff){
      d.data.diff.forEach(function(x){
        var sym = (String(x.f12).charAt(0) === "6" || String(x.f12).charAt(0) === "9" || String(x.f12).charAt(0) === "5") ? "sh" : "sz";
        out[sym + x.f12] = { price: x.f2, prev: x.f18, chg: x.f3, ts: "", name: x.f14 };
      });
    }
    cb(Object.keys(out).length ? out : null);
  };
  loadScript("https://push2.eastmoney.com/api/qt/ulist.np/get?secids=" + secids.join(",") +
             "&fields=f2,f3,f12,f14,f18&fltt=2&invt=2&cb=" + cbName, "utf-8", function(){}, function(){
    delete window[cbName]; cb(null);
  });
}
function poll(){
  pollTencent(function(q){
    if(q){ state.ok = true; state.source = "实时行情"; state.fail = 0; state.last = q; applyQuotes(q); }
    else {
      pollEM(function(q2){
        if(q2){ state.ok = true; state.source = "实时行情(备)"; state.fail = 0; state.last = q2; applyQuotes(q2); }
        else { state.fail++; state.ok = false; if(state.fail === 1) setStatus("行情通道暂时不可用, 显示最近快照"); }
      });
    }
  });
}

/* ---------------- 渲染 ---------------- */
var TAG_COLORS = ["#534AB7", "#0F6E56", "#993C1D", "#185FA5", "#854F0B", "#993556", "#3B6D11", "#A32D2D"];
var TAG_LIST = [];
(function(){ var s = {}; Object.keys(CFG.funds).forEach(function(k){ (CFG.funds[k].tags || []).forEach(function(t){ s[t] = 1; }); }); TAG_LIST = Object.keys(s).sort(); })();
function tagIndex(t){ var i = TAG_LIST.indexOf(t); return i < 0 ? 0 : i; }
function tagColor(t){ return TAG_COLORS[tagIndex(t) % TAG_COLORS.length]; }
function tagBadges(tags){
  if(!tags || !tags.length) return "";
  return tags.map(function(t){ return '<span class="tag-badge tag-click" data-tag="' + t + '" style="color:' + tagColor(t) + ';border-color:' + tagColor(t) + '55">' + t + '</span>'; }).join("");
}
/* 标签介绍浮层(点击标签显示: 含义/特征/注意/参考) */
function showTagInfo(tag){
  var box = document.getElementById("tag-info");
  if(!box) return;
  if(box.getAttribute("data-tag") === tag && box.style.display !== "none"){
    box.style.display = "none";
    return;
  }
  var def = (CFG.tag_defs || {})[tag];
  var b = [];
  b.push('<div class="ti-head"><span class="ti-name" style="color:' + tagColor(tag) + '">', tag, '</span> <span class="src">点击标签可收起</span></div>');
  if(!def){
    b.push('<div class="ti-line">自定义标签：暂无内置说明。可在 <code>config/tag_defs.json</code> 的 tags 中补充该标签的含义/特征/注意事项。</div>');
  } else {
    b.push('<div class="ti-line"><span class="ti-k">含义</span>', def.desc || "—", '</div>');
    if(def.features && def.features.length){
      b.push('<div class="ti-line"><span class="ti-k">特征</span><ul>', def.features.map(function(x){ return '<li>' + x + '</li>'; }).join(""), '</ul></div>');
    }
    if(def.notes && def.notes.length){
      b.push('<div class="ti-line"><span class="ti-k">注意</span><ul>', def.notes.map(function(x){ return '<li>' + x + '</li>'; }).join(""), '</ul></div>');
    }
    if(def.ref && def.ref.length){
      b.push('<div class="ti-line"><span class="ti-k">参考</span>', def.ref.map(function(x){
        var href = x.split(" ").pop();
        var label = x.slice(0, x.lastIndexOf(" "));
        return '<a href="' + href + '" target="_blank" rel="noopener">' + label + '</a>';
      }).join(" &nbsp;·&nbsp; "), '</div>');
    }
  }
  box.innerHTML = b.join("");
  box.style.display = "block";
  box.setAttribute("data-tag", tag);
}
function hideTagInfo(){
  var box = document.getElementById("tag-info");
  if(box) box.style.display = "none";
}
document.addEventListener("click", function(ev){
  var el = ev.target;
  while(el && !(el.classList && el.classList.contains("tag-click"))){ el = el.parentNode; }
  if(el && el.getAttribute("data-tag")){ showTagInfo(el.getAttribute("data-tag")); }
});
function filterBarHTML(){
  var set = {};
  Object.keys(CFG.funds).forEach(function(k){ (CFG.funds[k].tags || []).forEach(function(t){ set[t] = 1; }); });
  var tags = Object.keys(set).sort();
  var btns = ['<button class="ft on" data-t="__all__">全部</button>'];
  btns = btns.concat(tags.map(function(t){
    return '<button class="ft" data-t="' + t + '" style="color:' + tagColor(t) + '">' + t + '</button>';
  }));
  return '<div class="filter-bar" id="filter-bar">' + btns.join("") + '</div>';
}
function applyFilter(tag){
  var shown = 0, total = 0;
  Object.keys(CFG.funds).forEach(function(k){
    var card = document.getElementById("card_" + k);
    if(!card) return;
    total++;
    var show = tag === "__all__" || (CFG.funds[k].tags || []).indexOf(tag) >= 0;
    card.style.display = show ? "" : "none";
    if(show) shown++;
  });
  var bar = document.getElementById("filter-bar");
  if(bar){
    bar.querySelectorAll(".ft").forEach(function(b){
      b.classList.toggle("on", b.getAttribute("data-t") === tag);
    });
  }
  /* 筛选提示条: 让用户知道当前筛选状态, 一键恢复全部 */
  var hint = document.getElementById("filter-hint");
  if(hint){
    if(tag !== "__all__" && shown < total){
      hint.style.display = "block";
      hint.innerHTML = '当前筛选: <b style="color:' + tagColor(tag) + '">' + tag + '</b> · 共 ' + total + ' 只基金, 显示 ' + shown + ' 只 —— 其他基金被隐藏。' +
        '<button class="btn" id="filter-clear" style="margin-left:8px;padding:2px 10px">显示全部</button>';
      var clearBtn = document.getElementById("filter-clear");
      if(clearBtn && !clearBtn._b){
        clearBtn._b = true;
        clearBtn.addEventListener("click", function(){
          applyFilter("__all__");
          var bar2 = document.getElementById("filter-bar");
          if(bar2) bar2.querySelectorAll(".ft").forEach(function(b){ b.classList.toggle("on", b.getAttribute("data-t") === "__all__"); });
        });
      }
    } else {
      hint.style.display = "none";
    }
  }
  try { localStorage.setItem("filter_tag", tag); } catch(e) {}
}
function money(v){
  if(v == null || isNaN(v)) return "—";
  return "¥" + Number(v).toLocaleString("zh-CN", {minimumFractionDigits:2, maximumFractionDigits:2});
}
var POS_DESC = {
  "当前金额(昨收)": "当前持仓市值 = 剩余份额 × 昨日官方净值",
  "持有金额": "当前持仓市值 = 剩余份额 × 当前净值(昨收)",
  "持仓成本": "剩余份额 × 移动加权平均成本单价(不含手续费)",
  "持有收益": "当前市值 − 持仓成本 (未实现盈亏)",
  "累计收益": "已实现收益 + 持仓浮动盈亏 − 累计手续费 (含费净收益)",
  "昨日收益": "剩余份额 × (昨日净值 − 前日净值)",
  "当日预估变化": "按今日实时行情预估的当日涨跌金额 (预估涨跌幅 × 当前市值)",
  "当日预估": "按今日实时行情预估的当日涨跌金额",
  "剩余份额": "累计买入份额 − 累计卖出份额",
  "平均成本净值": "按买入记录移动加权计算的平均成本单价",
  "累计买入": "全部买入记录金额合计(不含手续费)",
  "买入金额": "手动配置的买入总金额",
  "已实现收益": "已卖出部分的盈亏 = Σ(卖出金额 − 卖出份额×当时平均成本)",
  "当前净值(昨收)": "最近一个已发布的官方单位净值",
  "份额": "当前持有份额"
};
function posCell(label, value, cls){
  var desc = POS_DESC[label] || "";
  var t = desc ? ' title="' + desc + '" style="cursor:help"' : '';
  return '<div class="pc"' + t + '><div class="pk">' + label + '</div><div class="pv ' + (cls||"") + '">' + value + '</div></div>';
}
function posHTML(f, key){
  var p = f.position || {};
  if(!p.configured){
    return '<div class="pos pos-empty" id="pos_' + key + '">未持仓 (金额为 0) — 添加买入记录后自动计算持仓与收益</div>';
  }
  var srcTag = p.source === "trades"
    ? '<span class="src" style="background:#eef2ff;color:#4338ca;border-radius:4px;padding:0 6px;font-weight:600">按买卖记录计算</span>'
    : '<span class="src" style="background:#f3f4f6;color:#6b7280;border-radius:4px;padding:0 6px">手动配置</span>';
  var cell = [];
  cell.push(posCell("当前金额(昨收)", money(p.current_amount)));
  cell.push(posCell("持有收益", money(p.hold_gain) + " <span style='color:" + col(p.hold_gain_pct) + "'>(" + (p.hold_gain_pct == null ? "—" : p.hold_gain_pct.toFixed(2) + "%") + ")</span>", col(p.hold_gain)));
  cell.push(posCell("累计收益", money(p.total_gain) + " <span style='color:" + col(p.total_gain_pct) + "'>(" + (p.total_gain_pct == null ? "—" : p.total_gain_pct.toFixed(2) + "%") + ")</span>", col(p.total_gain)));
  cell.push(posCell("昨日收益", money(p.yesterday_gain), col(p.yesterday_gain)));
  cell.push(posCell("当日预估变化", '<span id="today_' + key + '">' + money(p.today_est_change) + '</span>', col(p.today_est_change)));
  if(p.source === "trades"){
    cell.push(posCell("剩余份额", Number(p.shares || 0).toLocaleString("zh-CN", {maximumFractionDigits:2}) + " 份"));
    cell.push(posCell("平均成本净值", p.avg_cost_nav == null ? "—" : Number(p.avg_cost_nav).toFixed(4)));
    cell.push(posCell("累计买入", money(p.buy_amount)));
    cell.push(posCell("已实现收益", money(p.realized_gain), col(p.realized_gain)));
    if(p.dividend_total) cell.push(posCell("累计分红", money(p.dividend_total), col(p.dividend_total)));
    cell.push(posCell("当前净值(昨收)", p.last_nav == null ? "—" : Number(p.last_nav).toFixed(4) + ' <span class="src">' + (p.last_nav_date || "") + '</span>'));
  } else {
    cell.push(posCell("买入金额", money(p.buy_amount)));
    cell.push(posCell("份额", (p.shares == null ? "—" : Number(p.shares).toLocaleString("zh-CN", {maximumFractionDigits:2})) + (p.shares_derived ? " (反推)" : "")));
  }
  var srcNote = p.source === "trades"
    ? '<span class="src">由历史买卖记录(移动加权平均成本)按最新净值计算</span>'
    : (p.shares_derived ? '<span class="src">(份额由买入金额/买入净值反推)</span>' : "");
  return '<div class="pos" id="pos_' + key + '"><div class="pos-title">个人持仓 ' + srcTag + ' ' + srcNote + '</div><div class="pos-grid">' + cell.join("") + '</div></div>';
}
function penSuffix(f){
  if(!f.holdings_penetrated) return "";
  var via = (f.penetrated_via || []).join("、");
  return " · 穿透聚合: 持有 " + via + " 并折算其下层成分股权重(同股票多路径求和)";
}
function cardHTML(f, key){
  var idx = CFG.indices[key] || {};
  var typeBadge = f.type === "ETF" ? '<span class="badge b-etf">场内ETF</span>' : '<span class="badge b-mut">场外混合</span>';
  var rows = f.stocks.map(function(s){
    return '<tr data-code="' + s.sina + '"><td class="nm">' + s.name + '</td><td class="num">' + s.code + '</td>' +
      '<td class="num">' + Number(s.weight).toFixed(2) + '%</td>' +
      '<td class="num px">—</td><td class="num py" style="color:#9ca3af">—</td>' +
      '<td class="num pt">—</td></tr>';
  }).join("");
  var navTabs = [["1m", "近1月"], ["3m", "近3月"], ["6m", "近6月"], ["1y", "近1年"], ["ytd", "今年以来"],
                 ["2y", "近2年"], ["3y", "近3年"], ["5y", "近5年"], ["all", "成立以来"]]
    .map(function(t){ return '<button class="nt" data-range="' + t[0] + '">' + t[1] + '</button>'; }).join("");
  /* 当前基金【直接持仓】结构(基金/股票 + 比例, 标真实/估算) */
  var directHTML = "";
  var direct = (f.holdings_direct || []);
  if(direct.length){
    var ditems = direct.map(function(h){
      var kind = h.type === "FUND" ? '<span class="d-tag d-fund">基金</span>' : '<span class="d-tag d-stock">股票</span>';
      var rtag = h.type === "FUND"
        ? (h.ratio_real ? '<span class="d-real">真实季报</span>' : '<span class="d-est">估算·合同下限</span>')
        : '';
      return '<li>' + kind + ' ' + h.name + ' <span class="d-code">' + h.code + '</span> 权重 <b>' + Number(h.weight).toFixed(2) + '%</b> ' + rtag + '</li>';
    }).join("");
    directHTML = '<div class="direct"><div class="direct-title">当前基金直接持仓</div><ul class="direct-list">' + ditems + '</ul>' +
      (f.holdings_penetrated ? '<div class="direct-note">下方股票 = 穿透聚合后全部成分股权重（按持有比例折算，同股票经多路径持有则权重求和）</div>' : '') + '</div>';
  }
  var body = [
    '<div class="sub" id="sub_' + key + '">' + (f.official_snapshot_desc ? "快照参考: " + f.official_snapshot_desc + " · " : "") + "持仓披露 " + f.quarter + " (" + f.report_date + ")" + penSuffix(f) + '</div>',
    '<div class="base">基线(前一日收盘准确数据): ' + f.baseline_desc + '</div>',
    '<div class="model"><span class="tag">实时模型预估(持仓加权' + (f.coverage == null ? "—" : f.coverage) + '%)</span> <span id="model_' + key + '">—</span>' +
      (idx.name ? ' <span class="src">余量指数[' + idx.name + '] <span id="idx_' + key + '">—</span></span>' : '') + '</div>',
    posHTML(f, key),
    directHTML,
    '<div class="navsec"><div class="nav-tabs" id="navtabs_' + key + '">' + navTabs + '</div><div id="navchart_' + key + '" class="navchart"></div></div>',
    '<div id="chart_' + key + '" class="chart"></div>',
    '<table class="tbl"><thead><tr><th>名称</th><th class="num">代码</th><th class="num">权重(聚合)</th><th class="num">现价</th><th class="num">涨跌幅</th><th class="num">行情时间</th></tr></thead><tbody>' + rows + '</tbody></table>',
    '<div class="note">注: 价格为实时行情, 当日涨跌幅以"昨收"为锚(即前一日收盘准确数据); 可在顶部选择 20s/60s 自动刷新, 或点"手动刷新"更新, 暂停则不再自动刷新</div>'
  ].join("");
  var starOn = starOf(key);
  return '<div class="card" id="card_' + key + '">' +
    '<div class="card-head"><div class="fname"><button class="btn-star' + (starOn ? " on" : "") + '" id="star_' + key + '" title="' + (starOn ? "取消关注" : "关注(优先排前)") + '">' + (starOn ? "★" : "☆") + '</button><span class="fcode">' + key + '</span> ' + f.name + ' ' + typeBadge + tagBadges(f.tags) + '</div>' +
    '<div class="fh-right"><div class="fchg" id="chg_' + key + '" style="color:#9ca3af">—</div>' +
    '<button class="btn btn-fold" id="fold_' + key + '">收起</button></div></div>' +
    '<div id="body_' + key + '">' + body + '</div></div>';
}
/* 折叠: 默认全部展开, 状态记忆在当前浏览器 */
function foldToggle(key){
  var body = document.getElementById("body_" + key);
  var btn = document.getElementById("fold_" + key);
  if(!body || !btn) return;
  var folded = body.style.display === "none";  // 当前是否折叠
  body.style.display = folded ? "" : "none";
  btn.textContent = folded ? "收起" : "展开";
  /* 本次是"展开"操作(折叠->展开)时: 已初始化的图 resize 重绘(折叠时容器宽高为0, 必须resize);
     未初始化的(首屏跳过的)强制补初始化(展开后容器必可见) */
  if(folded){
    var ch = window["navchart_" + key];
    if(ch){ try{ ch.resize(); }catch(e){} }
    else { initNavChart(key, true); }
  }
  try { localStorage.setItem("fold_" + key, folded ? "0" : "1"); } catch(e) {}
}
function restoreFold(key){
  var folded = "0";
  try { folded = localStorage.getItem("fold_" + key) || "0"; } catch(e) {}
  if(folded === "1"){
    var body = document.getElementById("body_" + key);
    var btn = document.getElementById("fold_" + key);
    if(body) body.style.display = "none";
    if(btn) btn.textContent = "展开";
  }
}
/* 历史净值曲线: 范围切换 近1月/近3月/近6月/近1年/今年以来/近2年/近3年/近5年/成立以来
   数据来源: 页面打开时通过 /api/navs/<code> 从数据库异步拉取, 缓存在 window["navdata_"+key] */
function navSeries(key, range){
  var pts = window["navdata_" + key] || [];
  var now = new Date();
  var cutoff = "";
  if(range === "1m"){ var d = new Date(now); d.setMonth(d.getMonth() - 1); cutoff = d.toISOString().slice(0,10); }
  else if(range === "3m"){ var d = new Date(now); d.setMonth(d.getMonth() - 3); cutoff = d.toISOString().slice(0,10); }
  else if(range === "6m"){ var d = new Date(now); d.setMonth(d.getMonth() - 6); cutoff = d.toISOString().slice(0,10); }
  else if(range === "1y"){ var d = new Date(now); d.setFullYear(d.getFullYear() - 1); cutoff = d.toISOString().slice(0,10); }
  else if(range === "2y"){ var d = new Date(now); d.setFullYear(d.getFullYear() - 2); cutoff = d.toISOString().slice(0,10); }
  else if(range === "3y"){ var d = new Date(now); d.setFullYear(d.getFullYear() - 3); cutoff = d.toISOString().slice(0,10); }
  else if(range === "5y"){ var d = new Date(now); d.setFullYear(d.getFullYear() - 5); cutoff = d.toISOString().slice(0,10); }
  else if(range === "ytd"){ cutoff = now.getFullYear() + "-01-01"; }
  if(!cutoff) return pts;
  return pts.filter(function(p){ return p[0] >= cutoff; });
}
/* 交易记录 -> 曲线标点数据: 过滤到所选时间范围内, 返回 [{date, nav, tr}] */
function tradePoints(key, range){
  var trades = CFG.funds[key] ? (CFG.funds[key].trades || []) : [];
  if(!trades.length) return [];
  var pts = navSeries(key, range);
  if(!pts.length) return [];
  var lo = pts[0][0], hi = pts[pts.length - 1][0];
  /* 构建 日期->净值 查找, 供分红标点定位到当日净值(分红记录本身无净值) */
  var navMap = {};
  for(var i=0;i<pts.length;i++) navMap[pts[i][0]] = pts[i][1];
  return trades.filter(function(t){ return t.date >= lo && t.date <= hi; })
    .map(function(t){
      var nav = t.nav;
      if(t.type === "dividend") nav = (navMap[t.date] != null) ? navMap[t.date] : lastNavBefore(navMap, t.date);
      return { date: t.date, nav: nav, type: t.type, amount: t.amount, shares: t.shares, id: t.id };
    });
}
function lastNavBefore(navMap, date){
  var best = null;
  for(var k in navMap){ if(k <= date && (best === null || k > best)) best = k; }
  return best !== null ? navMap[best] : null;
}
function drawNav(key, range){
  var chart = window["navchart_" + key];
  if(!chart) return;
  var navPts = navSeries(key, range);
  var navDates = navPts.map(function(p){ return p[0]; });
  var vals = navPts.map(function(p){ return p[1]; });
  var lo = navDates.length ? navDates[0] : "";
  var hi = navDates.length ? navDates[navDates.length - 1] : "";
  var isFull = !range || range === "全部" || range === "all";
  /* nav 查找表: 交易标点 -> 当日净值(分红无净值, 用 lastNavBefore 兜底) */
  var navMap = {};
  for(var i = 0; i < navDates.length; i++) navMap[navDates[i]] = vals[i];
  /* 交易标点: 买入(红)/卖出(绿)/分红(橙)。早于净值序列的交易在"全部"视图扩展到轴左,
     其余视图夹到边界, 保证在图上可见(提示用户存在更早/更晚交易) */
  var trades = CFG.funds[key] ? (CFG.funds[key].trades || []) : [];
  var buys = [], sells = [], divs = [];
  trades.forEach(function(t){
    var x = t.date, y;
    if(t.type === "dividend"){
      y = navMap[x] != null ? navMap[x] : lastNavBefore(navMap, x);
      if(y == null) y = vals.length ? vals[0] : 0;   // 无历史净值: 用区间起点净值兜底, 确保可见
    } else {
      y = (t.nav && t.nav > 0) ? t.nav : (navMap[x] != null ? navMap[x] : lastNavBefore(navMap, x));
      if(y == null) y = vals.length ? vals[0] : 0;
    }
    if(lo && x < lo) x = lo;
    else if(hi && x > hi) x = hi;
    var pt = [x, y, t];
    if(t.type === "buy") buys.push(pt);
    else if(t.type === "sell") sells.push(pt);
    else divs.push(pt);
  });
  /* 轴: "全部"视图把早于净值序列的交易日期并入(扩展左边界), 使交易落在真实位置 */
  var dates = navDates.slice();
  if(isFull){
    trades.forEach(function(t){ if(t.date < lo && dates.indexOf(t.date) < 0) dates.push(t.date); });
    dates.sort();
  }
  /* NAV 线对齐到扩展后的轴(无净值处为 null, 形成断层而非错位) */
  var navIdx = {}; navDates.forEach(function(d, i){ navIdx[d] = i; });
  var alignedVals = dates.map(function(d){ return navIdx[d] != null ? vals[navIdx[d]] : null; });
  var tp = function(t){
    var title = t.type === "buy" ? "买入" : (t.type === "sell" ? "卖出" : "分红");
    var body = t.type === "dividend"
      ? "金额: " + money(t.amount) + " <span style=\"color:#9ca3af\">(现金分红, 无份额/净值)</span>"
      : "净值: " + Number(t.nav).toFixed(4) + "<br/>份额: " + Number(t.shares).toLocaleString() + "<br/>金额: " + money(t.amount);
    return '<div style="font-weight:600;margin-bottom:4px">' + title + " · " + t.date + "</div>" + body
      + '<div style="color:#9ca3af;font-size:11px;margin-top:3px">点击标点查看详情</div>';
  };
  /* 右侧百分比轴: 以扩展轴首个有净值处为基准 0%, 方便直观看出涨跌百分比 */
  var _nn = alignedVals.filter(function(v){ return v != null; });
  var v0 = _nn.length ? _nn[0] : 0;
  var vmin = _nn.length ? Math.min.apply(null, _nn) : 0;
  var vmax = _nn.length ? Math.max.apply(null, _nn) : 0;
  var pad = (vmax - vmin) * 0.12;
  if(!(pad > 0)) pad = (vmax || 1) * 0.04;
  var lmin = vmin - pad, lmax = vmax + pad;
  var rmin = v0 ? (lmin - v0) / v0 * 100 : 0;
  var rmax = v0 ? (lmax - v0) / v0 * 100 : 0;
  try {
    chart.setOption({
    grid: { left: 8, right: 54, top: 12, bottom: 26, containLabel: true },
    tooltip: { trigger: "axis",
      formatter: function(ps){
        var p = ps[0];
        if(!p) return "";
        var v = p.value;
        var tip = p.axisValue + "<br/>单位净值: " + (v == null ? "—" : Number(v).toFixed(4));
        if(v != null && v0){
          var pct = (v - v0) / v0 * 100;
          tip += "<br/>较起点: " + (pct > 0 ? "+" : "") + pct.toFixed(2) + "%";
        }
        return tip;
      } },
    xAxis: { type: "category", data: dates, boundaryGap: false, axisLabel: { color: "#9ca3af", fontSize: 10 }, axisLine: { lineStyle: { color: "#e5e7eb" } } },
    yAxis: [
      { type: "value", min: lmin, max: lmax, axisLabel: { color: "#6b7280", fontSize: 10 }, splitLine: { lineStyle: { color: "#eef0f3" } } },
      { type: "value", min: rmin, max: rmax, position: "right",
        axisLabel: { fontSize: 10,
          formatter: function(v){ var c = v >= 0 ? "r" : "g"; return "{" + c + "|" + (v > 0 ? "+" : "") + v.toFixed(1) + "%}"; },
          rich: { r: { color: "#e23c39" }, g: { color: "#16a34a" } } },
        axisLine: { show: false }, splitLine: { show: false }, axisTick: { show: false } }
    ],
    dataZoom: [
      { type: "inside", xAxisIndex: 0, start: 0, end: 100, zoomOnMouseWheel: true, moveOnMouseMove: true },
      { type: "slider", xAxisIndex: 0, start: 0, end: 100, height: 14, bottom: 2,
        borderColor: "#e5e7eb", fillerColor: "rgba(24,95,165,0.12)", handleStyle: { color: "#185FA5" } }
    ],
    series: [
      { type: "line", data: alignedVals, symbol: "none", sampling: "lttb", z: 1,
        lineStyle: { color: "#185FA5", width: 2 },
        areaStyle: { color: "rgba(24,95,165,0.08)" } },
      { name: "买入", type: "scatter", data: buys, z: 10,
        symbol: "circle", symbolSize: 9,
        itemStyle: { color: UP, borderColor: "#fff", borderWidth: 1.5 },
        label: { show: true, formatter: "买", position: "top", fontSize: 9, color: UP, fontWeight: 600 },
        tooltip: { trigger: "item", formatter: function(params){ var t = params.data[2]; return t ? tp(t) : ""; } } },
      { name: "卖出", type: "scatter", data: sells, z: 10,
        symbol: "circle", symbolSize: 9,
        itemStyle: { color: DOWN, borderColor: "#fff", borderWidth: 1.5 },
        label: { show: true, formatter: "卖", position: "top", fontSize: 9, color: DOWN, fontWeight: 600 },
        tooltip: { trigger: "item", formatter: function(params){ var t = params.data[2]; return t ? tp(t) : ""; } } },
      { name: "分红", type: "scatter", data: divs, z: 10,
        symbol: "diamond", symbolSize: 11,
        itemStyle: { color: "#b45309", borderColor: "#fff", borderWidth: 1.5 },
        label: { show: true, formatter: "红", position: "top", fontSize: 9, color: "#b45309", fontWeight: 600 },
        tooltip: { trigger: "item", formatter: function(params){ var t = params.data[2]; return t ? tp(t) : ""; } } }
    ],
    }, true);  /* notMerge: 完整替换option, 一并清除"加载中"提示graphic; 规避 $action:remove 删除不存在元素触发 __ec_inner 崩溃 */
    /* 绘制成功: 重置重建计数(防间歇性错误累计导致永久降级) */
    window["navchart_rebuild_" + key] = 0;
  } catch(e) {
    /* echarts 实例内部异常(如 DOM 被破坏/半残实例, __ec_inner 类错误)
       自愈策略: 销毁并重建(最多2次); 仍失败则静态降级——绝不自动整页重载, 否则在受干扰环境会无限刷新 */
    var rc = (window["navchart_rebuild_" + key] || 0) + 1;
    window["navchart_rebuild_" + key] = rc;
    try{ if(chart && chart.dispose) chart.dispose(); }catch(e2){}
    window["navchart_" + key] = null;
    var cel = document.getElementById("navchart_" + key);
    if(cel) cel.innerHTML = "";
    if(rc <= 2){
      /* 延迟到下一轮事件循环, 避免同步重入导致栈溢出或计数失控 */
      setTimeout(function(){ try { initNavChart(key, true); }catch(e3){} }, 0);
    } else {
      /* 多次重建仍失败: 静态降级 + 手动重试按钮(用户主动点击才重建, 不自动循环) */
      showNavFallback(key, "图表渲染遇到问题，请刷新页面或点击重试。");
    }
  }
}
function initNavChart(key, force){
  var el = document.getElementById("navchart_" + key);
  if(!el) return;
  /* 先绑定时间区间 Tab 事件(与图表初始化解耦, 即使图表初始化异常也保证点击可用) */
  var tabs = document.getElementById("navtabs_" + key);
  if(tabs && !tabs._bound){
    tabs._bound = true;
    tabs.querySelectorAll(".nt").forEach(function(b){
      b.addEventListener("click", function(){
        tabs.querySelectorAll(".nt").forEach(function(x){ x.classList.remove("on"); });
        b.classList.add("on");
        drawNav(key, b.getAttribute("data-range"));
      });
    });
    var def = tabs.querySelector(".nt[data-range='3m']");
    if(def) def.classList.add("on");
  }
  /* 已初始化过则跳过(避免重复); 但若实例残缺(echarts加载异常导致), 重置后重新初始化 */
  var old = window["navchart_" + key];
  if(old){
    if(typeof old.setOption === "function") return;
    try{ if(old.dispose) old.dispose(); }catch(e){}
    window["navchart_" + key] = null;
    el.innerHTML = "";
  }
  try {
    var chart = echarts.init(el);
    window["navchart_" + key] = chart;
    window["navchart_pending_" + key] = false;
    /* 点击买卖标点 -> 展示交易详情浮层 */
    chart.on("click", function(params){
      if(params.seriesType === "scatter" && params.data && params.data[2]){
        showTradePop(params.data[2], key);
      }
    });
    drawNav(key, "3m");  /* 默认近3个月 */
    /* 异步从数据库接口拉取历史净值 -> 渲染曲线(数据回来时页面 layout 已完成, resize 确保正常绘制) */
    loadNavData(key);
  } catch(e) {}
}
/* 异步加载某基金历史净值(接口 -> 数据库): 与图表初始化解耦, 无条件发起请求
   数据先缓存到 window["navdata_"+key], 图表就绪后绘制 */
function loadNavData(key){
  /* 幂等: 已有缓存则直接用(图表可能稍后才初始化) */
  if(window["navdata_" + key]){
    var c0 = window["navchart_" + key];
    if(c0) drawNav(key, "3m");
    return;
  }
  /* 防重复: 请求进行中则跳过(否则 build/initNavChart/重建 多处调用会重复请求) */
  if(window["navload_" + key]) return;
  window["navload_" + key] = true;
  var chart = window["navchart_" + key];
  /* 显示"加载中"提示(图表已就绪时, 带id便于后续移除), 数据返回后由 drawNav 移除 */
  if(chart){
    try {
      chart.setOption({ graphic: [{ id: "nav-tip", type: "text", left: "center", top: "middle",
        style: { text: "加载趋势数据…", fill: "#9ca3af", fontSize: 12 } }] });
    } catch(e) {}
  }
  fetch("/api/navs/" + key).then(function(r){ return r.json(); }).then(function(j){
    window["navload_" + key] = false;
    if(j && j.ok && j.data && j.data.length){
      window["navdata_" + key] = j.data;
      var ch = window["navchart_" + key];
      if(ch){
        drawNav(key, "3m");
        setTimeout(function(){ try{ ch.resize(); }catch(e){} }, 0);
      }
    } else {
      drawNavEmpty(key, "无净值数据");
    }
  }).catch(function(){
    window["navload_" + key] = false;
    var fileMode = false;
    try { fileMode = location.protocol === "file:"; } catch(e) {}
    drawNavEmpty(key, fileMode ? "请通过 http://127.0.0.1:8123 打开查看历史曲线" : "未连接服务(8123), 请启动 start_server.bat");
  });
}
function drawNavEmpty(key, msg){
  var el = document.getElementById("navchart_" + key);
  if(!el) return;
  var chart = window["navchart_" + key];
  /* chart 是正常 echarts 实例 -> 用 graphic 文本提示 */
  if(chart && typeof chart.setOption === "function"){
    try {
      chart.setOption({
        grid: { left: 8, right: 16, top: 12, bottom: 26, containLabel: true },
        xAxis: { type: "category", data: [], axisLabel: { color: "#9ca3af" } },
        yAxis: { type: "value", scale: true },
        series: [],
        graphic: [{ id: "nav-tip", type: "text", left: "center", top: "middle", style: { text: msg, fill: "#9ca3af", fontSize: 12 } }]
      });
      return;
    } catch(e) {}
  }
  /* chart 缺失/异常(如 echarts 未正确加载): 用 DOM 提示, 不依赖 echarts */
  el.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:100%;color:#9ca3af;font-size:12px;text-align:center;padding:10px">' + msg + '</div>';
}
/* 图表多次重建仍失败的静态降级: 明确提示 + 手动重试按钮(用户主动点击才重建, 不自动循环) */
function showNavFallback(key, msg){
  var el = document.getElementById("navchart_" + key);
  if(!el) return;
  el.innerHTML =
    '<div style="display:flex;flex-direction:column;align-items:center;justify-content:center;height:100%;color:#9ca3af;font-size:12px;text-align:center;padding:10px;gap:8px">' +
      '<div>' + msg + '</div>' +
      '<div style="color:#6b7280;font-size:11px">可尝试刷新页面后重试。</div>' +
      '<button id="navretry_' + key + '" type="button" style="border:1px solid #185FA5;color:#185FA5;background:#fff;border-radius:6px;padding:4px 14px;font-size:12px;cursor:pointer">重试绘制</button>' +
    '</div>';
  var btn = document.getElementById("navretry_" + key);
  if(btn){
    btn.addEventListener("click", function(){
      window["navchart_rebuild_" + key] = 0;  /* 重置重建计数, 允许用户再次尝试 */
      var c = document.getElementById("navchart_" + key);
      if(c) c.innerHTML = "";
      try { initNavChart(key, true); }catch(e){}
    });
  }
}
/* 点击买卖标点 -> 展示交易详情浮层 */
function showTradePop(t, key){
  var f = CFG.funds[key] || {};
  var pop = document.getElementById("trade-pop");
  if(!pop) return;
  document.getElementById("tp-title").textContent =
    (t.type === "buy" ? "买入" : "卖出") + " · " + f.name + " (" + key + ")";
  document.getElementById("tp-body").innerHTML =
    '<table class="tp-tbl">' +
    '<tr><td>成交日期</td><td>' + t.date + '</td></tr>' +
    '<tr><td>成交净值</td><td>' + Number(t.nav).toFixed(4) + '</td></tr>' +
    '<tr><td>份额</td><td>' + Number(t.shares).toLocaleString() + '</td></tr>' +
    '<tr><td>金额</td><td>' + money(t.amount) + '</td></tr>' +
    '</table>';
  pop.style.display = "flex";
}
function hideTradePop(){ var pop = document.getElementById("trade-pop"); if(pop) pop.style.display = "none"; }
function applyQuotes(q){
  var now = new Date();
  Object.keys(CFG.funds).forEach(function(key){
    var f = CFG.funds[key]; var idx = CFG.indices[key] || {};
    var weighted = 0, wsum = 0, any = false;
    f.stocks.forEach(function(s){
      var d = q[s.sina]; if(!d) return;
      any = true; weighted += s.weight * d.chg; wsum += s.weight;
      var trs = document.querySelectorAll('tr[data-code="' + s.sina + '"]');
      for (var ti = 0; ti < trs.length; ti++){
        var tr = trs[ti];
        tr.querySelector(".px").textContent = d.price;
        var py = tr.querySelector(".py");
        py.textContent = sv(d.chg) + "%"; py.style.color = col(d.chg);
        tr.querySelector(".pt").textContent = fmtTs(d.ts);
      }
    });
    var model = wsum > 0 ? weighted / wsum : null;
    var idxD = q[idx.tencent]; var idxChg = idxD ? idxD.chg : null;
    var adjusted = null;
    if(model != null && idxChg != null && f.coverage != null){
      adjusted = (f.coverage * model + (100 - f.coverage) * idxChg) / 100;
    }
    /* 基金本身实时: 能实时则实时, 不能实时则回退显示最近官方净值(历史) */
    var big = null, badge = "";
    var hist = f.nav_latest || {};
    var histChg = (hist.chg_pct == null || isNaN(hist.chg_pct)) ? null : Number(hist.chg_pct);
    if(f.type === "ETF"){
      var self = f.self_sina ? q[f.self_sina] : null;
      if(self){ big = self.chg; badge = '<span class="rt-badge">实时·场内价 ' + self.price + ' (' + sv(self.chg) + '%) @ ' + fmtTs(self.ts) + '</span>'; }
      else { big = histChg; badge = '<span class="hb-badge">非实时·最近官方净值 ' + (hist.date || "") + ' ' + (histChg == null ? "—" : sv(histChg) + "%") + '</span>'; }
      document.getElementById("chg_" + key).textContent = (big == null ? "—" : sv(big) + "%");
      document.getElementById("chg_" + key).style.color = col(big);
      document.getElementById("sub_" + key).innerHTML = badge + ' · 持仓披露 ' + f.quarter + " (" + f.report_date + ") · 快照参考 " + f.official_snapshot_desc + penSuffix(f);
    } else {
      var anc = f.anchor_tencent ? q[f.anchor_tencent] : null;
      if(anc && anc.chg != null){
        big = anc.chg;
        badge = '<span class="rt-badge">实时·跟踪锚[' + (f.anchor_name || anc.name || "指数/ETF") + '] ' + anc.price + ' (' + sv(anc.chg) + '%) @ ' + fmtTs(anc.ts) + '</span>';
      } else if(model != null){
        big = model;
        badge = '<span class="rt-badge">实时·持仓加权模型 ' + sv(model) + '%</span>';
      } else {
        big = histChg;
        badge = '<span class="hb-badge">非实时·最近官方净值 ' + (hist.date || "") + ' ' + (histChg == null ? "—" : sv(histChg) + "%") + '</span>';
      }
      document.getElementById("chg_" + key).textContent = (big == null ? "—" : sv(big) + "%");
      document.getElementById("chg_" + key).style.color = col(big);
      document.getElementById("sub_" + key).innerHTML = badge + ' · 持仓披露 ' + f.quarter + " (" + f.report_date + ") · 快照参考 " + f.official_snapshot_desc + penSuffix(f);
    }
    document.getElementById("model_" + key).textContent = (model == null ? "—" : sv(model) + "%") + (adjusted != null ? " (修正 " + sv(adjusted) + "%)" : "");
    document.getElementById("model_" + key).style.color = col(adjusted != null ? adjusted : model);
    /* 当日预估变化实时联动: 预估涨跌幅 × 当前金额(昨收) */
    var p = f.position || {};
    var elToday = document.getElementById("today_" + key);
    if(elToday && p.current_amount != null && model != null){
      var amt = p.current_amount * model / 100;
      elToday.textContent = money(amt);
      elToday.style.color = col(amt);
      p.today_est_change = amt;  /* 同步写入, 供金额总览联动 */
    }
    if(idxChg != null){ var el = document.getElementById("idx_" + key); el.textContent = sv(idxChg) + "%"; el.style.color = col(idxChg); }
    updateChart(key, f, q);
  });
  refreshSummary();  /* 金额总览: 当日预估随实时行情联动 */
  setStatus("实时通道: " + state.source + " · " + refreshStateText() + " · 最近更新 " + now.toTimeString().slice(0,8) + " · 市场状态: " + CFG.market_status);
}
function updateChart(key, f, q){
  var chart = window["chart_" + key]; if(!chart) return;
  var sorted = f.stocks.slice().sort(function(a,b){
    var da = q[a.sina], db = q[b.sina]; return (db ? db.chg : -999) - (da ? da.chg : -999);
  });
  var names = [], vals = [], cols = [], tips = {};
  sorted.forEach(function(s){
    var d = q[s.sina];
    names.push(s.name); vals.push(d ? d.chg : null);
    cols.push(d ? col(d.chg) : "#e5e7eb");
    tips[s.name] = "权重 " + Number(s.weight).toFixed(2) + "% · 现价 " + (d ? d.price : "—");
  });
  chart.setOption({
    yAxis: { data: names },
    series: [{ data: vals.map(function(v, i){ return { value: v, itemStyle: { color: cols[i], borderRadius: 2 } }; }) }]
  });
}
function setStatus(msg){
  var el = document.getElementById("status");
  if(el) el.textContent = msg;
}
/* 刷新状态文案: 根据定时器是否实际运行动态显示, 避免暂停时误显示"自动刷新" */
function refreshStateText(){
  if(pollTimer != null){ return "自动刷新 " + (POLL_MS/1000) + "s"; }
  return "已暂停 · 点 20s/60s 或手动刷新更新";
}
function initCharts(){
  Object.keys(CFG.funds).forEach(function(key){
    var f = CFG.funds[key];
    var el = document.getElementById("chart_" + key);
    if(!el) return;
    try {
      var chart = echarts.init(el);
      window["chart_" + key] = chart;
      chart.setOption({
        grid: { left: 8, right: 60, top: 10, bottom: 8, containLabel: true },
        tooltip: { trigger: "axis", axisPointer: { type: "shadow" },
          formatter: function(ps){ var p = ps[0]; return (p.name || "") + "<br/>涨跌幅: " + sv(p.value) + "%"; } },
        xAxis: { type: "value", axisLabel: { formatter: "{value}%", color: "#6b7280" }, splitLine: { lineStyle: { color: "#eef0f3" } } },
        yAxis: { type: "category", data: [], axisLabel: { color: "#374151", fontSize: 11 }, axisLine: { show: false }, axisTick: { show: false } },
        series: [{ type: "bar", barWidth: "62%", label: { show: true, position: "right", formatter: function(p){ return sv(p.value) + "%"; }, color: "#374151", fontSize: 10 }, data: [] }]
      });
    } catch(e) { /* ECharts 加载失败仅影响图表, 不影响实时数字刷新 */ }
  });
  window.addEventListener("resize", function(){ Object.keys(window).forEach(function(k){ if((k.indexOf("chart_") === 0 || k.indexOf("navchart_") === 0) && window[k].resize) window[k].resize(); }); });
}
function corrHTML(){
  var list = CFG.corrections || [];
  if(!list.length){
    return '<div class="corr-empty">暂无修正记录 — 官方净值发布后自动生成(每个交易日: 模型预估 vs 官方实际)</div>';
  }
  var rows = list.map(function(c){
    var nm = c.code === "516670" ? "畜牧ETF" : (c.code === "003095" ? "中欧医疗" : c.code);
    return '<tr><td class="num">' + c.date + '</td><td>' + nm + '</td>' +
      '<td class="num">' + (c.official == null ? "—" : c.official.toFixed(2) + "%") + '</td>' +
      '<td class="num">' + (c.last_model == null ? "—" : c.last_model.toFixed(2) + "%") + '</td>' +
      '<td class="num">' + (c.min_model == null ? "—" : c.min_model.toFixed(2)) + " ~ " + (c.max_model == null ? "—" : c.max_model.toFixed(2)) + '</td>' +
      '<td class="num">' + c.snaps + '</td>' +
      '<td class="num" style="color:' + col(c.bias) + ';font-weight:600">' + (c.bias == null ? "—" : c.bias.toFixed(2) + "%") + '</td></tr>';
  }).join("");
  return '<div class="corr-title">历史修正记录(预估 vs 官方实际 · 留作判断)</div>' +
    '<table class="tbl"><thead><tr><th class="num">交易日</th><th>基金</th><th class="num">官方涨跌幅</th><th class="num">末次模型预估</th><th class="num">当日预估区间</th><th class="num">快照数</th><th class="num">偏差(模型-官方)</th></tr></thead><tbody>' + rows + '</tbody></table>';
}

/* ---------------- 持仓管理(需通过本地服务打开) ---------------- */
function apiPost(url, payload, cb){
  fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) })
    .then(function(r){ return r.json(); })
    .then(function(j){ cb(null, j); })
    .catch(function(e){ cb(e, null); });
}
/* 按钮等待效果: 在按钮内嵌入旋转 spinner + 文字并禁用按钮, 完成后 clearBtnBusy 还原。
   解决"只改按钮文字不够明显、不知何时结束"的问题——旋转动画一眼可知正在处理。 */
function setBtnBusy(btn, text){
  if(!btn) return;
  if(btn._busyOrig === undefined) btn._busyOrig = btn.innerHTML;
  btn.disabled = true;
  btn.classList.add("btn-busy");
  btn.innerHTML = '<span class="btn-spinner"></span>' + (text || "处理中…");
}
function clearBtnBusy(btn, origText){
  if(!btn) return;
  btn.disabled = false;
  btn.classList.remove("btn-busy");
  btn.innerHTML = (origText !== undefined) ? origText : (btn._busyOrig !== undefined ? btn._busyOrig : btn.innerHTML);
  delete btn._busyOrig;
}
function mgrRow(code, name, pos){
  var b = [];
  b.push('<div class="mg-row" data-code="', code, '" draggable="true">');
  b.push('<span class="mg-drag" title="拖拽排序">⠿</span>');
  b.push('<span class="mg-name">', name, ' <span class="src">', code, '</span></span>');
  b.push('<input class="mg-in mg-tags" data-f="tags" placeholder="分类(逗号分隔, 如: 养老金专属,指数基金)" value="', (pos.tags || []).join(","), '" style="flex:1;min-width:160px">');
  b.push('<input class="mg-in" data-f="buy_fee_rate" title="申购费率(百分比, 如0.12=0.12%)" placeholder="申购费率%" value="', (pos.buy_fee_rate ? pos.buy_fee_rate * 100 : ""), '" style="width:66px">');
  b.push('<button class="btn mg-save">保存</button>');
  b.push('<button class="btn mg-del">删除</button></div>');
  return b.join("");
}
function mgrHide(code, btn){
  var msg = document.getElementById("mgr-msg");
  if(btn.getAttribute("data-confirm") === "1"){
    /* 第二步: 确认执行(伪删除: 面板隐藏, 数据保留, 可重新添加恢复) */
    setBtnBusy(btn, "删除中…");
    var row = btn.closest(".mg-row");
    if(row){ row.classList.add("deleting"); row.style.opacity = "0.45"; }
    apiPost("/api/funds/hide", { code: code }, function(err, j){
      if(err || !j || !j.ok){
        msg.textContent = "删除失败: " + ((j && j.message) || err);
        if(row){ row.classList.remove("deleting"); row.style.opacity = ""; }
        clearBtnBusy(btn, "删除"); btn.setAttribute("data-confirm", "0");
      } else {
        /* 乐观移除该行(即使引擎刷新慢, 管理面板也立即不再显示) */
        if(row && row.parentNode) row.parentNode.removeChild(row);
        msg.textContent = "已删除(隐藏) " + code + ", 正在刷新数据...";
        setTimeout(function(){ location.reload(); }, 800);
      }
    });
  } else {
    /* 第一步: 内联确认(不依赖系统弹窗, 预览面板兼容) */
    btn.setAttribute("data-confirm", "1");
    btn.textContent = "确认删除?";
    btn.classList.add("confirming");
    setTimeout(function(){
      if(btn.getAttribute("data-confirm") === "1"){
        btn.setAttribute("data-confirm", "0");
        btn.textContent = "删除";
        btn.classList.remove("confirming");
      }
    }, 4000);
  }
}
function mgrInit(){
  var box = document.getElementById("mgr-funds");
  var msg = document.getElementById("mgr-msg");
  if(!box) return;
  fetch("/api/funds").then(function(r){ return r.json(); }).then(function(j){
    if(!j || !j.ok){ box.innerHTML = '<div class="corr-empty">静态模式: 页面管理需通过本地服务打开。请运行 <code>python live_server.py</code> 后访问 http://127.0.0.1:8123；或直接对话告诉我"加基金/改金额"，由我代为修改。</div>'; return; }
    var funds = j.funds || {};
    var keys = Object.keys(funds).sort(function(a,b){
      /* 面板行与主看板同序: 关注优先 + 手动顺序 */
      var ord = displayOrder();
      var ia = ord.indexOf(a), ib = ord.indexOf(b);
      ia = ia < 0 ? 1e9 : ia; ib = ib < 0 ? 1e9 : ib;
      return ia - ib;
    });
    box.innerHTML = keys.map(function(k){ return mgrRow(k, funds[k].name || k, funds[k] || {}); }).join("");
    /* ① 基金管理: 默认收起, 标题显示基金数, 点击展开/收起 */
    var cnt = document.getElementById("mg-count");
    if(cnt) cnt.textContent = "(" + keys.length + " 只基金)";
    var foldBtn = document.getElementById("mg-fold");
    var mbody = document.getElementById("mgr-body");
    if(foldBtn && mbody){
      var mf = "1";
      try { mf = localStorage.getItem("mgfold") || "1"; } catch(e) {}
      mbody.style.display = mf === "0" ? "" : "none";
      foldBtn.textContent = mf === "0" ? "收起" : "展开";
      if(!foldBtn._b){
        foldBtn._b = true;
        foldBtn.addEventListener("click", function(){
          var folded = mbody.style.display === "none";
          mbody.style.display = folded ? "" : "none";
          foldBtn.textContent = folded ? "收起" : "展开";
          try { localStorage.setItem("mgfold", folded ? "0" : "1"); } catch(e) {}
        });
      }
    }
    box.querySelectorAll(".mg-save").forEach(function(btn){
      btn.addEventListener("click", function(){
        var row = btn.closest(".mg-row");
        var code = row.getAttribute("data-code");
        var payload = { code: code };
        row.querySelectorAll(".mg-in").forEach(function(inp){ payload[inp.getAttribute("data-f")] = inp.value; });
        setBtnBusy(btn, "保存中…");
        apiPost("/api/funds/save", payload, function(err, j){
          msg.textContent = (err || !j || !j.ok) ? "保存失败: " + ((j && j.message) || err) : "已保存, 正在刷新数据...";
          if(j && j.ok) setTimeout(function(){ location.reload(); }, 1500);
          else { clearBtnBusy(btn, "保存"); }
        });
      });
    });
    /* 拖拽排序: 拖动行 -> 插入目标行前 -> 收集顺序保存到本地 -> 同步主看板卡片顺序 */
    var dragCode = null;
    box.querySelectorAll(".mg-row").forEach(function(row){
      row.addEventListener("dragstart", function(e){
        dragCode = row.getAttribute("data-code");
        row.classList.add("dragging");
        try { e.dataTransfer.effectAllowed = "move"; e.dataTransfer.setData("text/plain", dragCode); } catch(e2) {}
      });
      row.addEventListener("dragend", function(){
        row.classList.remove("dragging");
        box.querySelectorAll(".mg-row").forEach(function(r){ r.classList.remove("drag-over"); });
        /* 拖拽结束: 以管理面板当前行为序保存(面板即用户期望顺序) */
        var order = [];
        box.querySelectorAll(".mg-row").forEach(function(r){ order.push(r.getAttribute("data-code")); });
        orderSet(order);
        reorderCards();
        if(msg) msg.textContent = "已保存排序: " + order.join(" → ");
      });
      row.addEventListener("dragover", function(e){
        e.preventDefault();
        try { e.dataTransfer.dropEffect = "move"; } catch(e2) {}
        box.querySelectorAll(".mg-row").forEach(function(r){ r.classList.remove("drag-over"); });
        row.classList.add("drag-over");
      });
      row.addEventListener("drop", function(e){
        e.preventDefault();
        if(!dragCode || dragCode === row.getAttribute("data-code")) return;
        var src = box.querySelector('.mg-row[data-code="' + dragCode + '"]');
        if(!src || src === row) return;
        /* 插入到目标行之前 */
        if(row.parentNode) row.parentNode.insertBefore(src, row);
      });
    });
    var so = document.getElementById("mgr-sort-mode");
    if(so && !so._b){
      so._b = true;
      so.addEventListener("change", function(){
        if(so.checked){
          var order = [];
          box.querySelectorAll(".mg-row").forEach(function(r){ order.push(r.getAttribute("data-code")); });
          orderSet(order);
          reorderCards();
          if(msg) msg.textContent = "已按当前面板顺序排序(手动顺序保存在本地)";
        }
      });
    }
    box.querySelectorAll(".mg-del").forEach(function(btn){
      btn.addEventListener("click", function(){
        mgrHide(btn.closest(".mg-row").getAttribute("data-code"), btn);
      });
    });
    /* 已删除(隐藏)基金: 列表 + 一键恢复 / 彻底删除(从库抹除) */
    var del = j.deleted || {};
    var delBox = document.getElementById("mgr-deleted");
    if(delBox){
      var delKeys = Object.keys(del);
      if(!delKeys.length){
        delBox.style.display = "none";
      } else {
        delBox.style.display = "";
        var dfold = "1";
        try { dfold = localStorage.getItem("mgrdelfold") || "1"; } catch(e){}
        delBox.innerHTML =
          '<div class="mgr-sec-title">已删除基金(数据保留, 可恢复)' +
            '<button class="btn" id="mg-del-fold">' + (dfold === "1" ? "展开" : "收起") + '</button> <span class="src" id="mg-del-count"></span></div>' +
          '<div id="mgr-deleted-body" style="display:' + (dfold === "1" ? "none" : "") + '">' +
          '<div class="corr-empty" style="margin:2px 0 6px;font-size:11.5px">已删除基金仍在数据库保留历史净值, 恢复后自动补数据到最新; 也可"彻底删除"从库里抹除(基础信息 + 买卖记录 + 历史净值), 后期重新添加走全新加载流程。</div>' +
          delKeys.map(function(k){
            return '<div class="mg-row" data-code="' + k + '">' +
              '<span class="mg-name" style="opacity:.65">' + (del[k].name || k) + ' <span class="src">' + k + '</span></span>' +
              '<button class="btn mg-restore" data-code="' + k + '">恢复</button>' +
              '<button class="btn mg-purge" data-code="' + k + '">彻底删除</button></div>';
          }).join("") +
          '</div>';
        var dcount = document.getElementById("mg-del-count");
        if(dcount) dcount.textContent = "(" + delKeys.length + " 只)";
        var dfb = document.getElementById("mg-del-fold");
        if(dfb) dfb.addEventListener("click", function(){
          var b = document.getElementById("mgr-deleted-body");
          if(!b) return;
          var folded = b.style.display === "none";
          b.style.display = folded ? "" : "none";
          dfb.textContent = folded ? "收起" : "展开";
          try { localStorage.setItem("mgrdelfold", folded ? "0" : "1"); } catch(e){}
        });
        delBox.querySelectorAll(".mg-restore").forEach(function(btn){
          btn.addEventListener("click", function(){
            var code = btn.getAttribute("data-code");
            setBtnBusy(btn, "恢复中…");
            apiPost("/api/funds/unhide", { code: code }, function(err, j){
              if(err || !j || !j.ok){
                msg.textContent = "恢复失败: " + ((j && j.message) || err);
                clearBtnBusy(btn, "恢复");
              } else {
                msg.textContent = "已恢复 " + code + ", 正在重新抓取数据并刷新看板...";
                setTimeout(function(){ location.reload(); }, 1200);
              }
            });
          });
        });
        delBox.querySelectorAll(".mg-purge").forEach(function(btn){
          btn.addEventListener("click", function(){
            var code = btn.getAttribute("data-code");
            if(btn.getAttribute("data-confirm") === "1"){
              setBtnBusy(btn, "删除中…");
              apiPost("/api/funds/purge", { code: code }, function(err, j){
                if(err || !j || !j.ok){
                  msg.textContent = "彻底删除失败: " + ((j && j.message) || err);
                  clearBtnBusy(btn, "彻底删除"); btn.removeAttribute("data-confirm");
                } else {
                  msg.textContent = "已彻底删除 " + code + " 的全部数据, 正在刷新...";
                  setTimeout(function(){ location.reload(); }, 1000);
                }
              });
            } else {
              btn.setAttribute("data-confirm", "1");
              btn.textContent = "确认删除?";
              btn.classList.add("confirming");
              setTimeout(function(){ if(btn.getAttribute("data-confirm") === "1"){ btn.removeAttribute("data-confirm"); btn.textContent = "彻底删除"; btn.classList.remove("confirming"); } }, 4000);
            }
          });
        });
      }
    }
    trInit();
  }).catch(function(){ box.innerHTML = '<div class="corr-empty">静态模式: 页面管理需通过本地服务打开 (python live_server.py)。也可直接对话让我修改。</div>'; });
}
/* ---- 交易记录(买入/卖出) 管理: 按基金缩进分组, 每组可添加/删除 ---- */
function trInit(){
  renderTrList();
}
/* 添加/删除交易后: 仅局部刷新该基金的交易面板(交易列表+持仓汇总), 不再整页 reload,
   避免重新下载 1.2MB 页面 + 重初始化所有图表 + 重新触发每日同步遮罩(10~30秒) */
function refreshTradeFund(code){
  if(!CFG.funds[code]){ location.reload(); return; }
  fetch("/api/funds/state?code=" + encodeURIComponent(code)).then(function(r){ return r.json(); }).then(function(j){
    if(j && j.ok && j.fund){
      var f = CFG.funds[code];
      f.trades = j.fund.trades || [];
      f.position = j.fund.position || {};
      f.trade_summary = j.fund.trade_summary || {};
    }
    renderTrList();            // 重绘交易面板(纯表格, 不涉及图表)
    refreshCardValues(code);   // 轻量刷新: 只更新该卡片持仓数值, 不重建 navchart_/chart_ 图表
  }).catch(function(){ location.reload(); });
}
/* 轻量刷新: 仅更新某只基金卡片上的"个人持仓"数值(持有/累计收益、当前金额、剩余份额等),
   不重建 navchart_/chart_ 两个 ECharts 实例。用于交易增删后跟新卡片, 避免整页 reload。
   CFG.funds[code].position 由调用方(refreshTradeFund)从 /api/funds/state 拉取后已更新。 */
function refreshCardValues(code){
  var f = CFG.funds[code];
  if(!f) return;
  /* 仅替换 pos 区块 DOM(纯数值展示, 不含任何图表容器), 图表实例保持不动 */
  var host = document.getElementById("pos_" + code);
  if(host){
    var wrap = document.createElement("div");
    wrap.innerHTML = posHTML(f, code);
    var node = wrap.firstElementChild;
    if(node && host.parentNode){ host.parentNode.replaceChild(node, host); }
  }
  /* 金额总览: 现有金额/累计收益总计随持仓变化联动(只更新数值, 不重建DOM) */
  refreshSummary();
  /* 总览明细行: 同步该基金"现有金额"(children[1])与"累计收益"(children[2])两列(当日预估列由 refreshSummary 负责) */
  var tr = document.querySelector('tr[data-fkey="' + code + '"]');
  if(tr){
    var p = f.position || {};
    if(tr.children[1]) tr.children[1].textContent = money(p.current_amount);
    if(tr.children[2]){
      tr.children[2].textContent = money(p.total_gain) + (p.total_gain_pct == null ? "" : " (" + sv(p.total_gain_pct) + "%)");
      tr.children[2].style.color = col(p.total_gain);
    }
  }
}
function renderTrList(){
  var box = document.getElementById("tr-list");
  if(!box) return;
  var html = Object.keys(CFG.funds).map(function(code){
    var f = CFG.funds[code];
    var trades = f.trades || [];
    var p = f.position || {};          // 与基金卡片同一份持仓计算结果(优先按交易记录, 回退手动配置)
    var sum = f.trade_summary || {};   // 原始买卖聚合(累计买入/卖出/剩余份)
    /* 默认收起(避免页面过长), 展开状态记忆在当前浏览器 */
    var folded = "1";
    try { folded = localStorage.getItem("trfold_" + code) || "1"; } catch(e) {}
    /* 基金级折叠(默认展开: 显示添加行; 记录级默认收起) */
    var gfold = "0";
    try { gfold = localStorage.getItem("trgfold_" + code) || "0"; } catch(e) {}
    var rows = trades.slice().reverse().map(function(t){
      return '<tr><td>' + (t.type === "buy" ? '<span style="color:' + UP + ';font-weight:600">买入</span>' : t.type === "sell" ? (t.clear ? '<span style="color:#b45309;font-weight:600">清仓</span>' : '<span style="color:' + DOWN + ';font-weight:600">卖出</span>') : '<span style="color:#b45309;font-weight:600">分红</span>') + '</td>' +
        '<td class="num">' + t.date + '</td>' +
        '<td class="num">' + (t.type === "dividend" ? "—" : Number(t.nav).toFixed(4)) + '</td>' +
        '<td class="num">' + (t.type === "dividend" ? "—" : Number(t.shares).toLocaleString()) + '</td>' +
        '<td class="num" title="成交金额(不含手续费)">' + money(t.amount) + '</td>' +
        '<td class="num" title="' + (t.type === "dividend" ? "分红无手续费" : "该笔手续费: 买入按申购费率自动算, 卖出可手填(默认0)") + '">' + (t.type === "dividend" ? "—" : money(t.fee || 0)) + '</td>' +
        '<td><button class="btn tp-del" data-id="' + t.id + '">删除</button></td></tr>';
    }).join("");
    /* 持仓汇总: 直接复用基金卡片同一份 position, 让"交易记录"与"持仓"数据串起来(持有金额不再为空) */
    var srcLabel = p.source === "trades" ? "买卖记录" : "手动配置";
    var holdBlock =
      '<div class="tr-hold">' +
        '<div class="tr-hold-src">持仓来源: <b>' + srcLabel + '</b> · 剩余 ' +
          Number(p.shares || 0).toLocaleString() + ' 份 · 当前净值 ' + (p.last_nav != null ? Number(p.last_nav).toFixed(4) : "—") + '</div>' +
        '<div class="tr-hold-grid">' +
          posCell("持有金额", money(p.current_amount)) +
          posCell("持仓成本", money(p.cost_amount)) +
          posCell("持有收益", money(p.hold_gain), col(p.hold_gain)) +
          posCell("累计收益", money(p.total_gain) + ' <span style="font-size:11px">(' + (p.total_gain_pct == null ? "—" : p.total_gain_pct.toFixed(2) + "%") + ')</span>', col(p.total_gain)) +
          posCell("已实现收益", money(p.realized_gain), col(p.realized_gain)) +
          (p.dividend_total ? posCell("累计分红", money(p.dividend_total), col(p.dividend_total)) : "") +
          posCell("当日预估", money(p.today_est_change), col(p.today_est_change)) +
        '</div>' +
      '</div>';
    var aggLine = '<div class="src" style="margin:2px 0 4px">累计买入 ' + money(sum.buy_amount) + ' / ' +
      Number(sum.buy_shares || 0).toLocaleString() + '份 · 累计卖出 ' + money(sum.sell_amount) + ' / ' +
      Number(sum.sell_shares || 0).toLocaleString() + '份' +
      (sum.dividend_amount ? ' · <b style="color:#b45309">累计分红 ' + money(sum.dividend_amount) + '</b>' : '') + '</div>';
    var listHtml = trades.length
      ? holdBlock + aggLine + '<table class="tbl"><thead><tr><th>类型</th><th class="num">日期</th><th class="num">净值</th><th class="num">份额</th><th class="num">金额</th><th class="num">手续费</th><th></th></tr></thead><tbody>' + rows + '</tbody></table>'
      : holdBlock + aggLine + '<div class="corr-empty" style="margin:4px 0 2px">暂无交易记录 — 在上方添加买入/卖出/分红 (持仓金额将按记录自动计算)</div>';
    return '<div class="tr-group" data-code="' + code + '">' +
      '<div class="tr-group-head">' +
      '<button class="btn tr-gfold" data-code="' + code + '">' + (gfold === "1" ? "展开基金" : "收起基金") + '</button>' +
      '<button class="btn tr-fold" data-code="' + code + '">' + (folded === "1" ? "展开记录" : "收起记录") + '</button>' +
      '<b>' + f.name + '</b> <span class="src">' + code + '</span>' +
      '<span class="src" style="color:' + (p.shares > 0 ? "#047857" : "#9ca3af") + '">' + (p.shares > 0 ? "持仓中" : "未持仓") + '</span>' +
      '<span class="src">' + trades.length + ' 笔交易</span></div>' +
      '<div class="tr-body" style="display:' + (gfold === "1" ? "none" : "") + '">' +
      '<div class="tr-add-row">' +
      '<select class="mg-in tr-type" style="width:74px"><option value="buy">买入</option><option value="sell">卖出</option><option value="dividend">分红</option></select>' +
      '<input class="mg-in tr-date" type="date" title="选择成交日期(自动带出该日历史净值)" style="width:136px">' +
      '<input class="mg-in tr-nav" type="number" step="0.0001" placeholder="净值(自动)" style="width:88px" title="选择日期后自动填充, 可手改">' +
      '<input class="mg-in tr-amount" type="number" step="0.01" placeholder="金额*" style="width:108px">' +
      '<input class="mg-in tr-shares" type="number" step="0.0001" placeholder="份额(自动)" readonly style="width:102px" title="买入填金额自动算份额; 卖出切换为填份额自动算金额">' +
      '<input class="mg-in tr-fee" type="number" step="0.01" placeholder="手续费" style="width:78px" title="按基金申购/赎回费率自动计算, 可手改">' +
      '<label class="tr-clear-wrap" style="display:none;margin-left:6px;font-size:12px;color:#b45309;white-space:nowrap"><input type="checkbox" class="tr-clear"> 清仓(卖出全部剩余)</label>' +
      '<button class="btn tr-add2">添加</button>' +
      '<span class="tr-div-hint" style="margin-left:8px;color:#6b7280;font-size:12px"></span></div>' +
      '<div class="tr-records" style="display:' + (folded === "1" ? "none" : "") + '">' + listHtml + '</div>' +
      '</div></div>';
  }).join("");
  box.innerHTML = html;
  /* 基金级折叠: 收起/展开整只基金(含添加行与记录) */
  box.querySelectorAll(".tr-gfold").forEach(function(b){
    if(b._b) return; b._b = true;
    b.addEventListener("click", function(){
      var code = b.getAttribute("data-code");
      var bodyEl = b.closest(".tr-group").querySelector(".tr-body");
      var folded = bodyEl.style.display === "none";
      bodyEl.style.display = folded ? "" : "none";
      b.textContent = folded ? "收起基金" : "展开基金";
      try { localStorage.setItem("trgfold_" + code, folded ? "0" : "1"); } catch(e) {}
    });
  });
  /* 记录级折叠: 仅收起/展开买卖记录表(添加行始终可见) */
  box.querySelectorAll(".tr-fold").forEach(function(b){
    if(b._b) return; b._b = true;
    b.addEventListener("click", function(){
      var code = b.getAttribute("data-code");
      var recEl = b.closest(".tr-group").querySelector(".tr-records");
      var folded = recEl.style.display === "none";
      recEl.style.display = folded ? "" : "none";
      b.textContent = folded ? "收起记录" : "展开记录";
      try { localStorage.setItem("trfold_" + code, folded ? "0" : "1"); } catch(e) {}
    });
  });
  /* 每基金"添加"按钮 */
  box.querySelectorAll(".tr-add2").forEach(function(btn){
    if(btn._b) return; btn._b = true;
    btn.addEventListener("click", function(){
      var msg = document.getElementById("mgr-msg");
      var group = btn.closest(".tr-group");
      var code = group.getAttribute("data-code");
      var type = group.querySelector(".tr-type").value;
      var nav = group.querySelector(".tr-nav").value;
      var amount = group.querySelector(".tr-amount").value;
      var shares = group.querySelector(".tr-shares").value;
      var date = group.querySelector(".tr-date").value;
      if(!date){ msg.textContent = "请选择" + (type === "dividend" ? "分红" : "成交") + "日期"; return; }
      var clearChk = group.querySelector(".tr-clear");
      var isClear = (type === "sell" && clearChk && clearChk.checked);
      var payload;
      if(type === "dividend"){
        if(!amount){ msg.textContent = "请填写分红金额"; return; }
        payload = { code: code, type: type, amount: amount, date: date };
      } else if(isClear){
        /* 清仓: 卖出当前全部剩余份额(消除四舍五入碎片残余, 剩余精确归0) */
        var rem = (CFG.funds[code] && CFG.funds[code].position && CFG.funds[code].position.shares) || 0;
        rem = Math.max(0, parseFloat(rem) || 0);
        if(rem <= 0){ msg.textContent = "当前无剩余份额可清仓(已是空仓)"; return; }
        shares = rem.toFixed(4);
        amount = (rem * parseFloat(nav)).toFixed(2);
        var feeC = group.querySelector(".tr-fee").value;
        payload = { code: code, type: "sell", amount: amount, shares: shares, nav: nav, date: date, fee: feeC, clear: true };
      } else {
        if(!nav){ msg.textContent = "请选择成交日期(自动带净值)或手填净值"; return; }
        if(!amount && !shares){ msg.textContent = "请填写金额或份额(另一个自动计算)"; return; }
        if(!amount && shares) amount = (parseFloat(shares) * parseFloat(nav)).toFixed(2);
        if(!shares && amount) shares = (parseFloat(amount) / parseFloat(nav)).toFixed(4);
        var fee = group.querySelector(".tr-fee").value;
        payload = { code: code, type: type, amount: amount, shares: shares, nav: nav, date: date, fee: fee };
      }
      setBtnBusy(btn, "添加中…");
      apiPost("/api/trades/add", payload, function(err, j){
        msg.textContent = (err || !j || !j.ok) ? "添加失败: " + ((j && j.message) || err) : "已添加交易, 正在刷新…";
        if(j && j.ok) setTimeout(function(){ refreshTradeFund(code); }, 300);
        else { clearBtnBusy(btn, "添加"); }
      });
    });
  });
  function findNavAt(arr, date){
    if(!arr || !arr.length) return null;
    var prev = null, last = null;
    for(var i=0;i<arr.length;i++){
      var dd = arr[i][0], v = arr[i][1];
      last = v;
      if(dd === date) return v;          /* 精确匹配该日 */
      if(dd < date) prev = v;            /* 该日之前最近的一条 */
    }
    return prev !== null ? prev : last;  /* 优先该日前最近, 否则最早可用 */
  }
  /* 截至某日期的累计持有份额(买+卖-), 用于分红行展示"当时持有"参考 */
  function heldSharesAt(code, date){
    var trs = (CFG.funds[code] && CFG.funds[code].trades) || [];
    var sh = 0;
    for(var i = 0; i < trs.length; i++){
      var t = trs[i];
      if(t.date > date) continue;
      if(t.type === "buy") sh += (t.shares || 0);
      else if(t.type === "sell") sh -= (t.shares || 0);
    }
    return sh;
  }
  function trNavFor(code, date, cb){
    var d = window["navdata_" + code];
    if(d && d.length){ cb(findNavAt(d, date)); return; }
    fetch("/api/navs/" + code).then(function(r){ return r.json(); }).then(function(j){
      var arr = (j && j.ok && j.data) ? j.data : [];
      window["navdata_" + code] = arr;
      cb(findNavAt(arr, date));
    }).catch(function(){ cb(null); });
  }
  /* 日期选择 -> 自动带出该日历史净值(精确; 无则取该日前最近) */
  box.querySelectorAll(".tr-date").forEach(function(dt){
    if(dt._b) return; dt._b = true;
    dt.addEventListener("change", function(){
      var msg = document.getElementById("mgr-msg");
      var group = dt.closest(".tr-group");
      var code = group.getAttribute("data-code");
      var date = dt.value;
      if(!date) return;
      var navEl = group.querySelector(".tr-nav");
      var amountEl = group.querySelector(".tr-amount");
      var sharesEl = group.querySelector(".tr-shares");
      var type = group.querySelector(".tr-type").value;
      trNavFor(code, date, function(nv){
        if(type === "dividend"){
          /* 分红无净值/份额: 仅展示该日净值(若有历史)与当时持有份额作为参考 */
          var hint = group.querySelector(".tr-div-hint");
          var hs = heldSharesAt(code, date);
          var navTxt = (nv === null || nv === undefined) ? "无历史净值" : ("该日净值 " + Number(nv).toFixed(4));
          var hsTxt = (hs && hs > 0) ? (" · 当时持有 " + Number(hs).toLocaleString() + " 份") : "";
          if(hint) hint.textContent = navTxt + hsTxt;
          navEl.value = "";
          return;
        }
        if(nv === null || nv === undefined){
          navEl.value = ""; if(msg) msg.textContent = "该日期无历史净值, 请手填净值或换日期";
        } else {
          navEl.value = Number(nv).toFixed(4);
          if(amountEl.value && !sharesEl.value) sharesEl.value = (parseFloat(amountEl.value)/nv).toFixed(4);
          if(sharesEl.value && !amountEl.value) amountEl.value = (parseFloat(sharesEl.value)*nv).toFixed(2);
          if(msg) msg.textContent = "";
          trCalcFee(dt.closest(".tr-group"));
        }
      });
    });
  });
  /* 金额/份额联动: 只填一个, 另一个按当前净值自动算 */
  box.querySelectorAll(".tr-amount").forEach(function(a){
    if(a._b) return; a._b = true;
    a.addEventListener("input", function(){
      var g = a.closest(".tr-group");
      var nav = g.querySelector(".tr-nav").value, sh = g.querySelector(".tr-shares");
      if(nav && a.value) sh.value = (parseFloat(a.value)/parseFloat(nav)).toFixed(4);
      else if(!a.value) sh.value = "";
      trCalcFee(g);
    });
  });
  box.querySelectorAll(".tr-shares").forEach(function(s){
    if(s._b) return; s._b = true;
    s.addEventListener("input", function(){
      var g = s.closest(".tr-group");
      var nav = g.querySelector(".tr-nav").value, am = g.querySelector(".tr-amount");
      if(nav && s.value) am.value = (parseFloat(s.value)*parseFloat(nav)).toFixed(2);
      else if(!s.value) am.value = "";
      trCalcFee(g);
    });
  });
  /* 按基金费率自动算手续费: buy=金额×申购费率, sell=金额×赎回费率 (金额为空则清空) */
  function trCalcFee(g){
    var code = g.getAttribute("data-code");
    var fcfg = CFG.funds[code] || {};
    var type = g.querySelector(".tr-type").value;
    var am = g.querySelector(".tr-amount").value;
    var rate = (type === "sell" ? (fcfg.sell_fee_rate || 0) : (fcfg.buy_fee_rate || 0));
    var feeEl = g.querySelector(".tr-fee");
    if(am && rate) feeEl.value = (parseFloat(am) * rate).toFixed(2);   /* 金额+费率 -> 自动算(买入) */
    else feeEl.value = "0.00";                                         /* 买入未填金额/卖出费率0 -> 默认0, 可手改 */
  }
  /* 交易类型切换: buy=填金额算份额, sell=填份额算金额, dividend=只填金额(无份额/净值/手续费) */
  function trSetMode(g, mode){
    var am = g.querySelector(".tr-amount"), sh = g.querySelector(".tr-shares"), feeEl = g.querySelector(".tr-fee"), navEl = g.querySelector(".tr-nav");
    var hint = g.querySelector(".tr-div-hint"); if(hint) hint.textContent = "";  /* 切换类型时清空分红参考提示 */
    if(mode === "dividend"){
      am.removeAttribute("readonly"); am.style.background = ""; am.placeholder = "分红金额*";
      sh.setAttribute("readonly", "readonly"); sh.style.background = "#f3f4f6"; sh.placeholder = "—";
      navEl.setAttribute("readonly", "readonly"); navEl.style.background = "#f3f4f6"; navEl.placeholder = "—"; navEl.value = "";
      feeEl.style.display = "none";
      return;
    }
    if(mode === "sell"){
      am.setAttribute("readonly", "readonly"); am.style.background = "#f3f4f6"; am.placeholder = "金额(自动)";
      sh.removeAttribute("readonly"); sh.style.background = ""; sh.placeholder = "份额*";
      feeEl.style.display = ""; feeEl.removeAttribute("readonly"); feeEl.style.background = "";
      var cw = g.querySelector(".tr-clear-wrap"); if(cw) cw.style.display = "";
      var cchk = g.querySelector(".tr-clear"); if(cchk){ cchk.checked = false; sh.removeAttribute("readonly"); sh.style.background = ""; sh.placeholder = "份额*"; }
    } else {
      sh.setAttribute("readonly", "readonly"); sh.style.background = "#f3f4f6"; sh.placeholder = "份额(自动)";
      am.removeAttribute("readonly"); am.style.background = ""; am.placeholder = "金额*";
      feeEl.style.display = "none";  /* 买入无需手续费输入框: 按基金申购费率自动算 */
    }
    navEl.removeAttribute("readonly"); navEl.style.background = ""; navEl.placeholder = "净值(自动)";
    trCalcFee(g);
  }
  box.querySelectorAll(".tr-type").forEach(function(sel){
    if(sel._b) return; sel._b = true;
    sel.addEventListener("change", function(){
      trSetMode(sel.closest(".tr-group"), sel.value);
    });
  });
  /* 清仓勾选: 自动填入当前剩余份额并禁用份额框(避免手填产生碎片残余) */
  box.querySelectorAll(".tr-clear").forEach(function(c){
    if(c._b) return; c._b = true;
    c.addEventListener("change", function(){
      var g = c.closest(".tr-group");
      var sh = g.querySelector(".tr-shares");
      if(c.checked){
        var code = g.getAttribute("data-code");
        var rem = (CFG.funds[code] && CFG.funds[code].position && CFG.funds[code].position.shares) || 0;
        rem = Math.max(0, parseFloat(rem) || 0);
        sh.value = rem > 0 ? rem.toFixed(4) : "";
        sh.setAttribute("readonly", "readonly"); sh.style.background = "#f3f4f6";
      } else {
        sh.removeAttribute("readonly"); sh.style.background = ""; sh.value = "";
      }
    });
  });
  /* 自动只读框聚焦时可手改(移除只读, 手改后自动联动另一框) */
  box.querySelectorAll(".tr-amount, .tr-shares").forEach(function(inp){
    if(inp._r) return; inp._r = true;
    inp.addEventListener("focus", function(){
      if(inp.hasAttribute("readonly")){
        inp.removeAttribute("readonly"); inp.style.background = "";
        inp.placeholder = inp.classList.contains("tr-amount") ? "金额*" : "份额*";
      }
    });
  });
  /* 初始: 买入模式(填金额算份额, 份额自动只读) */
  box.querySelectorAll(".tr-group").forEach(function(g){ trSetMode(g, "buy"); });
  /* 删除按钮(内联二次确认) */
  box.querySelectorAll(".tp-del").forEach(function(b){
    b.addEventListener("click", function(){
      var msg = document.getElementById("mgr-msg");
      var id = b.getAttribute("data-id");
      if(b.getAttribute("data-confirm") === "1"){
        setBtnBusy(b, "删除中…");
        apiPost("/api/trades/del", { id: id }, function(err, j){
          msg.textContent = (err || !j || !j.ok) ? "删除失败: " + ((j && j.message) || err) : "已删除交易记录, 正在刷新…";
          if(j && j.ok){ var _c = b.closest(".tr-group"); _c = _c && _c.getAttribute("data-code"); setTimeout(function(){ refreshTradeFund(_c); }, 300); }
          else { clearBtnBusy(b, "删除"); b.removeAttribute("data-confirm"); }
        });
      } else {
        b.setAttribute("data-confirm", "1"); b.textContent = "确认删除?";
        setTimeout(function(){ if(b.getAttribute("data-confirm") === "1"){ b.textContent = "删除"; b.removeAttribute("data-confirm"); } }, 4000);
      }
    });
  });
}
function mgrAdd(){
  var msg = document.getElementById("mgr-msg");
  var payload = {
    code: (document.getElementById("mg-code").value || "").trim(),
    name: (document.getElementById("mg-name").value || "").trim(),
    anchor: (document.getElementById("mg-anchor").value || "").trim()
  };
  var btn = document.getElementById("mg-add-btn");
  if(!payload.code){ msg.textContent = "请输入基金编码(6位, 如 022982)"; return; }
  setBtnBusy(btn, "添加中…");
  apiPost("/api/funds/add", payload, function(err, j){
    if(j && j.ok){
      /* 添加完成后刷新整页(重新生成看板 + 引擎已抓取新基金数据), 确保主看板立即可见新基金 */
      msg.innerHTML = '<span style="color:#16a34a">✓ 已添加 ' + payload.code + (payload.name ? ' (' + payload.name + ')' : '') + '，正在刷新页面…</span>';
      clearBtnBusy(btn, "添加基金");
      setTimeout(function(){ location.reload(); }, 900);
    } else {
      clearBtnBusy(btn, "添加基金");
      msg.textContent = "添加失败: " + ((j && j.message) || err);
    }
  });
}
/* 面板内做过增删改(dirty)后, 关闭面板时一次性整页刷新, 让主看板同步新基金/新数据; 纯浏览关闭不刷新 */
var mgrDirty = false;
function mgrToggle(){
  var m = document.getElementById("mgr");
  if(m.style.display === "none"){ m.style.display = "block"; mgrInit(); }
  else {
    m.style.display = "none";
    if(mgrDirty){ mgrDirty = false; location.reload(); }
  }
}
/* 打开页面即同步: 每日只自动执行一次昨日/历史检查补充
   同步期间显示全屏覆盖层, 同步完成自动进入主页面; 失败可"重试"或"直接进入" */
function showSyncOverlay(){
  var o = document.getElementById("sync-overlay");
  if(!o) return;
  o.classList.remove("fail");
  o.classList.add("show");
  document.getElementById("sync-title").textContent = "正在同步最新数据…";
  document.getElementById("sync-sub").textContent = "检查昨日官方净值 / 补全历史记录, 请稍候（约需 10~30 秒）";
  document.getElementById("sync-btns").style.display = "none";
}
function hideSyncOverlay(){
  var o = document.getElementById("sync-overlay");
  if(o) o.classList.remove("show");
}
function showSyncFail(msg){
  var o = document.getElementById("sync-overlay");
  if(!o) return;
  o.classList.add("fail");
  document.getElementById("sync-title").textContent = "数据同步失败";
  document.getElementById("sync-sub").textContent = (msg || "接口暂时不可用") + "。可点击\"重试同步\"再试，或\"直接进入\"查看最近数据（行情仍实时刷新）。";
  document.getElementById("sync-btns").style.display = "flex";
  var retry = document.getElementById("sync-retry");
  var enter = document.getElementById("sync-enter");
  if(retry && !retry._b){ retry._b = true; retry.addEventListener("click", function(){ doSync(true); }); }
  if(enter && !enter._b){ enter._b = true; enter.addEventListener("click", function(){ hideSyncOverlay(); }); }
  setStatus("数据同步失败, 当前显示最近一次数据");
}
function doSync(force){
  if(force){ syncRun(true); return; }  // 手动重试: 直接执行, 不先 check
  /* 先轻量 check: 无需同步 -> 直接进入(零等待零闪烁); 需要 -> 显示覆盖层再执行 */
  fetch("/api/sync/check").then(function(r){ return r.json(); }).then(function(j){
    if(j && j.ok && j.need){
      syncRun(false);
    } else {
      /* 今日已同步/无需同步/静态模式 -> 直接进入主页面 */
      if(j && j.message) setStatus(j.message);
    }
  }).catch(function(){ /* 静态模式(无服务) -> 直接进入 */ });
}
function syncRun(force){
  showSyncOverlay();  // 同步期间遮挡主页面, 完成后才进入
  fetch("/api/sync" + (force ? "?force=1" : "")).then(function(r){ return r.json(); }).then(function(j){
    if(j && j.ok && j.need){
      if(j.done){
        document.getElementById("sync-sub").textContent = "数据已同步到最新, 正在进入…";
        setTimeout(function(){ location.reload(); }, 600);
      } else {
        showSyncFail(j.message || "同步未完成");
      }
    } else if(j && !j.ok){
      showSyncFail(j.message || "同步失败");
    } else {
      hideSyncOverlay();
    }
  }).catch(function(){ hideSyncOverlay(); });
}
function syncIfNeeded(){ doSync(false); }
/* echarts 完整性检测: 缺失时动态重载本地库(带时间戳防缓存), 加载完成后重试初始化图表
   注意: 只检查 init 函数存在, 绝不调用 echarts.init 做探测(空对象会抛 getContext 错误) */
function ensureEcharts(){
  if(window.echarts && typeof window.echarts.init === "function"){
    return true;
  }
  try {
    var s = document.createElement("script");
    s.src = "static/echarts.v2.min.js?t=" + Date.now();
    s.onload = function(){
      /* 重载成功: 重新初始化所有净值图 */
      Object.keys(CFG.funds).forEach(function(k){
        initNavChart(k, true);
        loadNavData(k);
      });
    };
    document.head.appendChild(s);
  } catch(e) {}
  return false;
}
/* ---------------- 关注(星标) + 手动排序(本地持久化) ---------------- */
function starState(){ var s = {}; try { s = JSON.parse(localStorage.getItem("nav_star") || "{}"); } catch(e){} return s; }
function starSet(code, on){ var s = starState(); if(on) s[code] = 1; else delete s[code]; try { localStorage.setItem("nav_star", JSON.stringify(s)); } catch(e){} }
function starOf(code){ return !!starState()[code]; }
function orderState(){ var o = []; try { o = JSON.parse(localStorage.getItem("nav_order") || "[]"); } catch(e){} return o; }
function orderSet(arr){ try { localStorage.setItem("nav_order", JSON.stringify(arr || [])); } catch(e){} }
function displayOrder(){
  /* 关注优先, 再按手动顺序, 最后按配置顺序 */
  var cfgKeys = Object.keys(CFG.funds);
  var man = orderState();
  function idx(k){ var i = man.indexOf(k); return i < 0 ? 1e9 : i; }
  var starred = cfgKeys.filter(function(k){ return starOf(k); });
  var rest = cfgKeys.filter(function(k){ return !starOf(k); });
  starred.sort(function(a,b){ return idx(a) - idx(b); });
  rest.sort(function(a,b){ return idx(a) - idx(b); });
  return starred.concat(rest);
}
function reorderCards(){
  var main = document.getElementById("main");
  if(!main) return;
  var order = displayOrder();
  order.forEach(function(k){
    var el = document.getElementById("card_" + k);
    if(el) main.appendChild(el);  /* 移动 DOM 节点到末尾, 依次形成新顺序, 不重建图表 */
  });
  Object.keys(CFG.funds).forEach(function(k){
    var b = document.getElementById("star_" + k);
    if(b){
      var on = starOf(k);
      b.textContent = on ? "★" : "☆";
      b.classList.toggle("on", on);
      b.title = on ? "取消关注" : "关注(优先排前)";
    }
  });
}
function toggleStar(k){
  starSet(k, !starOf(k));
  reorderCards();
  /* 若打开了管理面板的"排序模式"开关, 同步其顺序 */
  var so = document.getElementById("mgr-sort-mode");
  if(so && so.checked && !starOf(k)){ /* 取消关注时回到手动顺序尾部, 重排已处理 */ }
}
/* ---------------- 金额总览看板 ---------------- */
function summaryData(){
  /* 聚合所有已配置持仓基金的金额指标(未持仓 configured=false 不计入) */
  var total = { current: 0, gain: 0, buy: 0, today: 0, funds: [] };
  Object.keys(CFG.funds).forEach(function(k){
    var p = CFG.funds[k].position || {};
    if(!p.configured) return;
    var cur = p.current_amount || 0, gain = p.total_gain || 0, buy = p.buy_amount || 0, td = p.today_est_change || 0;
    total.current += cur; total.gain += gain; total.buy += buy; total.today += td;
    total.funds.push({ key: k, name: CFG.funds[k].name, code: k,
      current: cur, gain: gain, gain_pct: p.total_gain_pct, buy: buy, today: td });
  });
  if(sumSortKey){
    total.funds.sort(function(a, b){
      return ((a[sumSortKey] || 0) - (b[sumSortKey] || 0)) * sumSortDir;
    });
  }
  total.gain_pct = total.buy > 0 ? total.gain / total.buy * 100 : null;
  return total;
}
function sumSortTh(key, label){
  var ind = sumSortKey === key ? (sumSortDir > 0 ? " ▲" : " ▼") : "";
  return '<th class="sortable num" data-sort="' + key + '" title="点击排序"> ' + label + ind + '</th>';
}
function summaryHTML(){
  var d = summaryData();
  var held = d.funds;
  var big = money(d.current);
  var gainStr = money(d.gain) + (d.gain_pct == null ? "" : " (" + sv(d.gain_pct) + "%)");
  var rows = held.length
    ? held.map(function(f){
        return '<tr data-fkey="' + f.key + '" style="cursor:pointer" title="点击定位到该基金卡片">' +
          '<td class="nm">' + f.name + ' <span class="src">' + f.code + '</span></td>' +
          '<td class="num" title="该基金当前市值 = 剩余份额 × 最新净值(昨收)">' + money(f.current) + '</td>' +
          '<td class="num" title="该基金累计收益 = 已实现收益 + 持仓浮动盈亏 − 累计手续费" style="color:' + col(f.gain) + '">' + money(f.gain) + (f.gain_pct == null ? "" : " (" + sv(f.gain_pct) + "%)") + '</td>' +
          '<td class="num" title="该基金按今日实时行情预估的当日涨跌金额" style="color:' + col(f.today) + '">' + money(f.today) + '</td></tr>';
      }).join("")
    : '<tr><td colspan="4" style="color:#9ca3af;text-align:center;padding:8px">暂无持仓 — 在持仓管理中配置金额或添加买卖记录后显示</td></tr>';
  return '<div class="card sumcard" id="summary-card">' +
    '<div class="sum-head">金额总览</div>' +
    '<div class="sum-grid">' +
      '<div class="sum-cell" title="全部持仓基金按最新官方净值(昨收)计算的市值合计"><div class="sum-label">现有金额（昨收市值）</div><div class="sum-big" id="sum-current">' + big + '</div></div>' +
      '<div class="sum-cell" title="全部基金累计收益合计 = 已实现收益 + 持仓浮动盈亏 − 累计手续费"><div class="sum-label">累计收益</div><div class="sum-big" id="sum-gain" style="color:' + col(d.gain) + '">' + gainStr + '</div>' +
        '<div class="sum-sub" title="累计投入 = 全部买入金额合计(不含手续费)">累计投入 ' + money(d.buy) + '</div></div>' +
      '<div class="sum-cell" title="全部持仓按今日实时行情预估的当日涨跌金额合计"><div class="sum-label">当日预估变化</div><div class="sum-big" id="sum-today" style="color:' + col(d.today) + '">' + money(d.today) + '</div></div>' +
    '</div>' +
    '<div class="sum-title">持有基金累计收益（' + held.length + ' 只）' +
      '<button class="btn sum-fold-btn" id="sum-fold-btn">' + (sumFold === "1" ? "展开" : "收起") + '</button></div>' +
    '<div id="summary-detail" style="display:' + (sumFold === "1" ? "none" : "") + '">' +
    '<table class="tbl"><thead><tr><th>基金</th>' +
      sumSortTh("current", "现有金额") + sumSortTh("gain", "累计收益") + sumSortTh("today", "当日预估") +
    '</tr></thead><tbody>' + rows + '</tbody></table>' +
    '<div class="note">注: 金额以最新官方净值(昨收)计算; 累计收益=已实现收益+持仓浮动盈亏−累计手续费; 当日预估随行情自动刷新。点击列表头可排序, 点击某行可定位到该基金卡片。</div>' +
    '</div>' +
    '</div>';
}
function refreshSummary(){
  /* 实时行情变化时联动更新总览的当日预估(不重建DOM, 只更新数值) */
  var d = summaryData();
  var e1 = document.getElementById("sum-current");
  var e2 = document.getElementById("sum-gain");
  var e3 = document.getElementById("sum-today");
  if(e1) e1.textContent = money(d.current);
  if(e2){ e2.textContent = money(d.gain) + (d.gain_pct == null ? "" : " (" + sv(d.gain_pct) + "%)"); e2.style.color = col(d.gain); }
  if(e3){ e3.textContent = money(d.today); e3.style.color = col(d.today); }
  /* 明细行: 更新当日预估列 */
  d.funds.forEach(function(f){
    var tr = document.querySelector('tr[data-fkey="' + f.key + '"]');
    if(tr){
      var c = tr.children[3];
      if(c){ c.textContent = money(f.today); c.style.color = col(f.today); }
    }
  });
}
function bindSummary(){
  var card = document.getElementById("summary-card");
  if(!card) return;
  try { sumFold = localStorage.getItem("sumfold") || "0"; } catch(e){ sumFold = "0"; }
  var det = document.getElementById("summary-detail");
  if(det) det.style.display = sumFold === "1" ? "none" : "";
  var fb = document.getElementById("sum-fold-btn");
  if(fb){
    if(!fb._b){ fb._b = true; fb.addEventListener("click", function(){
      var d = document.getElementById("summary-detail"); if(!d) return;
      var folded = d.style.display === "none";
      d.style.display = folded ? "" : "none";
      fb.textContent = folded ? "收起" : "展开";
      try { localStorage.setItem("sumfold", folded ? "0" : "1"); } catch(e){}
    }); }
    fb.textContent = sumFold === "1" ? "展开" : "收起";
  }
  card.querySelectorAll("th.sortable").forEach(function(th){
    if(th._b) return; th._b = true;
    th.addEventListener("click", function(){
      var k = th.getAttribute("data-sort");
      if(sumSortKey === k){ sumSortDir = -sumSortDir; }
      else { sumSortKey = k; sumSortDir = -1; }
      renderSummary();
    });
  });
  card.querySelectorAll('tr[data-fkey]').forEach(function(tr){
    if(tr._b) return; tr._b = true;
    tr.addEventListener("click", function(){
      var k = tr.getAttribute("data-fkey");
      var c = document.getElementById("card_" + k);
      if(c){ var body = document.getElementById("body_" + k); if(body && body.style.display === "none") foldToggle(k); c.scrollIntoView({behavior:"smooth", block:"start"}); c.style.transition="box-shadow .5s"; c.style.boxShadow="0 0 0 3px #c7d2fe"; setTimeout(function(){ c.style.boxShadow=""; },1500); }
    });
  });
}
function renderSummary(){
  var card = document.getElementById("summary-card");
  if(!card) return;
  try { sumFold = localStorage.getItem("sumfold") || "0"; } catch(e){ sumFold = "0"; }
  card.innerHTML = summaryHTML();
  bindSummary();
}
function build(){
  ensureEcharts();  /* echarts 缺失/异常时尝试动态重载(带时间戳防缓存) */
  var main = document.getElementById("main");
  var order = displayOrder();
  var fbEl = document.getElementById("filter-box");
  if(fbEl) fbEl.innerHTML = filterBarHTML();
  main.innerHTML = summaryHTML() + order.map(function(k){ return cardHTML(CFG.funds[k], k); }).join("");
  var cEl = document.getElementById("corr-box");
  if(cEl) cEl.innerHTML = corrHTML();
  var bar = document.getElementById("filter-bar");
  if(bar){
    bar.querySelectorAll(".ft").forEach(function(b){
      b.addEventListener("click", function(){
        var t = b.getAttribute("data-t");
        applyFilter(t);
        if(t === "__all__"){ hideTagInfo(); } else { showTagInfo(t); }
      });
    });
  }
  var savedFilter = "__all__";
  try { savedFilter = localStorage.getItem("filter_tag") || "__all__"; } catch(e) {}
  if(savedFilter !== "__all__" && !(bar && bar.querySelector('.ft[data-t="' + savedFilter + '"]'))){
    savedFilter = "__all__";
  }
  applyFilter(savedFilter);
  /* 诊断 + 保险: 启动时若发现过半卡片处于折叠(多为误触/旧状态, 导致"曲线/日期区间不可见"), 自动全部展开 */
  var foldedAll = 0;
  try {
    order.forEach(function(k){ if(localStorage.getItem("fold_" + k) === "1") foldedAll++; });
    if(foldedAll >= order.length / 2 && order.length > 1){
      order.forEach(function(k){ localStorage.setItem("fold_" + k, "0"); });
      foldedAll = 0;
    }
  } catch(e) {}
  order.forEach(function(k){
    var fb = document.getElementById("fold_" + k);
    if(fb) fb.addEventListener("click", function(){ foldToggle(k); });
    var sb = document.getElementById("star_" + k);
    if(sb) sb.addEventListener("click", function(){ toggleStar(k); });
    restoreFold(k);
    initNavChart(k);
    loadNavData(k);  /* 无条件触发数据拉取(与echarts初始化解耦, 保证Network必有navs请求) */
  });
  /* 金额总览: 折叠/排序/明细行定位 事件绑定 */
  bindSummary();
  try { initCharts(); } catch(e) {}
  document.getElementById("gen").textContent = "页面快照生成: " + CFG.generated_at.replace("T", " ") + " · 打开即自动同步数据";
  /* 自动刷新: 页面加载时从数据库读取设置(默认开启, 60秒); 读取失败退回默认。
     初始数据拉取由 applyAutoRefresh 统一处理: 开启则启动定时器, 关闭则只拉一次不启动定时器 */
  applyAutoRefresh();
  syncIfNeeded();
  checkConn();
  /* 空库提示: 无基金数据时展示导入/添加引导, 否则隐藏 */
  try {
    var _empty = !CFG.funds || Object.keys(CFG.funds).length === 0;
    var _eh = document.getElementById("empty-hint");
    if (_eh) _eh.style.display = _empty ? "block" : "none";
  } catch (e) {}
  /* 全局 resize 兜底: build 同步执行时 layout 未完成, 等 layout 后所有图表重绘一次 */
  setTimeout(function(){
    Object.keys(CFG.funds).forEach(function(k){
      var ch = window["navchart_" + k];
      if(ch && typeof ch.resize === "function"){ try{ ch.resize(); }catch(e){} }
      var ch2 = window["chart_" + k];
      if(ch2 && typeof ch2.resize === "function"){ try{ ch2.resize(); }catch(e){} }
    });
  }, 50);
}
/* 连接状态横幅: 检测是否通过服务打开(file:// 或服务未连接时历史曲线不可用, 明确提示正确打开方式) */
function checkConn(){
  var el = document.getElementById("conn-hint");
  if(!el) return;
  try {
    if(location.protocol === "file:"){
      el.style.display = "block";
      el.innerHTML = '<b>当前为本地文件模式，历史曲线不可用。</b> 请双击 <b>start_server.bat</b> 启动服务，然后访问 <b>http://127.0.0.1:8123</b>（不要直接打开 dashboard.html）。行情/管理功能同样需要服务。';
      return;
    }
  } catch(e) {}
  fetch("/api/funds").then(function(r){ return r.json(); }).then(function(j){
    if(j && j.ok){
      el.style.display = "none";
    } else {
      el.style.display = "block";
      el.innerHTML = '<b>未连接到本地服务(127.0.0.1:8123)，历史曲线不可用。</b> 请双击 <b>start_server.bat</b> 启动服务后刷新页面。';
    }
  }).catch(function(){
    el.style.display = "block";
    el.innerHTML = '<b>未连接到本地服务(127.0.0.1:8123)，历史曲线不可用。</b> 请双击 <b>start_server.bat</b> 启动服务后刷新页面。';
  });
}
/* 页面诊断状态条: echarts/净值数据/曲线初始化/折叠 状态一览, 便于定位"曲线不显示"原因 */
/* 全部展开 / 全部收起(卡片) */
function cardExpandAll(){
  Object.keys(CFG.funds).forEach(function(k){
    var b = document.getElementById("body_" + k);
    var fb = document.getElementById("fold_" + k);
    if(b && b.style.display === "none"){
      b.style.display = "";
      if(fb) fb.textContent = "收起";
      var ch = window["navchart_" + k];
      if(ch){ try{ ch.resize(); }catch(e){} }
      else { initNavChart(k, true); }
    }
    try { localStorage.setItem("fold_" + k, "0"); } catch(e) {}
  });
}
function cardCollapseAll(){
  Object.keys(CFG.funds).forEach(function(k){
    var b = document.getElementById("body_" + k);
    var fb = document.getElementById("fold_" + k);
    if(b){ b.style.display = "none"; if(fb) fb.textContent = "展开"; }
    try { localStorage.setItem("fold_" + k, "1"); } catch(e) {}
  });
}
document.getElementById("btn-refresh").addEventListener("click", function(){ poll(); });
["20", "60"].forEach(function(s){
  document.getElementById("intv-" + s).addEventListener("click", function(){
    POLL_MS = parseInt(s, 10) * 1000;
    clearInterval(pollTimer); pollTimer = setInterval(poll, POLL_MS);
    setStatus("自动刷新间隔已改为 " + s + "s"); poll();
    persistRefresh(true, parseInt(s, 10));
  });
});
document.getElementById("intv-stop").addEventListener("click", function(){
  clearInterval(pollTimer); pollTimer = null;
  setStatus("已暂停 · 点 20s/60s 或手动刷新更新");
  persistRefresh(false, Math.round(POLL_MS / 1000));
});
/* 数据备份 / 初始化: 导出与导入 */
document.getElementById("btn-export").addEventListener("click", function(){
  setStatus("正在导出数据…");
  fetch("/api/export").then(function(r){ return r.json(); }).then(function(b){
    var blob = new Blob([JSON.stringify(b, null, 2)], {type: "application/json"});
    var a = document.createElement("a");
    var ts = (b.exported_at || "").replace(/[-:T]/g, "").slice(0, 13);
    var pretty = ts.replace(/(\d{8})(\d{2})/, "$1-$2");
    a.href = URL.createObjectURL(blob);
    a.download = "fund-data-" + (pretty || "backup") + ".json";
    document.body.appendChild(a); a.click(); a.remove();
    var fc = (b.funds && b.funds.funds) ? Object.keys(b.funds.funds).length : 0;
    var tc = (b.trades && b.trades.trades) ? b.trades.trades.length : 0;
    setStatus("已导出数据备份(" + fc + " 只基金, " + tc + " 笔交易)");
  }).catch(function(){ setStatus("导出失败: 无法连接服务"); });
});
document.getElementById("btn-import").addEventListener("click", function(){
  document.getElementById("import-file").click();
});
document.getElementById("import-file").addEventListener("change", function(ev){
  var file = ev.target.files && ev.target.files[0];
  if(!file) return;
  var reader = new FileReader();
  reader.onload = function(){
    var bundle;
    try { bundle = JSON.parse(reader.result); }
    catch(e){ alert("导入失败: 文件不是合法 JSON"); ev.target.value = ""; return; }
    if(!confirm("导入将覆盖当前基金配置与交易记录并重建看板，确定继续？\n（用于空数据库初始化或换机迁移）")){
      ev.target.value = ""; return;
    }
    setStatus("正在导入并重建看板…");
    var ib = document.getElementById("btn-import");
    setBtnBusy(ib, "导入中…");
    fetch("/api/import", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(bundle)
    }).then(function(r){ return r.json(); }).then(function(j){
      if(j && j.ok){
        setStatus("导入成功: " + (j.funds || 0) + " 只基金, " + (j.trades || 0) + " 笔交易 — 即将刷新页面");
        setTimeout(function(){ location.reload(); }, 800);
      } else {
        clearBtnBusy(ib, "导入数据");
        setStatus("导入失败: " + ((j && j.message) || "未知错误"));
        ev.target.value = "";
      }
    }).catch(function(){ clearBtnBusy(ib, "导入数据"); setStatus("导入失败: 无法连接服务"); ev.target.value = ""; });
  };
  reader.readAsText(file);
});
/* 自动刷新设置: 页面加载时读取, 与开关/间隔按钮联动持久化到数据库 */
function applyAutoRefresh(){
  fetch("/api/settings").then(function(r){ return r.json(); }).then(function(j){
    var st = (j && j.settings) || {};
    var on = st.auto_refresh !== false;            // 默认开启
    var sec = (st.refresh_seconds === 20 || st.refresh_seconds === 60) ? st.refresh_seconds : 60;
    POLL_MS = sec * 1000;
    if(on){
      clearInterval(pollTimer); pollTimer = setInterval(poll, POLL_MS);
      setStatus("自动刷新已开启 · 间隔 " + sec + "s");
    } else {
      clearInterval(pollTimer); pollTimer = null;
      setStatus("已暂停 · 点 20s/60s 或手动刷新更新");
    }
    poll();  /* 无论开关, 加载时先拉一次最新行情(开启后由定时器持续刷新, 关闭则仅此一次) */
  }).catch(function(){
    POLL_MS = 60000;
    clearInterval(pollTimer); pollTimer = setInterval(poll, POLL_MS);
    setStatus("自动刷新已开启 · 间隔 60s(默认)");
    poll();
  });
}
function persistRefresh(autoRefresh, sec){
  try {
    fetch("/api/settings", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({auto_refresh: autoRefresh, refresh_seconds: sec})
    }).catch(function(){});
  } catch(e) {}
}
var btnMgr = document.getElementById("btn-mgr");
if(btnMgr) btnMgr.addEventListener("click", mgrToggle);
/* 右侧浮动按钮: 回到顶部 / 打开持仓管理 */
var fabTop = document.getElementById("fab-top");
if(fabTop) fabTop.addEventListener("click", function(){ window.scrollTo({ top: 0, behavior: "smooth" }); });
var fabMgr = document.getElementById("fab-mgr");
if(fabMgr) fabMgr.addEventListener("click", function(){
  var m = document.getElementById("mgr");
  if(m){
    if(m.style.display === "none"){ m.style.display = "block"; mgrInit(); }
    m.scrollIntoView({ behavior: "smooth", block: "start" });
  }
});
var btnExA = document.getElementById("btn-expand-all");
if(btnExA) btnExA.addEventListener("click", cardExpandAll);
var btnColA = document.getElementById("btn-collapse-all");
if(btnColA) btnColA.addEventListener("click", cardCollapseAll);
var btnMgAdd = document.getElementById("mg-add-btn");
if(btnMgAdd) btnMgAdd.addEventListener("click", mgrAdd);
/* ② 交易记录: 全部展开 / 全部收起 */
var trEx = document.getElementById("tr-expand-all");
if(trEx) trEx.addEventListener("click", function(){
  var list = document.getElementById("tr-list");
  Object.keys(CFG.funds).forEach(function(k){ try{ localStorage.setItem("trfold_" + k, "0"); localStorage.setItem("trgfold_" + k, "0"); }catch(e){} });
  if(list){
    list.querySelectorAll(".tr-group").forEach(function(g){
      var body = g.querySelector(".tr-body"); if(body) body.style.display = "";
      var rec = g.querySelector(".tr-records"); if(rec) rec.style.display = "";
    });
    list.querySelectorAll(".tr-gfold").forEach(function(b){ b.textContent = "收起基金"; });
    list.querySelectorAll(".tr-fold").forEach(function(b){ b.textContent = "收起记录"; });
  }
});
var trCol = document.getElementById("tr-collapse-all");
if(trCol) trCol.addEventListener("click", function(){
  var list = document.getElementById("tr-list");
  Object.keys(CFG.funds).forEach(function(k){ try{ localStorage.setItem("trfold_" + k, "1"); }catch(e){} });
  if(list){
    list.querySelectorAll(".tr-records").forEach(function(b){ b.style.display = "none"; });
    list.querySelectorAll(".tr-fold").forEach(function(b){ b.textContent = "展开记录"; });
  }
});
var trColF = document.getElementById("tr-collapse-funds");
if(trColF) trColF.addEventListener("click", function(){
  var list = document.getElementById("tr-list");
  Object.keys(CFG.funds).forEach(function(k){ try{ localStorage.setItem("trgfold_" + k, "1"); }catch(e){} });
  if(list){
    list.querySelectorAll(".tr-body").forEach(function(b){ b.style.display = "none"; });
    list.querySelectorAll(".tr-gfold").forEach(function(b){ b.textContent = "展开基金"; });
  }
});
var tpClose = document.getElementById("tp-close");
if(tpClose) tpClose.addEventListener("click", hideTradePop);
var tpPop = document.getElementById("trade-pop");
if(tpPop) tpPop.addEventListener("click", function(e){ if(e.target === tpPop) hideTradePop(); });
if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", build); else build();
"""

JS = JS.replace("__CFG__", json.dumps(CFG, ensure_ascii=False))

HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="dash-version" content="v50-bugfix-audit">
<title>基金实时涨跌看板 · 招商畜牧ETF & 中欧医疗健康</title>
<link rel="icon" type="image/svg+xml" href="/favicon.ico">
<script>
/* ECharts 5.5.0 内联(避免外部脚本加载被缓存/扩展干扰) */
__ECHARTS_INLINE__
</script>
<style>
  * { margin:0; padding:0; box-sizing:border-box; }
  body { font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif; background:#f6f7f9; color:#1f2937; padding:20px; }
  .wrap { max-width:1080px; margin:0 auto; }
  h1 { font-size:22px; font-weight:700; }
  .meta { color:#6b7280; font-size:12.5px; margin:6px 0 10px; }
  .bar { display:flex; align-items:center; gap:10px; margin-bottom:14px; flex-wrap:wrap; }
  .status { background:#fff; border:1px solid #e5e7eb; border-radius:8px; padding:8px 12px; font-size:12.5px; color:#374151; flex:1; min-width:260px; }
  .btn { border:1px solid #d1d5db; background:#fff; border-radius:6px; padding:5px 12px; font-size:12.5px; cursor:pointer; color:#374151; }
  .btn:hover { background:#f3f4f6; }
  .btn-mgr { background:#eef2ff; color:#4338ca; border-color:#c7d2fe; font-weight:600; }
  .btn-gfold { background:#eef2ff; color:#4338ca; border-color:#c7d2fe; }
  .mgr { background:#fbfcff; }
  .mg-row { display:flex; align-items:center; gap:8px; padding:7px 0; border-bottom:1px dashed #e8ecf3; flex-wrap:wrap; }
  .mg-row.dragging { opacity:0.5; }
  .mg-row.drag-over { border-top:2px solid #4338ca; }
  .mg-drag { cursor:grab; color:#9ca3af; font-size:14px; user-select:none; padding:0 2px; }
  .mg-name { font-weight:600; font-size:13px; min-width:170px; }
  .btn-star { background:none; border:none; font-size:17px; line-height:1; cursor:pointer; color:#d1d5db; padding:0 2px; }
  .btn-star.on { color:#f59e0b; }
  .mg-in { border:1px solid #d1d5db; border-radius:6px; padding:5px 8px; font-size:12.5px; width:120px; }
  .mg-in:focus { outline:2px solid #c7d2fe; border-color:#818cf8; }
  .mgr-add { margin-top:6px; display:flex; align-items:center; gap:8px; flex-wrap:wrap; }
  .mgr-sec-title { font-size:12.5px; font-weight:700; color:#4338ca; margin:14px 0 8px; }
  .tag-np { background:#f3f4f6; color:#6b7280; border-radius:4px; padding:1px 6px; font-size:11px; font-weight:400; }
  /* 交易记录: 按基金缩进分组 */
  .tr-group { margin:8px 0 6px 14px; padding:8px 10px 10px; border-left:2px solid #e0e7f5; background:#f8fafc; border-radius:0 8px 8px 0; }
  .tr-group-head { font-size:13px; margin-bottom:6px; display:flex; align-items:center; gap:8px; flex-wrap:wrap; }
  .tr-records { margin-top:6px; }
  .tr-group-head b { font-weight:700; color:#111827; }
  .tr-add-row { display:flex; align-items:center; gap:6px; flex-wrap:wrap; margin-bottom:6px; }
  .tr-add-row .mg-in { width:auto; padding:4px 7px; font-size:12px; }
  .tr-hold { background:#fff; border:1px solid #e5e7eb; border-radius:8px; padding:8px 10px; margin:4px 0 6px; }
  .tr-hold-src { font-size:12px; color:#6b7280; margin-bottom:6px; }
  .tr-hold-src b { color:#374151; }
  .tr-hold-grid { display:flex; flex-wrap:wrap; gap:8px 14px; }
  .tr-hold-grid .pc { min-width:118px; background:#f8fafc; border:1px solid #eef0f4; border-radius:6px; padding:5px 8px; }
  .tag-badge { display:inline-block; border:1px solid; background:#fff; border-radius:99px; padding:1px 8px; font-size:11px; font-weight:600; margin-left:4px; }
  .tag-click { cursor:pointer; }
  .tag-click:hover { transform:translateY(-1px); }
  .tag-info { background:#fbfcff; border:1px solid #e0e7f5; border-radius:12px; padding:14px 16px; margin-bottom:14px; font-size:12.5px; line-height:1.75; }
  .ti-head { font-weight:700; font-size:14px; margin-bottom:6px; display:flex; align-items:center; gap:8px; }
  .ti-line { margin-top:6px; color:#374151; }
  .ti-k { display:inline-block; background:#eef2ff; color:#4338ca; border-radius:6px; padding:1px 8px; font-size:11.5px; font-weight:600; margin-right:8px; }
  .ti-line ul { margin:4px 0 0 18px; padding:0; }
  .ti-line a { color:#185FA5; text-decoration:none; }
  .ti-line a:hover { text-decoration:underline; }
  .ti-line code { background:#eef2ff; color:#4338ca; padding:1px 5px; border-radius:4px; font-size:11px; }
  .filter-bar { display:flex; gap:6px; flex-wrap:wrap; margin-bottom:14px; }
  .ft { border:1px solid #d1d5db; background:#fff; border-radius:99px; padding:4px 14px; font-size:12.5px; cursor:pointer; color:#374151; }
  .ft:hover { background:#f3f4f6; }
  .ft.on { background:#1a56db; color:#fff !important; border-color:#1a56db; }
  .mg-tags { width:190px; }
  .mg-del { border-color:#f5c2c2; color:#b42318; background:#fff8f8; }
  .mg-del:hover { background:#fcebeb; }
  .mg-del.confirming { background:#b42318; color:#fff; border-color:#b42318; font-weight:700; }
  .mg-purge { border-color:#fecaca; color:#dc2626; background:#fff; }
  .mg-purge:hover { background:#fef2f2; }
  .mg-purge.confirming { background:#dc2626; color:#fff; border-color:#dc2626; font-weight:700; }
  .card { background:#fff; border:1px solid #e5e7eb; border-radius:12px; padding:18px 20px; margin-bottom:18px; box-shadow:0 1px 3px rgba(16,24,40,.05); }
  .card-head { display:flex; justify-content:space-between; align-items:center; }
  .fname { font-size:17px; font-weight:700; display:flex; align-items:center; gap:8px; }
  /* 金额总览 */
  .sumcard { background:linear-gradient(180deg,#f8fafc,#fff); border:1px solid #e2e8f0; }
  .sum-head { font-size:16px; font-weight:800; color:#1e293b; margin-bottom:10px; }
  .sum-grid { display:flex; gap:12px; flex-wrap:wrap; margin-bottom:14px; }
  .sum-cell { flex:1; min-width:180px; background:#fff; border:1px solid #e5e7eb; border-radius:10px; padding:12px 16px; }
  .sum-label { font-size:12px; color:#6b7280; margin-bottom:4px; }
  .sum-big { font-size:22px; font-weight:800; color:#1e293b; }
  .sum-sub { font-size:11.5px; color:#9ca3af; margin-top:2px; }
  .sum-title { font-size:13px; font-weight:700; color:#374151; margin:4px 0 8px; display:flex; align-items:center; gap:8px; }
  .sum-fold-btn { padding:2px 12px; font-size:12px; }
  .tbl th.sortable { cursor:pointer; user-select:none; white-space:nowrap; }
  .tbl th.sortable:hover { background:#eef2ff; color:#4338ca; }
  .fcode { background:#1a56db; color:#fff; border-radius:6px; padding:2px 8px; font-size:12px; font-weight:700; letter-spacing:.5px; }
  .fh-right { display:flex; align-items:center; gap:10px; }
  .btn-fold { border:1px solid #d1d5db; background:#f8fafc; border-radius:6px; padding:3px 10px; font-size:12px; cursor:pointer; color:#374151; }
  .btn-fold:hover { background:#eef2ff; }
  .navsec { margin-top:10px; }
  .nav-tabs { display:flex; gap:6px; margin-bottom:6px; }
  .nt { border:1px solid #d1d5db; background:#fff; border-radius:6px; padding:3px 10px; font-size:12px; cursor:pointer; color:#374151; }
  .nt:hover { background:#f3f4f6; }
  .nt.on { background:#185FA5; color:#fff; border-color:#185FA5; }
  .navchart { height:220px; }
  .fchg { font-size:30px; font-weight:800; line-height:1; }
  .badge { font-size:11px; padding:2px 8px; border-radius:99px; font-weight:600; }
  .b-etf { background:#e8f0fe; color:#1a56db; }
  .b-mut { background:#fef3e2; color:#b45309; }
  .rt-badge { display:inline-block; background:#ecfdf5; color:#047857; border:1px solid #a7f3d0; border-radius:6px; padding:1px 8px; font-size:12px; font-weight:600; }
  .hb-badge { display:inline-block; background:#f3f4f6; color:#6b7280; border:1px solid #e5e7eb; border-radius:6px; padding:1px 8px; font-size:12px; font-weight:600; }
  .sub { color:#6b7280; font-size:12.5px; margin-top:4px; }
  .base { margin-top:10px; background:#f8fafc; border:1px solid #eef0f3; border-radius:8px; padding:8px 12px; font-size:12.5px; }
  .model { margin-top:8px; font-size:13.5px; }
  .tag { display:inline-block; background:#eef2ff; color:#4338ca; border-radius:6px; padding:2px 8px; font-size:12px; margin-right:6px; font-weight:600; }
  .src { color:#9ca3af; font-size:11.5px; }
  .chart { height:300px; margin:14px 0 8px; }
  .tbl { width:100%; border-collapse:collapse; font-size:12.5px; }
  .tbl th { text-align:left; color:#6b7280; font-weight:600; border-bottom:1px solid #e5e7eb; padding:7px 6px; white-space:nowrap; }
  .tbl td { padding:6px; border-bottom:1px solid #f1f3f5; }
  .tbl .num { text-align:right; font-variant-numeric:tabular-nums; }
  .tbl th.num { text-align:right; }
  .tbl .nm { font-weight:600; }
  .tbl tbody tr:hover { background:#fafbfc; }
  .note { color:#9ca3af; font-size:11.5px; margin-top:8px; }
  .direct { margin-top:10px; background:#fafbfe; border:1px solid #e8ecf3; border-radius:10px; padding:10px 12px; }
  .direct-title { font-size:12px; font-weight:700; color:#374151; margin-bottom:6px; }
  .direct-list { margin:0; padding:0; list-style:none; display:flex; flex-wrap:wrap; gap:6px 14px; }
  .direct-list li { font-size:12.5px; color:#374151; display:flex; align-items:center; gap:5px; }
  .d-tag { display:inline-block; border-radius:5px; padding:1px 6px; font-size:10.5px; font-weight:700; }
  .d-fund { background:#fff1e6; color:#c2410c; }
  .d-stock { background:#e7f5ff; color:#1971c2; }
  .d-code { color:#9ca3af; font-size:11px; }
  .d-real { background:#e6fcf5; color:#0ca678; border-radius:5px; padding:1px 6px; font-size:10.5px; font-weight:600; }
  .d-est { background:#fff9db; color:#b08900; border-radius:5px; padding:1px 6px; font-size:10.5px; font-weight:600; }
  .direct-note { margin-top:8px; color:#9ca3af; font-size:11px; line-height:1.5; }
  .pos { margin-top:10px; background:#fafbfe; border:1px solid #e8ecf3; border-radius:10px; padding:10px 12px; }
  .pos-title { font-size:12px; font-weight:700; color:#374151; margin-bottom:8px; }
  .pos-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(130px,1fr)); gap:8px 12px; }
  .pc .pk { font-size:11px; color:#9ca3af; }
  .pc .pv { font-size:14.5px; font-weight:700; font-variant-numeric:tabular-nums; color:#1f2937; }
  .pos-empty { color:#9ca3af; font-size:12px; background:#f8fafc; border:1px dashed #e5e7eb; border-radius:8px; padding:10px 12px; margin-top:10px; }
  .pos-empty code { background:#eef2ff; color:#4338ca; padding:1px 6px; border-radius:4px; font-size:11.5px; }
  .corr-title { font-size:13px; font-weight:700; margin:4px 0 8px; }
  .corr-empty { color:#9ca3af; font-size:12px; background:#f8fafc; border:1px dashed #e5e7eb; border-radius:8px; padding:10px 12px; margin-bottom:12px; }
  .foot { color:#9ca3af; font-size:11.5px; line-height:1.7; margin-top:6px; }
  .disc { background:#fff8f0; border:1px solid #f5dfc0; border-radius:8px; padding:10px 14px; font-size:12px; color:#7c5a25; margin-top:16px; line-height:1.7; }
  /* ---- 打开即同步: 全屏覆盖层 ---- */
  #sync-overlay { position:fixed; top:0; left:0; right:0; bottom:0; background:rgba(248,250,252,0.94); z-index:9999; display:none; align-items:center; justify-content:center; }
  #sync-overlay.show { display:flex; }
  .sync-card { text-align:center; padding:34px 46px; background:#fff; border-radius:14px; box-shadow:0 10px 34px rgba(15,23,42,0.14); border:1px solid #e5e7eb; max-width:440px; margin:0 16px; }
  .sync-spinner { width:46px; height:46px; border:4px solid #dbeafe; border-top-color:#185FA5; border-radius:50%; margin:0 auto 18px; animation:syncspin 0.9s linear infinite; }
  @keyframes syncspin { to { transform:rotate(360deg); } }
  .sync-title { font-size:16px; font-weight:700; color:#111827; margin-bottom:8px; }
  .sync-sub { font-size:12.5px; color:#6b7280; line-height:1.7; }
  .sync-btns { margin-top:18px; display:none; gap:10px; justify-content:center; }
  #sync-overlay.fail .sync-spinner { border-color:#fecaca; border-top-color:#b42318; animation:none; }
  #sync-overlay.fail .sync-title { color:#b42318; }
  #sync-overlay.fail .sync-sub { color:#7f1d1d; }
  /* ---- 按钮等待效果(添加/删除/保存/恢复/导入等异步操作) ---- */
  .btn:disabled { opacity:.62; cursor:wait; }
  .btn-spinner { display:inline-block; width:13px; height:13px; border:2px solid rgba(55,65,81,.25); border-top-color:#4338ca; border-radius:50%; margin-right:6px; vertical-align:-2px; animation:syncspin .7s linear infinite; }
  /* ---- 右侧浮动按钮 ---- */
  .fab { position:fixed; right:18px; bottom:22px; display:flex; flex-direction:column; gap:10px; z-index:9990; }
  .fab-btn { width:52px; height:52px; border-radius:14px; border:1px solid #c7d2fe; background:#fff; color:#4338ca; box-shadow:0 6px 18px rgba(30,41,59,.18); cursor:pointer; font-size:18px; font-weight:700; line-height:1.1; display:flex; flex-direction:column; align-items:center; justify-content:center; gap:1px; transition:transform .12s, background .12s; }
  .fab-btn span { font-size:11px; font-weight:600; }
  .fab-btn:hover { background:#eef2ff; transform:translateY(-2px); }
  .fab-btn:active { transform:translateY(0); }
  /* ---- 筛选提示条 ---- */
  .filter-hint { background:#fffbeb; border:1px solid #fde68a; border-radius:8px; padding:8px 12px; font-size:12.5px; color:#92400e; margin-bottom:10px; }
  /* ---- 连接状态横幅 ---- */
  .conn-hint { background:#fef2f2; border:1px solid #fecaca; border-radius:8px; padding:10px 14px; font-size:13px; color:#991b1b; margin-bottom:10px; line-height:1.7; }
  /* ---- 交易详情浮层 ---- */
  #trade-pop { position:fixed; inset:0; background:rgba(15,23,42,0.35); z-index:9998; display:none; align-items:center; justify-content:center; }
  .tp-card { background:#fff; border-radius:12px; box-shadow:0 10px 34px rgba(15,23,42,0.2); border:1px solid #e5e7eb; min-width:300px; max-width:380px; margin:0 16px; }
  .tp-head { display:flex; justify-content:space-between; align-items:center; padding:12px 16px; border-bottom:1px solid #eef0f3; font-weight:700; font-size:14px; color:#111827; }
  .tp-tbl { width:100%; border-collapse:collapse; }
  .tp-tbl td { padding:8px 16px; font-size:13px; border-bottom:1px solid #f3f4f6; }
  .tp-tbl td:first-child { color:#6b7280; width:80px; }
</style>
</head>
<body>
<div class="wrap">
  <h1>基金实时涨跌看板</h1>
  <div class="meta" id="gen"></div>
  <div class="bar">
    <div class="status" id="status">正在连接实时行情…</div>
    <button class="btn" id="btn-refresh">手动刷新</button>
    <button class="btn" id="intv-20">20秒</button>
    <button class="btn" id="intv-60">60秒</button>
    <button class="btn" id="intv-stop">暂停</button>
    <button class="btn" id="btn-expand-all">全部展开</button>
    <button class="btn" id="btn-collapse-all">全部收起</button>
    <button class="btn btn-mgr" id="btn-mgr">持仓管理</button>
  </div>
  <div id="empty-hint" class="conn-hint" style="display:none">
    当前数据库为空，暂无基金数据。点击右上角 <strong>持仓管理 → 数据备份 / 初始化 → 导入数据</strong>，选择之前导出的 JSON 文件完成初始化；或在「持仓管理」面板添加基金。导入 / 添加后页面会自动重建。
  </div>
  <div id="conn-hint" class="conn-hint" style="display:none"></div>
  <div id="mgr" class="card mgr" style="display:none">
    <div class="mgr-sec-title">添加基金（编码6位, 如 022982; 添加后自动刷新页面）
      <button class="btn" id="mg-add-btn">添加基金</button></div>
    <div class="mgr-add" style="margin-top:4px">
      <input class="mg-in" id="mg-code" placeholder="基金编码(6位, 如022982)" style="width:200px">
      <input class="mg-in" id="mg-name" placeholder="名称(可选)">
      <input class="mg-in" id="mg-anchor" placeholder="底层ETF(联接基金选填, 如516670)" style="width:230px">
    </div>
    <div class="corr-title">持仓管理 — 两个区域分开: ① 基金管理(分类/删除) ② 交易记录(买卖, 按基金缩进; 持仓金额/收益全部按买卖记录自动计算)</div>
    <div class="mgr-sec-title">① 基金管理 — 设置分类(标签) / 保存 / 删除(伪删除, 数据保留可恢复) / 拖拽 ⠿ 调整排序(关注优先, 本地保存)
      <button class="btn" id="mg-fold">展开</button> <span class="src" id="mg-count"></span></div>
    <div id="mgr-body" style="display:none">
      <div id="mgr-funds"></div>
      <div id="mgr-deleted" style="display:none;margin-top:10px"></div>
    </div>
    <div class="mgr-sec-title">② 交易记录 — 每只基金下展示买入/卖出, 可添加/删除 (趋势图标点, 点击标点看详情)
      <button class="btn" id="tr-expand-all">全部展开</button>
      <button class="btn" id="tr-collapse-all">全部收起</button>
      <button class="btn btn-gfold" id="tr-collapse-funds" title="收起全部基金(折叠每只基金, 仅留标题)">收起全部基金</button></div>
    <div id="tr-list"></div>
    <div id="mgr-msg" class="src" style="margin-top:8px"></div>
    <div class="mgr-sec-title">数据备份 / 初始化（导出为文件，或导入文件初始化空库）
      <button class="btn" id="btn-export">导出数据</button>
      <button class="btn" id="btn-import">导入数据</button>
      <input type="file" id="import-file" accept=".json,application/json" style="display:none">
    </div>
    <div class="src" style="margin-top:4px">导出当前 基金配置 + 交易记录 + 设置 为 JSON 文件；导入将<strong>覆盖</strong>当前数据并重建看板，可用于空数据库初始化或换机迁移。</div>
  </div>
  <div id="trade-pop" style="display:none">
    <div class="tp-card">
      <div class="tp-head"><span id="tp-title"></span><button class="btn" id="tp-close">×</button></div>
      <div id="tp-body"></div>
    </div>
  </div>
  <div id="filter-box"></div>
  <div id="filter-hint" class="filter-hint" style="display:none"></div>
  <div id="tag-info" class="tag-info" style="display:none"></div>
  <div id="main"></div>
  <div class="card" style="box-shadow:none">
    <div id="corr-box"></div>
    <div class="foot">实时机制：页面定时通过公开行情接口拉取实时数据，现价/涨跌幅/持仓模型在本页自动刷新，无需后端服务。持仓披露与官方基线(前一日收盘净值)由后台引擎每日收盘后自动锁定更新。</div>
    <div class="foot">方法说明：① 每日首次运行先取前一日收盘后公布的准确数据(官方净值/涨跌幅)作为当日基线并锁定; ② 当日实时涨跌幅一律以"昨收"为锚计算，持仓加权模型=Σ(权重×个股实时涨跌幅)，未披露余量以行业指数近似(ETF用中证消费近似、混合用中证医疗); ③ ETF"基金本身"用场内实时价，场外混合基金用持仓加权模型实时计算。</div>
    <div class="disc">免责声明：以上内容基于公开数据和量化分析，仅供参考，不构成投资建议。市场有风险，投资需谨慎。任何投资决策应结合个人风险承受能力、资金状况和投资目标独立判断，必要时咨询持牌专业机构。过往表现不预示未来收益。</div>
  </div>
  <div class="fab">
    <button class="fab-btn" id="fab-top" title="回到页面顶部">↑<span>顶部</span></button>
    <button class="fab-btn" id="fab-mgr" title="打开持仓管理">⚙<span>管理</span></button>
  </div>
</div>
<div id="sync-overlay">
  <div class="sync-card">
    <div class="sync-spinner"></div>
    <div class="sync-title" id="sync-title">正在同步最新数据…</div>
    <div class="sync-sub" id="sync-sub">检查昨日官方净值 / 补全历史记录, 请稍候（约需 10~30 秒）</div>
    <div class="sync-btns" id="sync-btns">
      <button class="btn" id="sync-retry">重试同步</button>
      <button class="btn" id="sync-enter">直接进入</button>
    </div>
  </div>
</div>
<script>
""" + JS + """
</script>
</body>
</html>
"""

HTML = HTML.replace("__ECHARTS_INLINE__", ECHARTS_INLINE)

def _atomic_write(path, text, attempts=15, wait=0.15):
    """原子写: 先写同目录临时文件, 再 os.replace 覆盖。
    避免 live_server / VSCode / 杀软文件监视器正在读取目标文件时, 直接
    open('w') 独占写触发 Windows 共享冲突(PermissionError); 即便读句柄不带
    FILE_SHARE_DELETE 导致 os.replace 短暂失败, 也通过重试避开瞬时锁, 仍失败
    则回退为直接覆盖写(与旧行为一致)。"""
    import tempfile, time
    d = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        last = None
        for i in range(attempts):
            try:
                os.replace(tmp, path)
                return
            except (PermissionError, OSError) as e:
                last = e
                time.sleep(wait)
        # 重试耗尽: 回退直接写(覆盖可能被读锁挡, 但已尽力)
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
        except Exception:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise last
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise

_atomic_write(os.path.join(BASE, "dashboard.html"), HTML)
print("dashboard.html(实时版) 已生成, 长度", len(HTML))
