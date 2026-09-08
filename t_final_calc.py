# -*- coding: utf-8 -*-
"""倒T在 A类场外 / C类场外 / 场内ETF 三种工具下的净收益对比 + 生成SVG图表"""
import sqlite3, statistics, datetime, os

DB = 'data/fund_history.db'
CODE = '014414'
A_BUY = 0.0012
SELL_S, SELL_L = 0.015, 0.0
C_SVC = 0.004
ETF_C = 0.0002
CAP = 270000.0


def load():
    con = sqlite3.connect(DB)
    rows = con.execute(
        "SELECT trade_date, dwjz FROM nav_history WHERE fund_code=? "
        "AND dwjz IS NOT NULL ORDER BY trade_date", (CODE,)).fetchall()
    con.close()
    return ([datetime.date.fromisoformat(r[0]) for r in rows],
            [r[1] for r in rows])


def ma(navs, i, w):
    return sum(navs[i + 1 - w:i + 1]) / w if i + 1 >= w else None


def fee(hd):
    return SELL_S if hd < 7 else SELL_L


def reverse_t(dates, navs, up, back, w, tool, sell_frac=0.2, lo=0, hi=None):
    if hi is None:
        hi = len(navs)
    ma20 = [ma(navs, i, 20) for i in range(len(navs))]
    shares = 1.0
    pending = None
    trades = []
    i = max(lo + 21, 21)
    while i < hi - 1:
        if pending is None:
            m = ma20[i - 1]
            if m and navs[i - 1] >= m * (1 + up / 100.0):
                sell_sh = shares * sell_frac
                hd = (dates[i] - dates[lo]).days
                sf = fee(hd)
                cost = sf if tool != 'ETF' else ETF_C / 2
                cash = sell_sh * navs[i] * (1 - cost)
                shares -= sell_sh
                pending = (i, navs[i], cash)
        else:
            si, sp, amt = pending
            waited = i - si
            if (navs[i] <= sp * (1 - back / 100.0) and waited >= 5) \
                    or waited >= w or i == hi - 2:
                bf = A_BUY if tool == 'A' else (0.0 if tool == 'C' else ETF_C / 2)
                shares += amt * (1 - bf) / navs[i]
                trades.append({'ret': (sp / navs[i] - 1) * 100})
                pending = None
        i += 1
    final = shares * navs[hi - 1] + (pending[2] if pending else 0.0)
    bh = navs[hi - 1] / navs[lo]
    return trades, final, bh


def main():
    dates, navs = load()
    n = len(navs)
    yrs = (dates[-1] - dates[0]).days / 365.25
    print('=' * 88)
    print('倒T 在不同交易工具下的净收益  本金 %.0f 元  区间 %.2f 年' % (CAP, yrs))
    print('=' * 88)
    print('%-14s %6s %8s %11s %11s %12s' %
          ('工具', '次数', '成功率', '全期超额', '年化超额', '折合每年(元)'))
    rows = []
    for tool, name in (('A', 'A类场外'), ('C', 'C类场外'), ('ETF', '场内ETF')):
        tr, fin, bh = reverse_t(dates, navs, 3, 5, 20, tool)
        rs = [t['ret'] for t in tr]
        exc = (fin / bh - 1) * 100
        ann = ((fin / bh) ** (1 / yrs) - 1) * 100
        rows.append((name, len(tr), 100.0 * sum(1 for x in rs if x > 0) / len(rs),
                     exc, ann, CAP * ann / 100))
        print('%-14s %6d %7.1f%% %+10.1f%% %+10.2f%% %11.0f' %
              (name, len(tr), rows[-1][2], exc, ann, CAP * ann / 100))

    print('\n  注: 已扣各工具的交易成本。但上表未计"运作费差异":')
    print('      A/C类场外运作费 0.6%%/年(A) 或 1.0%%/年(C); 场内ETF 0.3%%/年。')
    print('      这部分已含在净值中, 故场外A类与ETF用的是同一条净值序列,')
    print('      实际场内ETF还能额外省下 0.3%%/年的运作费 = %.0f 元/年。' % (CAP * 0.003))

    # A/C 临界
    print('\n  A类 vs C类 临界: 每年做T次数 N, A类申购费0.12%%×N vs C类销售服务费0.4%%')
    print('  N > %.1f 次/年 → C 类更省' % (0.4 / 0.12))

    # 倒T vs 不做的最终金额
    tr, fin, bh = reverse_t(dates, navs, 3, 5, 20, 'A')
    base_end = CAP * bh
    t_end = CAP * fin
    print('\n【27万本金全期实测】(A类, 高3%%卖/回5%%买/等20日)')
    print('  一直持有到底      : %.0f 元  (%+.2f%%)' % (base_end, (bh - 1) * 100))
    print('  做倒T            : %.0f 元  (%+.2f%%)' % (t_end, (fin - 1) * 100))
    print('  倒T多赚          : %.0f 元  = 每年 %.0f 元' %
          (t_end - base_end, (t_end - base_end) / yrs))

    # 敏感度: 不同参数下的年化超额范围
    print('\n【参数稳健性】倒T 36组参数的年化超额分布(A类)')
    outs = []
    for up in (2, 3, 5, 8):
        for back in (2, 3, 5):
            for w in (10, 20, 40):
                t, f, b = reverse_t(dates, navs, up, back, w, 'A')
                if t:
                    outs.append(((f / b) ** (1 / yrs) - 1) * 100)
    outs.sort()
    print('  样本%d  最差%+.2f%%  P25%+.2f%%  中位%+.2f%%  P75%+.2f%%  最好%+.2f%%'
          % (len(outs), outs[0], outs[len(outs) // 4], statistics.median(outs),
             outs[3 * len(outs) // 4], outs[-1]))
    print('  → 中位年化超额 %+.2f%% = 27万本金每年 %.0f 元' %
          (statistics.median(outs), CAP * statistics.median(outs) / 100))
    print('  → 但这是全样本(含过拟合); 样本外中位约 +1.1%%/1.7年 ≈ %+.2f%%/年'
          % (1.1 / 1.71))


if __name__ == '__main__':
    main()
