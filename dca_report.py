# -*- coding: utf-8 -*-
"""Render data/dca_result.json -> self-contained HTML report (inline SVG, no CDN)."""
import json, os, html

BASE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(BASE, "data", "dca_result.json")
OUT = os.path.join(BASE, "dca_003095_report.html")

RED, GREEN, BLUE, GREY = "#c62828", "#1b7f4b", "#2563eb", "#94a3b8"
UP = RED      # 中国习惯: 涨红跌绿
DOWN = GREEN


def esc(s):
    return html.escape(str(s))


def fmt(v, p=2, suffix="%"):
    if v is None:
        return "—"
    return f"{v:+.{p}f}{suffix}" if suffix else f"{v:.{p}f}"


# ------------------------------------------------------------------ SVG helpers
def axis_frame(w, h, pad):
    return f'<rect x="{pad}" y="{pad}" width="{w-2*pad}" height="{h-2*pad}" fill="none" stroke="#e2e8f0"/>'


def line_svg(series, marks=None, w=900, h=340, title=""):
    """series: [(date_str, nav)]; marks: [(date_str, kind, label)] kind in buy/sell"""
    pad_l, pad_r, pad_t, pad_b = 56, 16, 28, 34
    vals = [v for _, v in series]
    vmin, vmax = min(vals), max(vals)
    rng = (vmax - vmin) or 1
    vmin -= rng * 0.08
    vmax += rng * 0.08
    n = len(series)
    iw, ih = w - pad_l - pad_r, h - pad_t - pad_b

    def X(i):
        return pad_l + iw * i / max(1, n - 1)

    def Y(v):
        return pad_t + ih * (1 - (v - vmin) / (vmax - vmin))

    pts = " ".join(f"{X(i):.1f},{Y(v):.1f}" for i, (_, v) in enumerate(series))
    area = f"{pad_l},{pad_t+ih} " + pts + f" {pad_l+iw},{pad_t+ih}"

    # 250d moving average
    ma = []
    for i in range(n):
        if i < 249:
            ma.append(None)
        else:
            ma.append(sum(v for _, v in series[i - 249:i + 1]) / 250)

    out = [f'<svg viewBox="0 0 {w} {h}" width="{w}" height="{h}" xmlns="http://www.w3.org/2000/svg" '
           f'font-family="-apple-system,Segoe UI,Microsoft YaHei,sans-serif" '
           f'style="max-width:100%;height:auto;display:block">']
    out.append(axis_frame(w, h, pad_l - 8))
    # y gridlines
    for k in range(5):
        v = vmin + (vmax - vmin) * k / 4
        y = Y(v)
        out.append(f'<line x1="{pad_l}" y1="{y:.1f}" x2="{w-pad_r}" y2="{y:.1f}" stroke="#eef2f7"/>')
        out.append(f'<text x="{pad_l-8}" y="{y+4:.1f}" font-size="11" fill="#64748b" '
                   f'text-anchor="end">{v:.2f}</text>')
    # x labels
    for i in range(0, n, max(1, n // 6)):
        out.append(f'<text x="{X(i):.1f}" y="{h-pad_b+16}" font-size="11" fill="#64748b" '
                   f'text-anchor="middle">{series[i][0][:7]}</text>')
    out.append(f'<polygon points="{area}" fill="{BLUE}" opacity="0.07"/>')
    out.append(f'<polyline points="{pts}" fill="none" stroke="{BLUE}" stroke-width="1.8"/>')
    # MA250
    segs, cur = [], []
    for i, m in enumerate(ma):
        if m is None:
            if cur:
                segs.append(cur); cur = []
        else:
            cur.append((X(i), Y(m)))
    if cur:
        segs.append(cur)
    for s in segs:
        if len(s) > 1:
            out.append('<polyline points="' + " ".join(f"{x:.1f},{y:.1f}" for x, y in s) +
                       f'" fill="none" stroke="#f59e0b" stroke-width="1.4" stroke-dasharray="4,3"/>')
    # marks
    idx = {d: i for i, (d, _) in enumerate(series)}
    for d, kind, label in (marks or []):
        if d not in idx:
            for i in range(n - 1, -1, -1):
                if series[i][0] <= d:
                    j = i; break
            else:
                continue
        else:
            j = idx[d]
        x, y = X(j), Y(series[j][1])
        col = GREEN if kind == "sell" else RED
        out.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="6.5" fill="{col}" stroke="#fff" stroke-width="1.6"/>')
        out.append(f'<text x="{x:.1f}" y="{y-12:.1f}" font-size="11" fill="{col}" '
                   f'text-anchor="middle" font-weight="700">{esc(label)}</text>')
    out.append(f'<text x="{pad_l}" y="{pad_t-10}" font-size="12" fill="#334155">{esc(title)}</text>')
    out.append(f'<g font-size="11"><rect x="{w-pad_r-150}" y="{pad_t-22}" width="10" height="3" '
               f'fill="{BLUE}"/><text x="{w-pad_r-134}" y="{pad_t-17}" fill="#64748b">单位净值</text>'
               f'<rect x="{w-pad_r-78}" y="{pad_t-22}" width="10" height="3" fill="#f59e0b"/>'
               f'<text x="{w-pad_r-62}" y="{pad_t-17}" fill="#64748b">250日均线</text></g>')
    out.append('</svg>')
    return "".join(out)


def bar_svg(items, w=900, h=300, title="", unit="%", show_zero=True, highlight=None):
    """items: [(label, value, color)]"""
    n_items = len(items)
    rotate = n_items > 16
    pad_l, pad_r, pad_t, pad_b = 46, 10, 30, (96 if rotate else 52)
    iw, ih = w - pad_l - pad_r, h - pad_t - pad_b
    vals = [v for _, v, _ in items]
    vmax = max(vals + [0])
    vmin = min(vals + [0])
    if show_zero:
        vmin = min(vmin, 0)
    rng = (vmax - vmin) or 1
    vmax += rng * 0.10
    vmin -= rng * 0.06 if vmin < 0 else 0
    n = len(items)
    slot = iw / max(1, n)
    bw = min(slot * 0.66, 46)

    def Y(v):
        return pad_t + ih * (1 - (v - vmin) / (vmax - vmin))

    out = [f'<svg viewBox="0 0 {w} {h}" width="{w}" height="{h}" xmlns="http://www.w3.org/2000/svg" '
           f'font-family="-apple-system,Segoe UI,Microsoft YaHei,sans-serif" '
           f'style="max-width:100%;height:auto;display:block">']
    out.append(axis_frame(w, h, pad_l - 8))
    for k in range(5):
        v = vmin + (vmax - vmin) * k / 4
        y = Y(v)
        out.append(f'<line x1="{pad_l}" y1="{y:.1f}" x2="{w-pad_r}" y2="{y:.1f}" stroke="#eef2f7"/>')
        out.append(f'<text x="{pad_l-8}" y="{y+4:.1f}" font-size="10.5" fill="#64748b" '
                   f'text-anchor="end">{v:.1f}</text>')
    if vmin < 0 < vmax:
        out.append(f'<line x1="{pad_l}" y1="{Y(0):.1f}" x2="{w-pad_r}" y2="{Y(0):.1f}" '
                   f'stroke="#cbd5e1" stroke-width="1.2"/>')
    for i, (lab, v, col) in enumerate(items):
        x = pad_l + slot * i + (slot - bw) / 2
        y0, y1 = Y(max(v, 0)), Y(min(v, 0))
        hh = max(1.5, abs(y1 - y0))
        hi = (highlight is not None and lab == highlight)
        fill_col = "#1e40af" if hi else col
        out.append(f'<rect x="{x:.1f}" y="{min(y0,y1):.1f}" width="{bw:.1f}" height="{hh:.1f}" '
                   f'fill="{fill_col}" rx="2" opacity="{1.0 if hi else 0.88}"/>')
        ty = (y0 - 6) if v >= 0 else (y1 + 13)
        out.append(f'<text x="{x+bw/2:.1f}" y="{ty:.1f}" font-size="{9.5 if rotate else 10.5}" '
                   f'fill="{fill_col}" text-anchor="middle" font-weight="{700 if hi else 600}">{v:.2f}</text>')
        cx = pad_l + slot * i + slot / 2
        if rotate:
            out.append(f'<text transform="rotate(-42 {cx:.1f} {h-pad_b+22:.1f})" '
                       f'x="{cx:.1f}" y="{h-pad_b+22:.1f}" font-size="9.5" fill="#475569" '
                       f'text-anchor="end">{esc(lab)}</text>')
        else:
            out.append(f'<text x="{cx:.1f}" y="{h-pad_b+16}" font-size="10.5" '
                       f'fill="#475569" text-anchor="middle">{esc(lab)}</text>')
    out.append(f'<text x="{pad_l}" y="{pad_t-10}" font-size="12.5" fill="#334155">{esc(title)}</text>')
    out.append('</svg>')
    return "".join(out)


def heat_table(rows, cols, data, caption_map, fmt_fn=None):
    """rows: [(sid, name)], cols: [key] -> colored HTML table"""
    fmt_fn = fmt_fn or (lambda v: f"{v:+.1f}")
    out = ['<table class="tbl"><thead><tr><th class="l">策略</th>']
    for c in cols:
        out.append(f'<th>{esc(caption_map.get(c, c))}</th>')
    out.append('</tr></thead><tbody>')
    for sid, name in rows:
        out.append(f'<tr><td class="l"><b>{esc(sid)}</b> <span class="dim">{esc(name)}</span></td>')
        for c in cols:
            cell = data.get(sid, {}).get(c)
            if cell is None:
                out.append('<td class="dim">—</td>')
                continue
            avg, win = cell["avg"], cell["win"]
            if avg > 0.5:
                bg, fg = "#fdecec", "#b91c1c"
            elif avg < -0.5:
                bg, fg = "#e8f6ee", "#12703f"
            else:
                bg, fg = "#f6f7f9", "#64748b"
            out.append(f'<td style="background:{bg};color:{fg}"><b>{fmt_fn(avg)}</b>'
                       f'<span class="win">{win:.0f}%</span></td>')
        out.append('</tr>')
    out.append('</tbody></table>')
    return "".join(out)


# ------------------------------------------------------------------ report
def build():
    D = json.load(open(SRC, encoding="utf-8"))
    meta, grid, rob_sum = D["meta"], D["grid"], D["rob_sum"]
    rob_sum_ama, ama, rob = D["rob_sum_ama"], D["ama"], D["robust"]
    cad_spread, fee_cmp = D["cad_spread"], D["fee_cmp"]
    nav = [(d, v) for d, v in D["nav_series"]]
    strat_name = {s["sid"]: s["name"] for s in D["strategies"]}

    # ---- nav stats
    vals = [v for _, v in nav]
    peak_i = vals.index(max(vals))
    mdd, peak = 0.0, vals[0]
    for v in vals:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)
    nav_chg = (vals[-1] / vals[0] - 1) * 100

    # ---- main-window rows (monthly 15th, fixed amount)
    def g(sid, cad="每月15日"):
        return grid.get(f"{cad}|{sid}", {})

    sids = [s["sid"] for s in D["strategies"]]

    # ---- chart 1: nav + trade marks of best strategy
    best = max(grid.items(), key=lambda kv: kv[1]["roi"])
    best_cad, best_sid = best[0].split("|")
    cf = best[1]["cashflows"]
    marks = []
    for d, a in cf:
        if a > 1 and not (d == cf[-1][0]):
            marks.append((d, "sell", "卖"))
    chart_nav = line_svg(nav, marks, title=f"近三年单位净值走势与卖出点（{best_cad} + {strat_name.get(best_sid, best_sid)}）")

    # ---- chart 2: cadence
    cad_items = []
    for c in D["cadences"]:
        lab = c["label"]
        r = g("hold", lab)
        cad_items.append((lab, r["roi"], BLUE))
    chart_cad = bar_svg(cad_items, w=900, title="定投频率 vs 近三年收益率（不止盈，总投入均为12万）",
                        highlight=best_cad if best_cad in [c["label"] for c in D["cadences"]] else None)

    cad_sp_items = [(k, v["mean"], BLUE) for k, v in
                    sorted(cad_spread.items(), key=lambda x: -x[1]["mean"])]
    chart_cad_sp = bar_svg(cad_sp_items, w=900,
                           title="28个三年窗口的平均收益率（不止盈）：频率间极差仅 1.36 个百分点",
                           highlight=None)

    # ---- chart 3: sell strategies (main window, monthly 15th)
    order = sorted(sids, key=lambda s: -g(s, "每月15日").get("roi", -99))
    sell_items = [(s, g(s, "每月15日")["roi"],
                   UP if g(s, "每月15日")["roi"] >= g("hold", "每月15日")["roi"] else GREY)
                  for s in order]
    chart_sell = bar_svg(sell_items, w=900, h=340,
                         title="卖出策略 vs 近三年收益率（每月15日定投，灰=跑输不止盈）")

    # ---- chart 4: target / drawdown sensitivity
    tgt = [(f"+{k}%", g(f"target{k}")["roi"], BLUE) for k in (10, 15, 20, 25, 30, 35, 40, 50, 60, 80)]
    base_roi = g("hold")["roi"]
    chart_tgt = bar_svg(tgt, w=900, h=300,
                        title=f"目标收益率止盈阈值敏感性（不止盈基准 {base_roi:.2f}%）")
    dd_items = [(f"{k}%", g(f"dd{k}")["roi"], BLUE) for k in (5, 8, 10, 12, 15, 20)]
    chart_dd = bar_svg(dd_items, w=900, h=300, title="最大回撤止盈阈值敏感性（近三年）")

    # ---- chart 5: fees
    fee_keys = [("每月15日", "A_disc", "A类·1折"), ("每月15日", "A_full", "A类·原价"),
                ("每月15日", "C", "C类"), ("每月15日", "ZERO", "零费率(理论)")]
    fee_items = []
    for cad, pk, lab in fee_keys:
        r = fee_cmp.get(f"{cad}|{pk}|target25")
        fee_items.append((lab, r["roi"], BLUE))
    chart_fee = bar_svg(fee_items, w=760, h=250, title="费率口径对比（每月15日 + 目标+25%止盈，12万投入）")

    # ---- C class NAV change over the same window (for the fee section)
    import sqlite3
    con = sqlite3.connect(os.path.join(BASE, "data", "fund_history.db"))
    r = con.execute("SELECT dwjz FROM nav_history WHERE fund_code='003096' "
                    "AND trade_date BETWEEN ? AND ? ORDER BY trade_date",
                    (meta["start"], meta["end"])).fetchall()
    con.close()
    c_navs = [x[0] for x in r if x[0]]
    c_chg = (c_navs[-1] / c_navs[0] - 1) * 100 if len(c_navs) > 1 else None

    # ---- tables
    reg_cols = ["all", "bull", "range", "bear"]
    _n = {c: rob_sum["target25"][c]["n"] for c in reg_cols if c in rob_sum["target25"]}
    reg_cap = {"all": f"全样本({_n.get('all', 28)}窗口)", "bull": f"牛市({_n.get('bull', 0)}窗口)",
               "range": f"震荡市({_n.get('range', 0)}窗口)", "bear": f"熊市({_n.get('bear', 0)}窗口)"}
    n_bull, n_bear, n_range = _n.get("bull", 0), _n.get("bear", 0), _n.get("range", 0)
    rows_for_tbl = [(s, strat_name.get(s, s)) for s in sids]
    tbl_fixed = heat_table(rows_for_tbl, reg_cols, rob_sum, reg_cap)
    tbl_ama = heat_table(rows_for_tbl, reg_cols, rob_sum_ama, reg_cap)

    # ---- range-market window detail (most relevant: the last 3y is one of them)
    base_map = {r["start"]: r for r in rob.get("每月15日|hold", [])}
    t25_map = {r["start"]: r for r in rob.get("每月15日|target25", [])}
    d12_map = {r["start"]: r for r in rob.get("每月15日|dd12", [])}
    rng_rows = []
    for s in sorted(base_map):
        b = base_map[s]
        if not (-20 <= b["bh"] <= 50):
            continue
        t, d = t25_map.get(s), d12_map.get(s)
        if not t or not d:
            continue
        cur = s == meta["start"]
        sty = ' style="background:#fffbeb"' if cur else ""
        rng_rows.append(
            f'<tr{sty}><td class="l">{esc(s)} ~ {esc(b["end"])}'
            f'{"<b> ← 本次回测区间</b>" if cur else ""}</td>'
            f'<td class="num">{b["bh"]:+.1f}%</td><td class="num">{b["roi"]:+.2f}%</td>'
            f'<td class="num">{t["roi"]:+.2f}%</td>'
            f'<td class="num {"up" if t["excess"]>0 else "down"}">{t["excess"]:+.1f}</td>'
            f'<td class="num">{d["roi"]:+.2f}%</td>'
            f'<td class="num {"up" if d["excess"]>0 else "down"}">{d["excess"]:+.1f}</td></tr>')
    # the exact window under review, appended for direct comparison
    _ls, _h, _t, _d = D["lumpsum"]["roi"], g("hold"), g("target25"), g("dd12")
    rng_rows.append(
        f'<tr style="background:#fffbeb"><td class="l"><b>{meta["start"]} ~ {meta["end"]}'
        f' ← 本次回测区间</b></td><td class="num">{_ls:+.1f}%</td>'
        f'<td class="num">{_h["roi"]:+.2f}%</td><td class="num">{_t["roi"]:+.2f}%</td>'
        f'<td class="num up">+{_t["roi"]-_h["roi"]:.1f}</td>'
        f'<td class="num">{_d["roi"]:+.2f}%</td>'
        f'<td class="num down">{_d["roi"]-_h["roi"]:+.1f}</td></tr>')

    # ---- best combos table
    top = sorted(grid.items(), key=lambda kv: -kv[1]["roi"])[:12]
    top_rows = []
    for k, r in top:
        cad, sid = k.split("|")
        top_rows.append(
            f'<tr><td class="l">{esc(cad)}</td><td class="l">{esc(strat_name.get(sid,sid))}</td>'
            f'<td class="num up">{r["roi"]:.2f}%</td><td class="num">{r["xirr"]:.2f}%</td>'
            f'<td class="num">{r["final_value"]:,.0f}</td><td class="num">{r["profit"]:,.0f}</td>'
            f'<td class="num">{r["buy_fee"]:,.0f}</td><td class="num">{r["sell_count"]}</td></tr>')

    # ---- amount rule table
    ama_rows = []
    for sid in ("hold", "target25", "q90r30", "dd12", "q95"):
        a = ama.get(f"每月15日|fixed|{sid}", {}).get("roi")
        b = ama.get(f"每月15日|ma_bias|{sid}", {}).get("roi")
        d = (b - a) if (a is not None and b is not None) else None
        cls = "up" if (d or 0) > 0 else "down"
        ama_rows.append(
            f'<tr><td class="l">{esc(strat_name.get(sid,sid))}</td>'
            f'<td class="num">{a:.2f}%</td><td class="num">{b:.2f}%</td>'
            f'<td class="num {cls}">{d:+.2f}pct</td></tr>')

    # ---- fee detail table
    fee_rows = []
    for cad, pk, lab in fee_keys:
        for sid in ("hold", "target25"):
            r = fee_cmp.get(f"{cad}|{pk}|{sid}")
            fee_rows.append(
                f'<tr><td class="l">{esc(lab)}</td><td class="l">{esc(strat_name.get(sid,sid))}</td>'
                f'<td class="num">{r["roi"]:.2f}%</td><td class="num">{r["buy_fee"]:,.0f}</td>'
                f'<td class="num">{r["final_value"]:,.0f}</td></tr>')

    # ---- cadence spread table
    cad_rows = []
    for lab, s in sorted(cad_spread.items(), key=lambda x: -x[1]["mean"]):
        cad_rows.append(f'<tr><td class="l">{esc(lab)}</td><td class="num">{s["mean"]:.2f}%</td>'
                        f'<td class="num dim">{s["std"]:.2f}</td>'
                        f'<td class="num dim">{s["min"]:.1f}% ~ {s["max"]:.1f}%</td></tr>')

    S = []
    S.append(f'''<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>中欧医疗健康混合(003095) 定投方式 · 卖出时机 · 费率 全回测</title>
<style>
*{{box-sizing:border-box}}
body{{margin:0;background:#f1f5f9;color:#0f172a;
 font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif;line-height:1.65}}
.wrap{{max-width:1000px;margin:0 auto;padding:28px 20px 60px}}
h1{{font-size:26px;margin:0 0 6px}}
.sub{{color:#64748b;font-size:13.5px;margin-bottom:22px}}
h2{{font-size:19px;margin:34px 0 12px;padding-left:11px;border-left:4px solid {BLUE}}}
h3{{font-size:15px;margin:22px 0 8px;color:#334155}}
.card{{background:#fff;border:1px solid #e2e8f0;border-radius:10px;padding:18px 20px;margin:14px 0;
 box-shadow:0 1px 2px rgba(15,23,42,.04)}}
.grid4{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:16px 0}}
.grid2{{display:grid;grid-template-columns:repeat(2,1fr);gap:12px}}
.kpi{{background:#fff;border:1px solid #e2e8f0;border-radius:10px;padding:14px 16px}}
.kpi .k{{font-size:12px;color:#64748b}}
.kpi .v{{font-size:24px;font-weight:700;margin-top:2px}}
.kpi .n{{font-size:11.5px;color:#94a3b8;margin-top:2px}}
.up{{color:{UP}}} .down{{color:{DOWN}}} .dim{{color:#94a3b8}}
table.tbl{{width:100%;border-collapse:collapse;font-size:13px;margin:10px 0}}
table.tbl th{{background:#f8fafc;color:#475569;font-weight:600;padding:8px 10px;
 border-bottom:2px solid #e2e8f0;text-align:right;white-space:nowrap}}
table.tbl th.l{{text-align:left}}
table.tbl td{{padding:7px 10px;border-bottom:1px solid #f1f5f9;text-align:right;white-space:nowrap}}
table.tbl td.l{{text-align:left;white-space:normal}}
table.tbl tbody tr:hover{{background:#f8fafc}}
table.tbl td.num{{font-variant-numeric:tabular-nums}}
.win{{display:block;font-size:10.5px;opacity:.72;font-weight:500}}
.note{{background:#fffbeb;border:1px solid #fde68a;border-radius:8px;padding:12px 15px;
 font-size:13.5px;color:#78350f;margin:14px 0}}
.ok{{background:#eff6ff;border:1px solid #bfdbfe;color:#1e3f8f}}
.bad{{background:#fef2f2;border:1px solid #fecaca;color:#7f1d1d}}
ul{{margin:8px 0 8px 20px;padding:0}} li{{margin:5px 0;font-size:14px}}
.tag{{display:inline-block;background:#eef2ff;color:#3730a3;border-radius:4px;
 padding:1px 7px;font-size:11.5px;margin-right:5px}}
.big{{font-size:15px;font-weight:600}}
hr{{border:0;border-top:1px solid #e2e8f0;margin:26px 0}}
code{{background:#f1f5f9;padding:1px 5px;border-radius:4px;font-size:12.5px}}
@media(max-width:760px){{.grid4,.grid2{{grid-template-columns:repeat(2,1fr)}}}}
</style></head><body><div class="wrap">''')

    S.append(f'''<h1>中欧医疗健康混合A（003095）定投全回测</h1>
<div class="sub">回测区间 {meta['start']} ~ {meta['end']}（{meta['days']} 个交易日） ·
净值数据来自天天基金官方历史净值（本地库 nav_history） ·
费率按天天基金披露的基金费率表建模 · 生成于 {meta['generated']}</div>

<div class="grid4">
 <div class="kpi"><div class="k">近三年净值涨跌</div><div class="v {('up' if nav_chg>0 else 'down')}">{nav_chg:+.2f}%</div>
   <div class="n">{vals[0]:.4f} → {vals[-1]:.4f}</div></div>
 <div class="kpi"><div class="k">期间最高 / 最低</div><div class="v" style="font-size:19px">{max(vals):.4f}</div>
   <div class="n">最低 {min(vals):.4f}</div></div>
 <div class="kpi"><div class="k">期间最大回撤</div><div class="v down">{mdd*100:.1f}%</div>
   <div class="n">峰值 {nav[peak_i][0]}</div></div>
 <div class="kpi"><div class="k">期初一次性买入</div><div class="v">{D['lumpsum']['roi']:+.2f}%</div>
   <div class="n">12万 → {D['lumpsum']['final_value']:,.0f}</div></div>
</div>

<div class="note ok"><b>一句话结论：</b>近三年这是一个<b>典型震荡市</b>（净值三年只涨 {nav_chg:+.2f}%，
中途最大回撤 {abs(mdd*100):.0f}%）。在这样的行情里，<b>卖出时机比定投频率重要得多</b>——
频率带来的差异不到 1.7 个百分点（统计噪音级），而选对卖出规则能差出 <b>7 个百分点</b>。
近三年最优是「<b>月投 + 收益率到 +25% 清仓后继续投</b>」：以每月15日扣款为例，12 万投入期末约
{g('target25','每月15日')['final_value']:,.0f} 元（<b>+{g('target25','每月15日')['roi']:.2f}%</b>），
同期不止盈只有 {base_roi:.2f}%，差 <b>{g('target25','每月15日')['roi']-base_roi:.2f} 个百分点</b>。
但要把这个结论放到 28 个三年窗口里检验才可靠 —— 见第 3 节，那才是真正决定你该不该止盈的地方。</div>''')

    # ---------------- 1 cadence
    S.append(f'''<h2>一、定投频率：多久投一次，其实几乎不重要</h2>
<div class="card">
<p>先把「卖出」固定为不止盈，只改变频率，15 种频率全部投入相同的 12 万元（每期金额 = 12万 ÷ 期数）。</p>
{chart_cad}
<div class="grid2" style="margin-top:6px">
<div>{chart_cad_sp}</div>
<div>
<h3>28 个三年窗口的平均表现</h3>
<table class="tbl"><thead><tr><th class="l">频率</th><th>平均收益率</th><th>标准差</th><th>区间</th></tr></thead>
<tbody>{"".join(cad_rows)}</tbody></table>
<p class="dim" style="font-size:12.5px">起点从 2016-09 到 2023-06，每 3 个月一个起点，共 28 个不重叠程度不同的三年窗口。</p>
</div></div>
<div class="note"><b>结论：频率是噪音级因素。</b>
15 种频率在 28 个窗口上的平均收益率极差只有 <b>1.36 个百分点</b>（20.43% ~ 21.79%），
而同期收益的标准差高达 <b>51~53 个百分点</b>——市场本身的影响是频率的 38 倍。
近三年单窗口的差异（12.96% ~ 14.62%）同样处在这个噪音区间内，<b>「每月几号 / 每周几」不存在稳定最优解</b>，
选一个你能坚持的就行（推荐月投或双周投，扣款次数少、心理负担小、资金安排更从容）。</p>
</div></div>''')

    # ---------------- 2 sell timing
    S.append(f'''<h2>二、卖出时机：近三年止盈明显跑赢，+25% 是最优阈值</h2>
<div class="card">
{chart_sell}
<p class="dim" style="font-size:12.5px;margin-top:2px">灰色 = 跑输「不止盈」基准（{base_roi:.2f}%）。
所有组合投入相同，可直接比较期末金额。</p>
<h3>阈值敏感性</h3>
<div>{chart_tgt}</div>
<div>{chart_dd}</div>
<h3>近三年最优组合 TOP12</h3>
<table class="tbl"><thead><tr><th class="l">定投频率</th><th class="l">卖出规则</th>
<th>收益率</th><th>年化(XIRR)</th><th>期末金额</th><th>净收益</th><th>总费用</th><th>卖出次数</th>
</tr></thead><tbody>{"".join(top_rows)}</tbody></table>
<div class="note ok"><b>近三年读出的规律：</b>
<ul>
<li><b>目标 +25% 清仓</b>在 15 种频率上<b>全部位列第一梯队</b>（20.4%~21.4%），而且不同频率之间差异极小——
说明这不是某个频率的巧合，是卖出规则在起作用。</li>
<li>+20%~+30% 是甜蜜区：阈值太低（+10%）会被反复震出、支付更多赎回费且吃不到主升段；
阈值太高（≥+30%）在近三年<b>根本没触发过</b>，等于不止盈。</li>
<li><b>回撤止盈（跌 X% 就跑）在近三年是坑</b>：dd 系列只有 5%~7%，远不如不止盈的 {base_roi:.1f}%。
原因是 2026-07-17 单日 -7.65% 的那类急跌会把它触发在阶段低点，卖完就反弹。</li>
<li>分批止盈、移动止盈表现居中，没有额外的优势。</li>
</ul></div>
</div>''')

    # ---------------- 3 robustness
    S.append(f'''<h2>三、稳健性检验：止盈不是万能的，它高度依赖你处在什么行情</h2>
<div class="card">
<p>只看近三年会得出「止盈必胜」的结论，但三年只是一个样本。下面把同样的策略放到
<b>2016 年以来 28 个三年窗口</b>上重跑，并按窗口所属行情分组
（按该窗口「期初一次性买入并持有」的收益率划分：牛市 &gt;+50%、震荡 -20%~+50%、熊市 &lt;-20%）。
表中数字是<b>相对「不止盈」的超额收益率</b>（百分点），小字是该组的胜率。</p>
<h3>固定金额定投</h3>
{tbl_fixed}
<h3>低位加码定投（净值低于250日均线时加码买入）</h3>
{tbl_ama}
<h3>震荡市窗口逐个看（近三年就属于这一类，共 {n_range} 个）</h3>
<table class="tbl"><thead><tr><th class="l">三年窗口</th><th>期初买入持有</th>
<th>不止盈收益率</th><th>目标+25%</th><th>超额</th><th>回撤12%</th><th>超额</th>
</tr></thead><tbody>{"".join(rng_rows)}</tbody></table>
<p class="dim" style="font-size:12.5px">「期初买入持有」= 该窗口第一笔钱一次性买入并持有到期末的收益率，用来划分行情。
最后一行（黄底）是本次回测的精确区间，它不在滚动窗口序列里，单列出来便于直接对照。</p>
<div class="note bad"><b>这是全篇最重要的一张表，它推翻了第二节的表面结论：</b>
<ul>
<li><b>牛市里，所有止盈策略都大幅跑输</b>，而且是灾难级：目标 +25% 平均少赚 <b>50 个百分点</b>，
回撤止盈少赚 <b>50~65 个百分点</b>。这只基金 2017-2021 从 1 元涨到 4.3 元，任何中途下车都是错的。</li>
<li><b>震荡市里，止盈稳定跑赢</b>：几乎所有策略都是正超额，胜率 60%~100%。
近三年正属于这一类，所以第二节的结论成立。<b>但请把样本量看清楚：28 个窗口里震荡市只有
{n_range} 个、牛市 {n_bull} 个、熊市 {n_bear} 个</b>——分类后的统计力度有限，
上面这张表的数字应该被当作"方向性证据"，而不是精确概率。</li>
<li><b>熊市里，只有「回撤止盈」管用</b>：dd5/dd8/dd10/dd12 平均多赚 <b>10~16 个百分点、胜率 91%</b>
（{n_bear} 个熊市窗口里只有 1 个没跑赢）；目标收益率类几乎从不触发（超额 0）。</li>
<li><b>没有一个策略能在三种行情里通吃。</b>全样本看，止盈的平均超额都是负的（-8% ~ -22%），
因为牛市的踏空损失远大于熊市/震荡市的收益。</li>
<li>相对而言 <b>dd12 的性价比最高</b>：全样本胜率 50%（并列最高），
熊市 +16.4%（胜率 91%）、震荡市 +11.3%（胜率 80%）；唯一代价是牛市要认栽 -61.8%。
<b>target80（+80% 才止盈）的全样本损失最小（-8.0%），因为它几乎只在极端泡沫时触发</b>——
这也是最"保守"的止盈方式。</li>
</ul></div>
<div class="note"><b>所以「该不该止盈」本质上是一个择时判断，而不是一个规则：</b>
止盈的全部价值 = 熊市/震荡市的落袋收益 − 牛市的踏空损失。医学板块长期成长性还在，
但波动极大（这只基金三年可以跌 55%，也可以涨 140%）。
如果你判断未来三年是<b>震荡或下行</b>，那用 +25% 目标或 dd12 回撤止盈；
如果你判断是<b>牛市启动</b>，那任何止盈都是自废武功。</div>
</div>''')

    # ---------------- 4 amount rule
    S.append(f'''<h2>四、金额策略：低位加码（低于250日均线多买）</h2>
<div class="card">
<p>「定投方式」除了频率，还有每期买多少。这里测试一种简单规则：每期基准金额固定，
但按当日净值相对 250 日均线的偏离调整——低于均线 20% 以上买 2 倍、低 10~20% 买 1.5 倍、
高于均线 10~20% 只买 0.5 倍、高于 20% 不买（未投出的钱留在现金里按 1.5%/年计息）。</p>
<table class="tbl"><thead><tr><th class="l">卖出规则（每月15日定投）</th>
<th>固定金额</th><th>低位加码</th><th>差异</th></tr></thead>
<tbody>{"".join(ama_rows)}</tbody></table>
<div class="note ok"><b>结论：低位加码是小幅正面的改进，而且能减轻止盈的副作用。</b>
<ul>
<li>单纯不止盈时，低位加码只多赚 0.6~0.9 个百分点——<b>对「买」端的优化空间本身很小</b>。</li>
<li>但它和止盈规则有<b>协同效应</b>：低位加码把持仓成本拉低，使「+25% 收益」更容易达到，
让一些原本三年都不触发的规则（如 q90r30）真正生效，近三年 q90r30 从 13.65% 提升到 22.21%。</li>
<li>在 28 窗口检验里，低位加码让几乎所有止盈策略的<b>牛市踏空损失变小</b>
（target25：-50.5% → -46.2%；dd12：-61.8% → -49.9%），整体超额略有改善。</li>
<li>代价是规则复杂度上升，且需要你能在市场最悲观、最不想买的时候真的下单。</li>
</ul></div>
</div>''')

    # ---------------- 5 fees
    S.append(f'''<h2>五、买卖费率：怎么买比什么时候卖更容易确定地省钱</h2>
<div class="card">
<div>{chart_fee}</div>
<h3>费率口径明细（12万投入，3年）</h3>
<table class="tbl"><thead><tr><th class="l">费率口径</th><th class="l">卖出规则</th>
<th>收益率</th><th>总费用</th><th>期末金额</th></tr></thead>
<tbody>{"".join(fee_rows)}</tbody></table>
<h3>当前 003095 的费率结构（天天基金披露）</h3>
<table class="tbl"><thead><tr><th class="l">项目</th><th class="l">A类 003095</th><th class="l">C类 003096</th></tr></thead>
<tbody>
<tr><td class="l">申购费</td><td class="l">原价 1.50%，第三方平台 <b>1折 0.15%</b></td><td class="l"><b>0.00%</b></td></tr>
<tr><td class="l">赎回费（按持有期分档）</td><td class="l" colspan="2">&lt;7天 1.50% · 7-30天 0.75% ·
 30-365天 <b>0.50%</b> · 365-730天 0.25% · ≥730天 <b>0.00%</b></td></tr>
<tr><td class="l">销售服务费</td><td class="l">无</td><td class="l"><b>0.80%/年</b>（每日从净值中扣除）</td></tr>
<tr><td class="l">赎回费计算方式</td><td class="l" colspan="2">按<b>先进先出（FIFO）</b>逐笔计算持有天数与对应费率</td></tr>
</tbody></table>
<div class="note"><b>费率结论（三条，都是确定性的省钱）：</b>
<ul>
<li><b>务必走 1 折渠道申购。</b>同样 12 万投三年，1折渠道总费用约 <b>{fee_cmp['每月15日|A_disc|hold']['buy_fee']:,.0f} 元</b>，
原价渠道 <b>{fee_cmp['每月15日|A_full|hold']['buy_fee']:,.0f} 元</b>，差额 {fee_cmp['每月15日|A_full|hold']['buy_fee']-fee_cmp['每月15日|A_disc|hold']['buy_fee']:,.0f} 元，
吃掉 <b>{abs(fee_cmp['每月15日|A_full|hold']['roi']-fee_cmp['每月15日|A_disc|hold']['roi']):.2f} 个百分点</b>收益。
这一条比本文讨论的绝大多数「卖出技巧」都更确定、更值钱。</li>
<li><b>三年期持有，A 类（1折）优于 C 类约 1.2 个百分点。</b>C 类虽无申购费，
但 0.80%/年的销售服务费三年累计约 2.4%（已直接体现在净值里：同期 A 类净值 {nav_chg:+.2f}%，
C 类只有 {c_chg:+.2f}%，差距 {nav_chg-c_chg:.1f} 个百分点），远大于 A 类一次性的 0.15% 申购费。
<b>大致分界：持有超过约 1 年半到 2 年，A 类（1折）就更划算。</b></li>
<li><b>止盈会额外付出赎回费，但这不是主要成本。</b>近三年 target25 总费用约
{fee_cmp['每月15日|A_disc|target25']['buy_fee']:,.0f} 元，比不止盈多
{fee_cmp['每月15日|A_disc|target25']['buy_fee']-fee_cmp['每月15日|A_disc|hold']['buy_fee']:,.0f} 元
（占本金 0.2%）——因为它把原本能持有满 2 年、免赎回费的份额提前卖在了 0.5%/0.25% 的档位上。
相对而言，<b>牛市踏空 50 个百分点才是真正的代价</b>。</li>
</ul></div>
<div class="note ok"><b>顺带一个纯费率的优化：</b>赎回费在持有满 730 天后归零、满 365 天降到 0.25%。
如果你本就打算长期持有，<b>尽量避免持有不足 365 天就赎回</b>（那一档是 0.5%，且短期交易还有 7 天 1.5% 的惩罚档）。
回测里的 <code>hold365_5</code> 规则（每笔满 1 年且盈利 ≥5% 才赎回该笔）就是纯按费率设计的，
近三年它没能跑赢，但在熊市窗口里胜率 46%、全样本超额 -11.7%，属于中规中矩的费率友好型做法。</div>
</div>''')

    # ---------------- 6 nav chart + marks
    S.append(f'''<h2>六、最优策略的买卖点在净值图上的位置</h2>
<div class="card">
{chart_nav}
<p class="dim" style="font-size:12.5px">绿点为清仓卖出。可以看到近三年这两个卖点都落在<b>阶段性高点附近</b>——
这正是震荡市止盈有效的原因：净值反复回到同一区间，每一次冲高都是兑现机会。
（若换成 2017-2021 的单边牛市，同样的规则会在半山腰就把你甩下车。）</p>
</div>''')

    # ---------------- 7 conclusion
    S.append(f'''<h2>七、给你的操作建议</h2>
<div class="card">
<p class="big">按「确定性」从高到低排序：</p>
<ul>
<li><b><span class="tag">确定</span>走 1 折渠道申购 A 类（003095），不要按原价 1.5% 买。</b>
三年 12 万本金省下约 1,590 元，无风险、无判断成本。三年以上持有不选 C 类。</li>
<li><b><span class="tag">确定</span>频率随意，选月投（如每月 15 日或发薪日后 1 天）。</b>
频率差异是噪音，月投扣款次数少、更容易坚持，这才是真正影响结果的因素——坚持投满三年。</li>
<li><b><span class="tag">大概率</span>当前医药板块处于震荡格局时，设 +25% 目标收益止盈，
清仓后继续按原计划定投（不要停）。</b>近三年这条规则在 15 种频率上全面跑赢，
在 {n_range} 个震荡市窗口里胜率 80%（4/5，<b>样本很小，方向性参考</b>）。</li>
<li><b><span class="tag">可选</span>如果愿意多一步操作：净值低于 250 日均线时把当期金额加到 1.5~2 倍，
高于均线 10% 以上时减半或跳过（省下的钱留着，等跌下来再投）。</b>
近三年能再增厚 0.6~0.9 个百分点，并且能明显减轻止盈规则在牛市里的踏空损失。</li>
<li><b><span class="tag">判断</span>如果你强烈看好医药进入上行周期，就别止盈。</b>
牛市里止盈的代价（平均少赚 50 个百分点）远超震荡市的收益。
折中方案是提高阈值到 +80%，只在极端亢奋时兑现。</li>
<li><b><span class="tag">提醒</span>不要纯用「回撤止盈」（跌 X% 就跑）。</b>
它在熊市里很香（+10~17pct、胜率 91%），但在近三年这种急跌后反弹的行情里会被打脸
（只有 5%~7%，远不如不止盈）。它只适合你明确判断处于下行趋势时使用。</li>
</ul>
<div class="note"><b>必须说清楚的三点局限：</b>
<ul>
<li>本文所有数字都是<b>历史回测</b>，用真实净值 + 真实费率建模，但不代表未来。医药板块的政策、
集采、估值中枢都可能变化。</li>
<li>「近三年最优 = +25%」有<b>事后挑选</b>的成分——它是在同一个三年样本里选出来的。
第三节的 28 窗口检验正是为了量化这种风险：结论是它在震荡市稳定有效（胜率 80%），
但在牛市会严重踏空。<b>请不要把它当成放之四海皆准的参数。</b></li>
<li>回测假设：按当日净值成交、赎回按 FIFO 分档计费、止盈落袋资金按 1.5%/年计息、
未考虑申赎确认的时间差与限大额（当前单日限购 10 万元）、未考虑税费（个人买卖基金差价免征个人所得税）。</li>
</ul></div>
</div>

<hr>
<p class="dim" style="font-size:12px">
数据来源：天天基金官方历史净值（本地 <code>data/fund_history.db</code> · nav_history，003095 共 2403 条、
003096 共 888 条）；费率来自天天基金基金费率页（fundf10.eastmoney.com/jjfl_003095.html、jjfl_003096.html）。
回测脚本：<code>dca_backtest.py</code>（引擎）·<code>dca_report.py</code>（本报告）·
结果数据：<code>data/dca_result.json</code>。
本文为数据分析，不构成投资建议。</p>
</div></body></html>''')

    with open(OUT, "w", encoding="utf-8") as f:
        f.write("".join(S))
    print("written", OUT, os.path.getsize(OUT), "bytes")


if __name__ == "__main__":
    build()
