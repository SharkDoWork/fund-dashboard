# -*- coding: utf-8 -*-
"""
倒T(先卖后买)回测 + 分年度做T表现 + 当前位置评估
场景: 已持有 27 万底仓, 能否靠"涨了卖、跌了买回"降成本
"""
import sqlite3, statistics, datetime

DB = 'data/fund_history.db'
CODE = '014414'
A_BUY, SELL_S, SELL_L = 0.0012, 0.015, 0.0


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


# ---------------- 倒T ----------------
def reverse_t(dates, navs, up_pct, back_pct, maxwait, sell_frac=0.2,
              capital=270000.0):
    """
    底仓持有, 净值高于MA20达 up_pct -> 卖出 sell_frac 仓位;
    之后 回落 back_pct 或 最多 maxwait 交易日 -> 买回。
    期末未买回的部分按期末净值计(踏空成本)。
    """
    n = len(navs)
    ma20 = [ma(navs, i, 20) for i in range(n)]
    shares = capital * (1 - A_BUY) / navs[0]      # 底仓份额
    cash = 0.0
    pending = None                                 # (卖出日idx, 卖出价, 现金)
    trades = []
    i = 21
    while i < n - 1:
        if pending is None:
            m = ma20[i - 1]
            if m and navs[i - 1] >= m * (1 + up_pct / 100.0):
                sell_sh = shares * sell_frac
                if sell_sh > 0:
                    hd = (dates[i] - dates[0]).days   # 底仓早已满7天
                    cash += sell_sh * navs[i] * (1 - fee(hd))
                    shares -= sell_sh
                    pending = (i, navs[i], cash)
                    cash = 0.0
        else:
            si, sp, amt = pending
            waited = i - si
            back = navs[i] <= sp * (1 - back_pct / 100.0)
            if (back and waited >= 5) or waited >= maxwait or i == n - 2:
                sh = amt * (1 - A_BUY) / navs[i]
                shares += sh
                trades.append({'bd': dates[si], 'bp': sp, 'sd': dates[i],
                               'sp': navs[i], 'wait': waited,
                               'ret': (sp / navs[i] - 1) * 100})
                pending = None
        i += 1
    final = shares * navs[-1] + (pending[2] if pending else 0.0)
    return trades, final


# ---------------- 分年度正T ----------------
def yearly(dates, navs):
    n = len(navs)
    ret = [0.0] + [(navs[i] / navs[i - 1] - 1) * 100 for i in range(1, n)]
    years = sorted({d.year for d in dates})
    print('\n【分年度】各年度内"连跌3日买入持有10日"的表现 vs 该年买入持有')
    print('-' * 88)
    print('%-8s %6s %7s %9s %11s %11s %11s' %
          ('年份', '次数', '胜率', '单次均值', '做T累计', '买入持有', '差值'))
    for y in years:
        lo = next(i for i, d in enumerate(dates) if d.year == y)
        hi = next((i for i, d in enumerate(dates) if d.year > y), n)
        if hi - lo < 40:
            continue
        sig = [i for i in range(max(lo, 3), hi - 11)
               if all(ret[j] < 0 for j in range(i - 2, i + 1))]
        eq = 1.0
        cnt = 0
        rs = []
        j = lo
        for s in sig:
            if s < j:
                continue
            bi, ei = s + 1, s + 1 + 10
            if ei >= hi:
                break
            hd = (dates[ei] - dates[bi]).days
            r = (navs[ei] / navs[bi]) * (1 - A_BUY) * (1 - fee(hd)) - 1
            rs.append(r * 100)
            eq *= 1 + r
            j = ei + 1
            cnt += 1
        bh = (navs[hi - 1] / navs[lo] - 1) * 100
        if cnt:
            print('%-8d %6d %6.1f%% %+8.2f%% %+10.1f%% %+10.1f%% %+10.1f%%' %
                  (y, cnt, 100.0 * sum(1 for x in rs if x > 0) / len(rs),
                   statistics.mean(rs), (eq - 1) * 100, bh, (eq - 1) * 100 - bh))
        else:
            print('%-8d %6d %7s %9s %11s %+10.1f%% %11s' %
                  (y, 0, '-', '-', '-', bh, '-'))


def main():
    dates, navs = load()
    n = len(navs)
    print('=' * 88)
    print('倒T(先卖后买) + 分年度检验    净值 %.4f -> %.4f' % (navs[0], navs[-1]))
    print('=' * 88)

    base = 270000.0 * (1 - A_BUY) / navs[0] * navs[-1]
    print('\n基线: 27万期初买入并持有到底 -> %.0f 元 (%+.2f%%)'
          % (base, (base / 270000 - 1) * 100))

    print('\n【倒T】底仓27万, 净值高于MA20 X%% 卖出20%%仓位, 回落Y%%或最多W日买回')
    print('-' * 88)
    print('%-26s %6s %7s %9s %13s %11s' %
          ('规则', '次数', '成功率', '单次均值', '期末资产', 'vs持有'))
    best = None
    for up in (2, 3, 5, 8):
        for back in (2, 3, 5):
            for w in (10, 20, 40):
                tr, final = reverse_t(dates, navs, up, back, w)
                if not tr:
                    continue
                rs = [t['ret'] for t in tr]
                good = 100.0 * sum(1 for x in rs if x > 0) / len(rs)
                diff = final - base
                if best is None or diff > best[0]:
                    best = (diff, up, back, w, len(tr), good,
                            statistics.mean(rs), final)
                print('%-26s %6d %6.1f%% %+8.2f%% %12.0f元 %+10.0f元' %
                      ('高%g%%卖_回%g%%买_等%d日' % (up, back, w), len(tr),
                       good, statistics.mean(rs), final, diff))
    print('\n  最优倒T: 高%g%%卖 / 回%g%%买 / 等%d日 → %d次, 成功率%.1f%%, '
          '单次均值%+.2f%%, 期末%.0f元, 比持有 %+.0f元'
          % (best[1], best[2], best[3], best[4], best[5], best[6], best[7], best[0]))

    yearly(dates, navs)

    # 当前位置
    print('\n【当前位置评估】')
    print('-' * 88)
    cur = navs[-1]
    m20 = ma(navs, n - 1, 20)
    m60 = ma(navs, n - 1, 60)
    print('  最新净值 %.4f (%s)' % (cur, dates[-1]))
    print('  MA20 %.4f  偏离 %+.2f%%' % (m20, (cur / m20 - 1) * 100))
    print('  MA60 %.4f  偏离 %+.2f%%' % (m60, (cur / m60 - 1) * 100))
    recent = navs[-21:]
    print('  近20日振幅: 最高%.4f 最低%.4f 波动%.2f%%'
          % (max(recent), min(recent), (max(recent) / min(recent) - 1) * 100))
    for w in (60, 120, 250):
        if n > w:
            seg = navs[-w:]
            print('  近%d日振幅 %.2f%%  (做T的理论空间上限)'
                  % (w, (max(seg) / min(seg) - 1) * 100))


if __name__ == '__main__':
    main()
