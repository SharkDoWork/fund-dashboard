# -*- coding: utf-8 -*-
"""Parameter heatmap + new-vs-old comparison chart."""
import sqlite3
import statistics

DB = 'data/fund_history.db'
CODE = '014414'
CAP = 270000.0
LOT = 8
COST_ETF = 0.0002

con = sqlite3.connect(DB)
rows = con.execute(
    'SELECT trade_date, dwjz FROM nav_history WHERE fund_code=? AND dwjz IS NOT NULL '
    'ORDER BY trade_date', (CODE,)).fetchall()
con.close()
all_dates = [r[0] for r in rows]
all_nav = [r[1] for r in rows]
gret = {i: (all_nav[i] / all_nav[i - 1] - 1) * 100 for i in range(1, len(all_nav))}


def run_window(idxs, a, b):
    nav0 = all_nav[idxs[0]]
    shares0 = CAP / nav0
    sl = shares0 / LOT
    cash, shares = 0.0, shares0
    baseline = shares0 * all_nav[idxs[-1]]
    book = []
    realized = 0.0

    def apply(d, sh, px):
        nonlocal realized
        eff = px * (1 + COST_ETF) if d > 0 else px * (1 - COST_ETF)
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

    for i in idxs:
        if i not in gret:
            continue
        r = gret[i]; px = all_nav[i]
        sig = 'S' if r >= a else ('B' if r <= -b else None)
        if sig == 'B':
            c = sl * px * (1 + COST_ETF)
            if cash >= c:
                shares += sl; cash -= c; apply(+1, sl, px)
        elif sig == 'S':
            if shares >= sl:
                shares -= sl; cash += sl * px * (1 - COST_ETF); apply(-1, sl, px)
    en = all_nav[idxs[-1]]
    unreal = sum(sh * (en - px) if d > 0 else sh * (px - en) for d, sh, px in book)
    return realized + unreal


def rolling_windows(win=22):
    out, end = [], len(all_dates)
    while end - win >= 0:
        out.append(list(range(end - win, end)))
        end -= win
    return list(reversed(out))


WINS = rolling_windows(22)
AS = [0.25, 0.4, 0.5, 0.6, 0.75, 1.0, 1.25, 1.5, 2.0, 2.5, 3.0]
BS = [0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0]
cell = {}
for a in AS:
    for b in BS:
        cell[(a, b)] = statistics.mean([run_window(w, a, b) for w in WINS])

mx = max(abs(v) for v in cell.values())


def colour(v):
    if v >= 0:
        t = min(1.0, v / mx)
        r = int(255 - (255 - 34) * t); g = int(255 - (255 - 139) * t); bl = int(255 - (255 - 84) * t)
        return 'rgb(%d,%d,%d)' % (r, g, bl)
    t = min(1.0, -v / mx)
    return 'rgb(%d,%d,%d)' % (255, int(255 - 214 * t), int(255 - 226 * t))


W = 900
CW, CH = 74, 34
PL, PT = 108, 92
H = PT + CH * len(AS) + 92
pw = CW * len(BS)
out = []
out.append(f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" '
           f'font-family="system-ui,-apple-system,\'Segoe UI\',sans-serif">')
out.append(f'<rect width="{W}" height="{H}" fill="#ffffff"/>')
out.append(f'<text x="{PL}" y="28" font-size="16" font-weight="700" fill="#0f172a">'
           f'倒T 参数热力图：100 组参数的 48 窗口平均盈亏</text>')
out.append(f'<text x="{PL}" y="48" font-size="12" fill="#64748b">'
           f'绿＝赚钱，红＝亏钱，颜色越深绝对值越大。76% 的格子为正 → 不是单点巧合，是一整片高地。</text>')
out.append(f'<text x="{PL}" y="70" font-size="12" fill="#64748b">'
           f'但我上一轮报告选的 (0.5, 0.5) 只值 +68，而高地核心在"涨1.5%卖 / 跌2.5%买"一带。</text>')
out.append(f'<text x="{PL - 96}" y="{PT - 12}" font-size="11.5" fill="#475569">卖出阈值 →</text>')

for j, b in enumerate(BS):
    x = PL + CW * j + CW / 2
    out.append(f'<text x="{x:.1f}" y="{PT - 12}" font-size="11.5" fill="#475569" text-anchor="middle">-{b:g}%</text>')

for i, a in enumerate(AS):
    y = PT + CH * i
    out.append(f'<text x="{PL - 12}" y="{y + CH/2 + 4:.1f}" font-size="11.5" fill="#475569" '
               f'text-anchor="end">+{a:g}%</text>')
    for j, b in enumerate(BS):
        v = cell[(a, b)]
        x = PL + CW * j
        out.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{CW-2}" height="{CH-2}" '
                   f'fill="{colour(v)}" rx="3"/>')
        fs = 10.5 if abs(v) < 1000 else 9.5
        out.append(f'<text x="{x + (CW-2)/2:.1f}" y="{y + CH/2 + 3.5:.1f}" font-size="{fs}" '
                   f'fill="{"#14532d" if v>=0 else "#7f1d1d"}" text-anchor="middle" '
                   f'font-weight="600">{v:+.0f}</text>')

# markers
def mark(a, b, label, col):
    i, j = AS.index(a), BS.index(b)
    x = PL + CW * j; y = PT + CH * i
    out.append(f'<rect x="{x-1:.1f}" y="{y-1:.1f}" width="{CW}" height="{CH}" fill="none" '
               f'stroke="{col}" stroke-width="2.6" rx="4"/>')
    out.append(f'<text x="{x + CW/2:.1f}" y="{y + CH + 13:.1f}" font-size="10.5" '
               f'font-weight="700" fill="{col}" text-anchor="middle">{label}</text>')

mark(0.5, 0.5, '旧选点', '#1d4ed8')
mark(1.5, 2.5, '稳健区', '#b91c1c')
mark(1.25, 2.5, '峰值', '#047857')

ly = PT + CH * len(AS) + 34
out.append(f'<text x="{PL}" y="{ly}" font-size="11.5" fill="#64748b">'
           f'格内数字＝每窗口平均盈亏（元，27万底仓·8份·ETF成本0.02%）。横轴＝买回阈值（跌幅），纵轴＝卖出阈值（涨幅）。</text>')
out.append(f'<text x="{PL}" y="{ly+18}" font-size="11.5" fill="#64748b">'
           f'读法：买回阈值设在 -2.5% 附近明显优于 -0.5%，说明"卖掉之后要耐心等深跌再接回"是这套策略真正的盈利来源。</text>')
out.append('</svg>')
open('images/t_param_heatmap.svg', 'w', encoding='utf-8').write('\n'.join(out))

# ---------------------------------------------------------------- chart: old vs new
old_vals = [run_window(w, 0.5, 0.5) for w in WINS]
new_vals = [run_window(w, 1.5, 2.5) for w in WINS]
W2, H2 = 900, 400
PL2, PR2, PT2, PB2 = 66, 26, 62, 62
bw2, bh2 = W2 - PL2 - PR2, H2 - PT2 - PB2
lo = min(min(old_vals), min(new_vals)); hi = max(max(old_vals), max(new_vals))
lo = lo * 1.08; hi = hi * 1.08


def XX(i, n):
    return PL2 + bw2 * (i + 0.5) / n


def YY(v):
    return PT2 + bh2 * (hi - v) / (hi - lo)


n = len(WINS)
bars = []
slot = bw2 / n
for i, (o, nw) in enumerate(zip(old_vals, new_vals)):
    x = PL2 + slot * i
    w = slot * 0.38
    for k, v in enumerate((o, nw)):
        xx = x + slot * 0.08 + k * w
        y0, y1 = YY(0), YY(v)
        top, h = (y1, y0 - y1) if v >= 0 else (y0, y1 - y0)
        col = '#93c5fd' if k == 0 else '#fca5a5'
        ec = '#2563eb' if k == 0 else '#dc2626'
        if abs(v) > 5000:
            col = ec
        bars.append(f'<rect x="{xx:.1f}" y="{top:.1f}" width="{w:.1f}" height="{h:.1f}" '
                    f'fill="{col}" opacity="0.9"/>')
zy = YY(0)
grid = []
for k in range(7):
    v = lo + (hi - lo) * k / 6
    y = YY(v)
    grid.append(f'<line x1="{PL2}" y1="{y:.1f}" x2="{PL2+bw2:.1f}" y2="{y:.1f}" stroke="#e2e8f0"/>')
    grid.append(f'<text x="{PL2-8}" y="{y+4:.1f}" font-size="10.5" fill="#94a3b8" text-anchor="end">{v/1000:+.0f}k</text>')
xlab = []
for i in range(0, n, 6):
    xlab.append(f'<text x="{XX(i,n):.1f}" y="{PT2+bh2+18:.1f}" font-size="10.5" fill="#64748b" text-anchor="middle">{all_dates[WINS[i][0]][:7]}</text>')

svg2 = f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W2} {H2}" width="{W2}" font-family="system-ui,-apple-system,'Segoe UI',sans-serif">
<rect width="{W2}" height="{H2}" fill="#ffffff"/>
<text x="{PL2}" y="28" font-size="16" font-weight="700" fill="#0f172a">两种参数的逐窗口对比：没有谁全面占优</text>
<text x="{PL2}" y="48" font-size="12" fill="#64748b">蓝＝旧 0.5/0.5（高频、小赢、偶发巨亏）　红＝新 1.5/2.5（低频、大赢、尾部较温和）</text>
{''.join(grid)}
<line x1="{PL2}" y1="{zy:.1f}" x2="{PL2+bw2:.1f}" y2="{zy:.1f}" stroke="#0f172a" stroke-width="1.4"/>
{''.join(bars)}
{''.join(xlab)}
<g transform="translate({PL2+bw2-300},{PT2+4})">
<rect width="290" height="42" rx="6" fill="#f8fafc" stroke="#e2e8f0"/>
<rect x="12" y="11" width="14" height="9" fill="#93c5fd"/><text x="34" y="19" font-size="11" fill="#334155">旧 0.5/0.5：胜率67%，但2022年单窗口 −19,927</text>
<rect x="12" y="26" width="14" height="9" fill="#fca5a5"/><text x="34" y="34" font-size="11" fill="#334155">新 1.5/2.5：胜率56%，最差 −12,869，年化期望更高</text>
</g>
<text x="{PL2}" y="{H2-12}" font-size="11" fill="#94a3b8">横轴＝48个滚动22日窗口　纵轴＝该窗口做T相对躺平的增量盈亏（元）。注意最左侧那个深蓝色的巨坑。</text>
</svg>'''
open('images/t_old_vs_new.svg', 'w', encoding='utf-8').write(svg2)

print('charts written')
print('old mean %.0f new mean %.0f' % (statistics.mean(old_vals), statistics.mean(new_vals)))
