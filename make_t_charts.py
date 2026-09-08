# -*- coding: utf-8 -*-
"""生成 做T分析 的三张 SVG 图表 (light theme)"""
import os

OUT = 'images'
os.makedirs(OUT, exist_ok=True)

INK = '#1f2937'
SUB = '#6b7280'
GRID = '#e5e7eb'
RED = '#dc2626'      # 成本/亏损
GREEN = '#16a34a'    # 收益
BLUE = '#2563eb'
AMBER = '#d97706'


def esc(s):
    return s.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def header(w, h, title, sub):
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="{w}" height="{h}" font-family="-apple-system,Segoe UI,Microsoft YaHei,sans-serif">
<rect width="{w}" height="{h}" fill="#ffffff"/>
<text x="24" y="34" font-size="17" font-weight="700" fill="{INK}">{esc(title)}</text>
<text x="24" y="56" font-size="12" fill="{SUB}">{esc(sub)}</text>
'''


# ============ 图1 成本对比 ============
def chart1():
    w, h = 680, 400
    groups = ['持有3天', '持有10天', '持有30天', '持有90天']
    data = {'A类场外 014414': [1.620, 0.120, 0.120, 0.120],
            'C类场外 014415': [1.503, 0.011, 0.033, 0.099],
            '场内ETF 516670': [0.020, 0.020, 0.020, 0.020]}
    colors = {'A类场外 014414': RED, 'C类场外 014415': AMBER, '场内ETF 516670': GREEN}
    ymax = 1.7
    x0, y0, pw, ph = 70, 90, 560, 250
    s = header(w, h, '图1  做T一次完整买卖的交易成本', '纵轴越低越好。持有<7天触发1.5%惩罚性赎回费 —— 这是短周期做T的死穴')
    # y grid
    for i in range(6):
        yv = ymax * i / 5
        y = y0 + ph - ph * i / 5
        s += f'<line x1="{x0}" y1="{y:.1f}" x2="{x0+pw}" y2="{y:.1f}" stroke="{GRID}" stroke-width="1"/>'
        s += f'<text x="{x0-10}" y="{y+4:.1f}" font-size="11" fill="{SUB}" text-anchor="end">{yv:.2f}%</text>'
    gw = pw / len(groups)
    bw = 34
    for gi, g in enumerate(groups):
        cx = x0 + gw * gi + gw / 2
        for bi, (name, vals) in enumerate(data.items()):
            v = vals[gi]
            bh = ph * v / ymax
            bx = cx - (len(data) * bw) / 2 + bi * bw + 4
            by = y0 + ph - bh
            s += (f'<rect x="{bx:.1f}" y="{by:.1f}" width="{bw-8}" height="{max(bh,1):.1f}" '
                  f'fill="{colors[name]}" rx="2"/>')
            s += (f'<text x="{bx+(bw-8)/2:.1f}" y="{by-5:.1f}" font-size="9.5" '
                  f'fill="{colors[name]}" text-anchor="middle" font-weight="600">{v:.3f}</text>')
        s += f'<text x="{cx:.1f}" y="{y0+ph+20}" font-size="12" fill="{INK}" text-anchor="middle">{esc(g)}</text>'
    # legend
    lx = x0
    for name, c in colors.items():
        s += f'<rect x="{lx}" y="{h-38}" width="12" height="12" fill="{c}" rx="2"/>'
        s += f'<text x="{lx+18}" y="{h-28}" font-size="11.5" fill="{INK}">{esc(name)}</text>'
        lx += 175
    s += f'<text x="{x0}" y="{y0+ph+44}" font-size="11" fill="{SUB}">管理费/托管费/销售服务费按日计提并已含在净值中，上表仅为交易环节成本</text>'
    s += '</svg>'
    return s


# ============ 图2 正T vs 倒T 年度超额 ============
def chart2():
    w, h = 680, 400
    years = ['2022\n震荡', '2023\n下跌', '2024\n下跌', '2025\n上涨', '2026\n下跌']
    pos = [-9.4, 5.7, 24.4, -1.7, -8.6]
    rev = [1.9, 2.3, 2.2, 0.8, 2.3]
    ymin, ymax = -14, 28
    x0, y0, pw, ph = 70, 95, 560, 235
    zy = y0 + ph * ymax / (ymax - ymin)

    def py(v):
        return y0 + ph - ph * (v - ymin) / (ymax - ymin)
    s = header(w, h, '图2  正T vs 倒T —— 逐年超额收益（相对买入持有）',
               '倒T每年稳定在 +0.8%~+2.3%；正T在 -9.4%~+24.4% 间剧烈摇摆，2026年反而亏 8.6%')
    for i in range(7):
        v = ymin + (ymax - ymin) * i / 6
        y = py(v)
        s += f'<line x1="{x0}" y1="{y:.1f}" x2="{x0+pw}" y2="{y:.1f}" stroke="{GRID}" stroke-width="1"/>'
        s += f'<text x="{x0-10}" y="{y+4:.1f}" font-size="11" fill="{SUB}" text-anchor="end">{v:+.0f}%</text>'
    s += f'<line x1="{x0}" y1="{zy:.1f}" x2="{x0+pw}" y2="{zy:.1f}" stroke="{INK}" stroke-width="1.5"/>'
    gw = pw / len(years)
    bw = 40
    for gi, yname in enumerate(years):
        cx = x0 + gw * gi + gw / 2
        for bi, (vals, c, lab) in enumerate(((pos, BLUE, '正T'), (rev, GREEN, '倒T'))):
            v = vals[gi]
            by = py(v) if v >= 0 else zy
            bh = abs(py(v) - zy)
            bx = cx - bw + bi * bw + 5
            yy = py(v) if v >= 0 else zy
            s += (f'<rect x="{bx:.1f}" y="{yy:.1f}" width="{bw-10}" height="{max(bh,1):.1f}" '
                  f'fill="{c}" rx="2"/>')
            ty = (yy - 6) if v >= 0 else (yy + bh + 14)
            s += (f'<text x="{bx+(bw-10)/2:.1f}" y="{ty:.1f}" font-size="10.5" '
                  f'fill="{c}" text-anchor="middle" font-weight="700">{v:+.1f}</text>')
        lines = yname.split('\n')
        s += f'<text x="{cx:.1f}" y="{y0+ph+20}" font-size="12" fill="{INK}" text-anchor="middle">{lines[0]}</text>'
        s += f'<text x="{cx:.1f}" y="{y0+ph+35}" font-size="10.5" fill="{SUB}" text-anchor="middle">{lines[1]}</text>'
    lx = x0
    for lab, c in (('正T 逢跌买入、涨了卖', BLUE), ('倒T 逢高卖出、跌回买', GREEN)):
        s += f'<rect x="{lx}" y="{h-30}" width="12" height="12" fill="{c}" rx="2"/>'
        s += f'<text x="{lx+18}" y="{h-20}" font-size="11.5" fill="{INK}">{esc(lab)}</text>'
        lx += 240
    s += '</svg>'
    return s


# ============ 图3 样本内 vs 样本外 ============
def chart3():
    w, h = 680, 360
    items = [('正T（逢跌买）', 19.8, -2.9, BLUE, '衰减 22.7pp'),
             ('倒T（逢高卖）', 4.7, 1.7, GREEN, '衰减 3.0pp')]
    ymin, ymax = -8, 24
    x0, y0, pw, ph = 90, 100, 480, 180
    zy = y0 + ph * ymax / (ymax - ymin)

    def py(v):
        return y0 + ph - ph * (v - ymin) / (ymax - ymin)
    s = header(w, h, '图3  过拟合检验 —— 样本内挑出的最优策略，放到样本外还行吗',
               '样本内=前60%时间(2022-04~2024-12)选参数；样本外=后40%(2024-12~2026-09)验证')
    for i in range(5):
        v = ymin + (ymax - ymin) * i / 4
        y = py(v)
        s += f'<line x1="{x0}" y1="{y:.1f}" x2="{x0+pw}" y2="{y:.1f}" stroke="{GRID}" stroke-width="1"/>'
        s += f'<text x="{x0-10}" y="{y+4:.1f}" font-size="11" fill="{SUB}" text-anchor="end">{v:+.0f}%</text>'
    s += f'<line x1="{x0}" y1="{zy:.1f}" x2="{x0+pw}" y2="{zy:.1f}" stroke="{INK}" stroke-width="1.5"/>'
    gw = pw / len(items)
    for gi, (name, isv, oosv, c, note) in enumerate(items):
        cx = x0 + gw * gi + gw / 2
        for bi, (v, lab) in enumerate(((isv, '样本内'), (oosv, '样本外'))):
            bx = cx - 70 + bi * 78
            bh = abs(py(v) - zy)
            yy = py(v) if v >= 0 else zy
            s += (f'<rect x="{bx}" y="{yy:.1f}" width="58" height="{max(bh,1):.1f}" '
                  f'fill="{c}" rx="2" opacity="{1.0 if bi==0 else 0.55}"/>')
            ty = (yy - 8) if v >= 0 else (yy + bh + 16)
            s += (f'<text x="{bx+29}" y="{ty:.1f}" font-size="12" fill="{c}" '
                  f'text-anchor="middle" font-weight="700">{v:+.1f}%</text>')
            s += f'<text x="{bx+29}" y="{y0+ph+20}" font-size="11.5" fill="{INK}" text-anchor="middle">{lab}</text>'
        s += f'<text x="{cx:.1f}" y="{y0+ph+40}" font-size="12.5" fill="{c}" text-anchor="middle" font-weight="700">{esc(name)}</text>'
        s += f'<text x="{cx:.1f}" y="{y0+ph+57}" font-size="11" fill="{SUB}" text-anchor="middle">{esc(note)}</text>'
    s += (f'<text x="{x0}" y="{h-42}" font-size="11.5" fill="{INK}">'
          f'正T：33个策略在样本外，百分位&gt;95%% 的有 0 个，超过一半不如随机买入 —— 样本内的好战绩全是数据窥探</text>')
    s += (f'<text x="{x0}" y="{h-24}" font-size="11.5" fill="{INK}">'
          f'倒T：27组参数在样本外，78%% 超额为正，中位 +1.1%%（年化约 +0.64%%）—— 与全样本中位 +0.63%% 几乎一致</text>')
    s += '</svg>'
    return s


for fn, maker in (('t_cost.svg', chart1), ('t_yearly.svg', chart2), ('t_oos.svg', chart3)):
    p = os.path.join(OUT, fn)
    with open(p, 'w', encoding='utf-8') as f:
        f.write(maker())
    print('wrote', p, os.path.getsize(p), 'bytes')
