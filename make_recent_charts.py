# -*- coding: utf-8 -*-
"""Charts for the last-month T-trading analysis of 014414."""
import sqlite3
import statistics
from collections import OrderedDict

DB = 'data/fund_history.db'
CODE = '014414'
CAP = 270000.0
LOT = 8
COST_ETF = 0.0002
BUY_OTC = 0.0012
SELL_OTC = 0.0

con = sqlite3.connect(DB)
rows = con.execute(
    'SELECT trade_date, dwjz FROM nav_history WHERE fund_code=? AND dwjz IS NOT NULL '
    'ORDER BY trade_date', (CODE,)).fetchall()
con.close()
all_dates = [r[0] for r in rows]
all_nav = [r[1] for r in rows]
gret = {i: (all_nav[i] / all_nav[i - 1] - 1) * 100 for i in range(1, len(all_nav))}


def run_window(idxs, a, b, mode, cb, cs):
    nav0 = all_nav[idxs[0]]
    shares0 = CAP / nav0
    sl = shares0 / LOT
    if mode == 'long':
        cash = sl * nav0 * 3
        shares = shares0
        baseline = shares0 * all_nav[idxs[-1]] + sl * nav0 * 3
    else:
        cash = 0.0
        shares = shares0
        baseline = shares0 * all_nav[idxs[-1]]
    book = []
    realized = 0.0
    fills = []

    def apply(d, sh, px, day):
        nonlocal realized
        eff = px * (1 + cb) if d > 0 else px * (1 - cs)
        need = sh
        while need > 1e-9 and book and book[0][0] == -d:
            ls, lp = book[0][1], book[0][2]
            take = min(ls, need)
            realized += take * (eff - lp) * (-d)
            book[0][1] -= take
            need -= take
            if book[0][1] <= 1e-9:
                book.pop(0)
        if need > 1e-9:
            book.append([d, need, eff])
        fills.append((day, 'BUY' if d > 0 else 'SELL', px))

    for i in idxs:
        if i not in gret:
            continue
        r = gret[i]
        px = all_nav[i]
        if mode == 'reverse':
            sig = 'S' if r >= a else ('B' if r <= -b else None)
        else:
            sig = 'B' if r <= -a else ('S' if r >= b else None)
        if sig == 'B':
            cost = sl * px * (1 + cb)
            if cash >= cost:
                shares += sl; cash -= cost; apply(+1, sl, px, all_dates[i])
        elif sig == 'S':
            ok = shares >= sl if mode == 'reverse' else (shares - shares0 >= sl - 1e-9)
            if ok:
                shares -= sl; cash += sl * px * (1 - cs); apply(-1, sl, px, all_dates[i])

    end_nav = all_nav[idxs[-1]]
    unreal = sum(sh * (end_nav - px) if d > 0 else sh * (px - end_nav) for d, sh, px in book)
    total = shares * end_nav + cash - baseline
    return realized, unreal, total, fills, shares0


def rolling_windows(win=22):
    out, end = [], len(all_dates)
    while end - win >= 0:
        out.append(list(range(end - win, end)))
        end -= win
    return list(reversed(out))


WIN = 22
wins = rolling_windows(WIN)
last = wins[-1]

# per-window totals for the two headline strategies
rev_totals, long_totals, labels = [], [], []
for w in wins:
    r1 = run_window(w, 0.5, 0.5, 'reverse', COST_ETF, COST_ETF)
    r2 = run_window(w, 0.5, 1.0, 'long', COST_ETF, COST_ETF)
    rev_totals.append(r1[2]); long_totals.append(r2[2])
    labels.append(all_dates[w[-1]][:7])

last_rev = run_window(last, 0.5, 0.5, 'reverse', COST_ETF, COST_ETF)
last_rev_otc = run_window(last, 0.5, 0.5, 'reverse', BUY_OTC, SELL_OTC)
last_long = run_window(last, 0.5, 1.0, 'long', COST_ETF, COST_ETF)

print('last window %s ~ %s' % (all_dates[last[0]], all_dates[last[-1]]))
print('rev ETF  real %.0f unreal %.0f total %.0f' % (last_rev[0], last_rev[1], last_rev[2]))
print('rev OTC  real %.0f unreal %.0f total %.0f' % (last_rev_otc[0], last_rev_otc[1], last_rev_otc[2]))
print('long ETF real %.0f unreal %.0f total %.0f' % (last_long[0], last_long[1], last_long[2]))
print('rev fills:', last_rev[3])

# ------------------------------------------------------------------ chart 1
W, H = 900, 420
PL, PR, PT, PB = 62, 24, 40, 56
pw, ph = W - PL - PR, H - PT - PB
vs = all_nav[last[0]:last[-1] + 1]
vmin, vmax = min(vs), max(vs)
pad = (vmax - vmin) * 0.18 or 0.01
lo, hi = vmin - pad, vmax + pad


def X(i):
    return PL + pw * i / (len(vs) - 1)


def Y(v):
    return PT + ph * (hi - v) / (hi - lo)


pts = ' '.join('%.1f,%.1f' % (X(i), Y(v)) for i, v in enumerate(vs))
area = '%s %.1f,%.1f %.1f,%.1f' % (pts, X(len(vs) - 1), Y(lo), X(0), Y(lo))

sv = []
for i in range(len(vs)):
    sv.append('<text x="%.1f" y="%.1f" font-size="10" fill="#64748b" text-anchor="middle">%s</text>'
              % (X(i), H - 24, all_dates[last[0] + i][5:]))
    if i % 2 == 0 or i == len(vs) - 1:
        sv.append('<text x="%.1f" y="%.1f" font-size="10" fill="#94a3b8" text-anchor="middle">%s</text>'
                  % (X(i), H - 8, all_dates[last[0] + i][:7]))
grid = []
for k in range(5):
    v = lo + (hi - lo) * k / 4
    y = Y(v)
    grid.append('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="#e2e8f0" stroke-width="1"/>' % (PL, y, PL + pw, y))
    grid.append('<text x="%.1f" y="%.1f" font-size="11" fill="#94a3b8" text-anchor="end">%.4f</text>' % (PL - 8, y + 4, v))

marks = []
fill_by_day = {d: s for d, s, _ in last_rev[3]}
for i, v in enumerate(vs):
    day = all_dates[last[0] + i]
    if day in fill_by_day:
        s = fill_by_day[day]
        up = (s == 'BUY')
        cy = Y(v) + (16 if up else -16)
        col = '#dc2626' if up else '#16a34a'
        marks.append('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="%s" stroke-width="1.4" stroke-dasharray="3,2"/>'
                     % (X(i), Y(v), X(i), cy, col))
        marks.append('<circle cx="%.1f" cy="%.1f" r="4.5" fill="%s" stroke="#fff" stroke-width="1.5"/>' % (X(i), Y(v), col))
        marks.append('<text x="%.1f" y="%.1f" font-size="10.5" font-weight="700" fill="%s" text-anchor="middle">%s</text>'
                     % (X(i), cy + (12 if up else -4), col, '买' if up else '卖'))

svg1 = f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" font-family="system-ui,-apple-system,'Segoe UI',sans-serif">
<rect width="{W}" height="{H}" fill="#ffffff"/>
<text x="{PL}" y="26" font-size="16" font-weight="700" fill="#0f172a">近一月实际买卖点：倒T 0.5%/0.5%（昨涨≥0.5%卖，昨跌≥0.5%买）</text>
<text x="{PL}" y="45" font-size="12" fill="#64748b">{all_dates[last[0]]} ~ {all_dates[last[-1]]}　22个交易日　红点买 / 绿点卖　底仓27万分8份</text>
{''.join(grid)}
<polygon points="{area}" fill="#eff6ff" opacity="0.75"/>
<polyline points="{pts}" fill="none" stroke="#2563eb" stroke-width="2.2" stroke-linejoin="round"/>
{''.join(marks)}
{''.join(sv)}
<g transform="translate({PL + pw - 250},{PT + 6})">
<rect width="250" height="46" rx="6" fill="#f8fafc" stroke="#e2e8f0"/>
<circle cx="16" cy="17" r="4.5" fill="#dc2626"/><text x="28" y="21" font-size="11.5" fill="#334155">买入（昨日跌≥0.5%，今日按收盘净值成交）</text>
<circle cx="16" cy="34" r="4.5" fill="#16a34a"/><text x="28" y="38" font-size="11.5" fill="#334155">卖出（昨日涨≥0.5%，今日按收盘净值成交）</text>
</g>
</svg>'''
open('images/t_recent_fills.svg', 'w', encoding='utf-8').write(svg1)

# ------------------------------------------------------------------ chart 2 (distribution)
W2, H2 = 900, 400
PL2, PR2, PT2, PB2 = 62, 24, 54, 64
bw = W2 - PL2 - PR2
bh = H2 - PT2 - PB2
allv = rev_totals + long_totals
amin, amax = min(allv), max(allv)
NB = 26
step = (amax - amin) / NB
bins = [0] * NB
for v in rev_totals:
    bins[min(NB - 1, int((v - amin) / step))] += 1
maxc = max(bins)
bars = []
for k, c in enumerate(bins):
    x0 = amin + k * step
    h = bh * c / maxc if maxc else 0
    y = PT2 + bh - h
    x = PL2 + bw * (x0 - amin) / (amax - amin)
    wbar = bw / NB - 2
    col = '#16a34a' if x0 >= 0 else '#dc2626'
    bars.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{wbar:.1f}" height="{h:.1f}" fill="{col}" opacity="0.78" rx="2"/>')
    if c:
        bars.append(f'<text x="{x + wbar/2:.1f}" y="{y - 5:.1f}" font-size="10" fill="#475569" text-anchor="middle">{c}</text>')

zt = PL2 + bw * (0 - amin) / (amax - amin)
med = statistics.median(rev_totals)
medx = PL2 + bw * (med - amin) / (amax - amin)
meanv = statistics.mean(rev_totals)
meanx = PL2 + bw * (meanv - amin) / (amax - amin)
ticks = []
for k in range(9):
    v = amin + (amax - amin) * k / 8
    x = PL2 + bw * k / 8
    ticks.append(f'<line x1="{x:.1f}" y1="{PT2+bh}" x2="{x:.1f}" y2="{PT2+bh+5}" stroke="#94a3b8"/>')
    ticks.append(f'<text x="{x:.1f}" y="{PT2+bh+19}" font-size="10.5" fill="#64748b" text-anchor="middle">{v/1000:+.1f}k</text>')

svg2 = f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W2} {H2}" width="{W2}" font-family="system-ui,-apple-system,'Segoe UI',sans-serif">
<rect width="{W2}" height="{H2}" fill="#ffffff"/>
<text x="{PL2}" y="26" font-size="16" font-weight="700" fill="#0f172a">倒T 0.5/0.5 在全部48个滚动月窗口的盈亏分布（含未平仓，27万底仓）</text>
<text x="{PL2}" y="45" font-size="12" fill="#64748b">绿＝赚钱的窗口，红＝亏钱的窗口。看形状而不是看最高柱：右偏但拖着一条长左尾。</text>
<line x1="{PL2}" y1="{PT2}" x2="{PL2}" y2="{PT2+bh}" stroke="#cbd5e1"/>
{''.join(bars)}
{''.join(ticks)}
<line x1="{zt:.1f}" y1="{PT2}" x2="{zt:.1f}" y2="{PT2+bh}" stroke="#0f172a" stroke-width="1.6"/>
<text x="{zt:.1f}" y="{PT2-8}" font-size="11" font-weight="700" fill="#0f172a" text-anchor="middle">盈亏平衡</text>
<line x1="{medx:.1f}" y1="{PT2}" x2="{medx:.1f}" y2="{PT2+bh}" stroke="#2563eb" stroke-width="1.6" stroke-dasharray="5,3"/>
<text x="{medx:.1f}" y="{PT2+bh+34}" font-size="11" font-weight="700" fill="#2563eb" text-anchor="middle">中位 {med:+,.0f}</text>
<line x1="{meanx:.1f}" y1="{PT2}" x2="{meanx:.1f}" y2="{PT2+bh}" stroke="#ea580c" stroke-width="1.6" stroke-dasharray="2,3"/>
<text x="{meanx:.1f}" y="{PT2+bh+48}" font-size="11" font-weight="700" fill="#ea580c" text-anchor="middle">均值 {meanv:+,.0f}</text>
<text x="{PL2}" y="{H2-8}" font-size="11" fill="#94a3b8">横轴＝该窗口做T相对"躺平不动"的增量盈亏（元）　纵轴＝窗口个数</text>
</svg>'''
open('images/t_recent_dist.svg', 'w', encoding='utf-8').write(svg2)

# ------------------------------------------------------------------ chart 3 (by year)
by_year = OrderedDict()
for lb, v in zip(labels, rev_totals):
    by_year.setdefault(lb[:4], []).append(v)
years = sorted(by_year)
W3, H3 = 900, 360
PL3, PT3 = 70, 54
pw3, ph3 = W3 - PL3 - 30, H3 - PT3 - 70
vals = [sum(by_year[y]) for y in years]
avmax = max(max(vals), abs(min(vals))) or 1
zero = PT3 + ph3 / 2
bars3, tk3 = [], []
slot = pw3 / len(years)
for i, (y, s) in enumerate(zip(years, vals)):
    h = abs(s) / avmax * (ph3 / 2 - 10)
    x = PL3 + slot * i + slot * 0.22
    w = slot * 0.56
    if s >= 0:
        bars3.append(f'<rect x="{x:.1f}" y="{zero - h:.1f}" width="{w:.1f}" height="{h:.1f}" fill="#16a34a" opacity="0.8" rx="3"/>')
        bars3.append(f'<text x="{x + w/2:.1f}" y="{zero - h - 7:.1f}" font-size="11.5" font-weight="700" fill="#15803d" text-anchor="middle">{s:+,.0f}</text>')
    else:
        bars3.append(f'<rect x="{x:.1f}" y="{zero:.1f}" width="{w:.1f}" height="{h:.1f}" fill="#dc2626" opacity="0.8" rx="3"/>')
        bars3.append(f'<text x="{x + w/2:.1f}" y="{zero + h + 16:.1f}" font-size="11.5" font-weight="700" fill="#b91c1c" text-anchor="middle">{s:+,.0f}</text>')
    tk3.append(f'<text x="{x + w/2:.1f}" y="{zero + ph3/2 + 26:.1f}" font-size="12.5" font-weight="600" fill="#334155" text-anchor="middle">{y}</text>')

svg3 = f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W3} {H3}" width="{W3}" font-family="system-ui,-apple-system,'Segoe UI',sans-serif">
<rect width="{W3}" height="{H3}" fill="#ffffff"/>
<text x="{PL3}" y="26" font-size="16" font-weight="700" fill="#0f172a">倒T 分年度合计盈亏：不是每年都赚</text>
<text x="{PL3}" y="45" font-size="12" fill="#64748b">2022年猪价单边上涨，倒T被反复打脸，一年亏掉 15,877 元——这就是最大的风险</text>
<line x1="{PL3}" y1="{zero:.1f}" x2="{PL3+pw3:.1f}" y2="{zero:.1f}" stroke="#0f172a" stroke-width="1.4"/>
{''.join(bars3)}
{''.join(tk3)}
<text x="{PL3}" y="{H3-10}" font-size="11" fill="#94a3b8">单位：元（相对同期躺平不动的增量）　27万底仓 / 8份 / 场内ETF成本0.02%</text>
</svg>'''
open('images/t_recent_yearly.svg', 'w', encoding='utf-8').write(svg3)

print('charts written')
print('rev totals: mean %.0f median %.0f win%% %.0f%%'
      % (statistics.mean(rev_totals), statistics.median(rev_totals),
         100 * sum(1 for x in rev_totals if x > 0) / len(rev_totals)))
print('long totals: mean %.0f median %.0f win%% %.0f%%'
      % (statistics.mean(long_totals), statistics.median(long_totals),
         100 * sum(1 for x in long_totals if x > 0) / len(long_totals)))
print('yearly:', {y: round(sum(by_year[y])) for y in years})
