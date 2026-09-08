# -*- coding: utf-8 -*-
"""Charts for the FIFO swing-T report (light theme)."""
import sqlite3
import datetime

DB = 'data/fund_history.db'
con = sqlite3.connect(DB)
rows = con.execute(
    "SELECT trade_date,dwjz FROM nav_history WHERE fund_code='014414' "
    "AND dwjz IS NOT NULL ORDER BY trade_date").fetchall()
dates = [r[0] for r in rows]
nav = [r[1] for r in rows]

INK = '#1f2937'
SUB = '#6b7280'
GRID = '#e5e7eb'
RED = '#dc2626'          # up / gain  (CN convention)
GREEN = '#059669'        # down / loss (CN convention)
BLUE = '#2563eb'
AMBER = '#d97706'
BG = '#ffffff'
PANEL = '#f9fafb'


def head(w, h, title, sub):
    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %d %d" '
            'font-family="-apple-system,Segoe UI,Helvetica,Arial,sans-serif">'
            '<rect width="%d" height="%d" fill="%s"/>'
            '<text x="24" y="34" font-size="19" font-weight="700" fill="%s">%s</text>'
            '<text x="24" y="56" font-size="12.5" fill="%s">%s</text>'
            % (w, h, w, h, BG, INK, title, SUB, sub))


# ---------------------------------------------------------------- chart 1
# segment-end bias
W, H = 900, 400
s = head(W, H, '段末偏差：把每段最后几天砍掉，结论就反转',
         '底部段定义为「净值≤0.85」，涨破即结束 → 每段末尾天然自带上涨，系统性惩罚先卖后买')
bars = [('原始整段', -36857, 47), ('砍掉末5日', -2469, 37),
        ('砍掉末10日', 2505, 32), ('砍掉末20日', 4062, 18)]
x0, y0, bw, gap = 90, 300, 110, 78
lo, hi = -40000, 8000
sc = 190.0 / (hi - lo)
zero = y0 - (0 - lo) * sc
s += '<line x1="70" y1="%.1f" x2="%d" y2="%.1f" stroke="%s" stroke-width="1.4"/>' % (
    zero, W - 180, zero, SUB)
s += '<text x="%d" y="%.1f" font-size="11" fill="%s">0</text>' % (W - 176, zero + 4, SUB)
for i, (lab, val, ns) in enumerate(bars):
    cx = x0 + i * (bw + gap)
    top = y0 - (max(val, 0) - lo) * sc
    hgt = abs(val) * sc
    col = RED if val > 0 else GREEN
    s += '<rect x="%.1f" y="%.1f" width="%d" height="%.1f" fill="%s" rx="3" opacity="0.88"/>' % (
        cx, top, bw, max(hgt, 1.5), col)
    ty = top - 9 if val > 0 else top + hgt + 17
    s += '<text x="%.1f" y="%.1f" font-size="13" font-weight="700" fill="%s" text-anchor="middle">%s</text>' % (
        cx + bw / 2, ty, col, '{:+,d}'.format(val))
    s += '<text x="%.1f" y="%d" font-size="12" fill="%s" text-anchor="middle">%s</text>' % (
        cx + bw / 2, y0 + 34, INK, lab)
    s += '<text x="%.1f" y="%d" font-size="10.5" fill="%s" text-anchor="middle">%d次卖出</text>' % (
        cx + bw / 2, y0 + 51, SUB, ns)
s += ('<text x="24" y="%d" font-size="12" fill="%s">'
      '结论：原始整段 −36,857 元这个数字是偏差产物，不可用。无偏检验必须用固定长度滚动窗口。</text>'
      % (H - 22, AMBER))
s += '</svg>'
open('images/t_fifo_bias.svg', 'w', encoding='utf-8').write(s)
print('images/t_fifo_bias.svg')


# ---------------------------------------------------------------- chart 2
# accounting illusion: realized vs total
W, H = 900, 430
s = head(W, H, '「确保每笔都赚」的会计幻觉',
         '343 个 60 日底部窗口平均值。已实现全是正的，总资产全是负的——差额是卖飞的机会成本')
rowsd = [('机械 1%/1%', -2742, -2742, 0),
         ('锁定1% 卖1.0%', 4450, -2104, 55),
         ('锁定1% 卖1.5%', 2954, -1479, 58),
         ('锁定2% 卖1.0%', 4848, -2086, 60),
         ('锁定2% 卖1.5%', 3824, -483, 59)]
x0, y0 = 150, 300
bw, gap = 26, 148
lo, hi = -8000, 6000
sc = 200.0 / (hi - lo)
zero = y0 - (0 - lo) * sc
s += '<line x1="120" y1="%.1f" x2="%d" y2="%.1f" stroke="%s" stroke-width="1.4"/>' % (
    zero, W - 60, zero, SUB)
for i, (lab, rl, tt, unc) in enumerate(rowsd):
    cx = x0 + i * gap
    for j, (val, col, tag) in enumerate(((rl, BLUE, '已实现'), (tt, AMBER, '总资产'))):
        bx = cx + j * (bw + 8)
        top = y0 - (max(val, 0) - lo) * sc
        hgt = abs(val) * sc
        s += '<rect x="%.1f" y="%.1f" width="%d" height="%.1f" fill="%s" rx="2" opacity="0.85"/>' % (
            bx, top, bw, max(hgt, 1.5), col)
        ty = top - 7 if val > 0 else top + hgt + 15
        s += '<text x="%.1f" y="%.1f" font-size="10.5" font-weight="600" fill="%s" text-anchor="middle">%s</text>' % (
            bx + bw / 2, ty, col, '{:+,d}'.format(val))
    s += '<text x="%.1f" y="%d" font-size="11.5" fill="%s" text-anchor="middle">%s</text>' % (
        cx + bw + 4, y0 + 36, INK, lab)
    if unc:
        s += '<text x="%.1f" y="%d" font-size="10.5" font-weight="700" fill="%s" text-anchor="middle">%d%% 卖飞</text>' % (
            cx + bw + 4, y0 + 53, GREEN, unc)
    else:
        s += '<text x="%.1f" y="%d" font-size="10.5" fill="%s" text-anchor="middle">全部平仓</text>' % (
            cx + bw + 4, y0 + 53, SUB)
s += '<rect x="150" y="%d" width="14" height="11" fill="%s" opacity="0.85" rx="2"/>' % (H - 46, BLUE)
s += '<text x="170" y="%d" font-size="11.5" fill="%s">已实现价差（含费）</text>' % (H - 36, INK)
s += '<rect x="310" y="%d" width="14" height="11" fill="%s" opacity="0.85" rx="2"/>' % (H - 46, AMBER)
s += '<text x="330" y="%d" font-size="11.5" fill="%s">总资产 vs 躺平（真实结果）</text>' % (H - 36, INK)
s += ('<text x="24" y="%d" font-size="12" fill="%s">'
      '规则保证了「每笔平仓都赚」，但 55%%~60%% 的卖出永远等不到回落——亏损被搬到了机会成本里。</text>'
      % (H - 12, AMBER))
s += '</svg>'
open('images/t_fifo_illusion.svg', 'w', encoding='utf-8').write(s)
print('images/t_fifo_illusion.svg')


# ---------------------------------------------------------------- chart 3
# current bottom phase with fills
i0 = dates.index('2026-05-12')
seg_d = dates[i0:]
seg_v = nav[i0:]
W, H = 900, 440
s = head(W, H, '当前底部段实盘回放：4 笔卖飞发生在最低点附近',
         '2026-05-12 ~ 2026-09-03，净值 0.8391 → 0.7726（−7.93%）。规则：涨1%卖出，只在跌回1%时买入')
L, R, T, B = 70, 40, 90, 90
pw, ph = W - L - R, H - T - B
lo, hi = min(seg_v) * 0.985, max(seg_v) * 1.012
def X(i): return L + pw * i / (len(seg_v) - 1)
def Y(v): return T + ph * (hi - v) / (hi - lo)
for k in range(5):
    v = lo + (hi - lo) * k / 4
    s += '<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="%s" stroke-width="1"/>' % (
        L, Y(v), W - R, Y(v), GRID)
    s += '<text x="%d" y="%.1f" font-size="10.5" fill="%s" text-anchor="end">%.3f</text>' % (
        L - 8, Y(v) + 3.5, SUB, v)
pts = ' '.join('%.1f,%.1f' % (X(i), Y(seg_v[i])) for i in range(len(seg_v)))
s += '<polyline points="%s" fill="none" stroke="%s" stroke-width="1.9"/>' % (pts, INK)
fills = [('2026-05-15', 'S', 0.8338, 0), ('2026-05-18', 'B', 0.8151, 0),
         ('2026-06-02', 'S', 0.7632, 0), ('2026-06-03', 'B', 0.7552, 0),
         ('2026-06-15', 'S', 0.7328, 0), ('2026-06-16', 'B', 0.7239, 0),
         ('2026-06-30', 'S', 0.6969, 1), ('2026-07-02', 'S', 0.7375, 1),
         ('2026-07-07', 'S', 0.7519, 1), ('2026-07-13', 'S', 0.7422, 1)]
for d, side, px, stuck in fills:
    if d not in seg_d:
        continue
    i = seg_d.index(d)
    x, y = X(i), Y(px)
    if side == 'S':
        col = GREEN if not stuck else RED
        s += '<path d="M%.1f,%.1f l5.5,9.5 l-11,0 z" fill="%s"/>' % (x, y - 11, col)
        if stuck:
            s += '<circle cx="%.1f" cy="%.1f" r="8.5" fill="none" stroke="%s" stroke-width="2"/>' % (
                x, y, RED)
    else:
        s += '<path d="M%.1f,%.1f l5.5,-9.5 l-11,0 z" fill="%s"/>' % (x, y + 11, BLUE)
lastx, lasty = X(len(seg_v) - 1), Y(seg_v[-1])
s += '<circle cx="%.1f" cy="%.1f" r="4" fill="%s"/>' % (lastx, lasty, INK)
s += '<text x="%.1f" y="%.1f" font-size="11" font-weight="600" fill="%s" text-anchor="end">0.7726</text>' % (
    lastx - 8, lasty - 8, INK)
ilow = seg_v.index(min(seg_v))
s += '<text x="%.1f" y="%.1f" font-size="11" fill="%s" text-anchor="middle">低点 %.4f</text>' % (
    X(ilow), Y(min(seg_v)) + 22, SUB, min(seg_v))
for i, d in enumerate(seg_d):
    if d[8:10] == '01' or (i == 0):
        s += '<text x="%.1f" y="%d" font-size="10" fill="%s" text-anchor="middle">%s</text>' % (
            X(i), H - B + 22, SUB, d[5:7] + '月')
y = H - 44
s += '<path d="M150,%d l5.5,9.5 l-11,0 z" fill="%s"/>' % (y - 8, GREEN)
s += '<text x="166" y="%d" font-size="11.5" fill="%s">卖出并成功买回</text>' % (y + 2, INK)
s += '<path d="M310,%d l5.5,9.5 l-11,0 z" fill="%s"/><circle cx="315.5" cy="%d" r="8.5" fill="none" stroke="%s" stroke-width="2"/>' % (
    y - 8, RED, y - 2, RED)
s += '<text x="332" y="%d" font-size="11.5" fill="%s">卖出后再也没等到 −1%% → 卖飞</text>' % (y + 2, INK)
s += ('<text x="24" y="%d" font-size="12" fill="%s">'
      '6/30 在 0.6969（本轮最低 0.6829 附近）卖出后，净值一路涨到 0.7726 再没回落 1%%，'
      '4 笔共 20 万踏空。已实现 +2,223，总资产却比躺平少 9,019。</text>' % (H - 12, AMBER))
s += '</svg>'
open('images/t_fifo_current.svg', 'w', encoding='utf-8').write(s)
print('images/t_fifo_current.svg')
