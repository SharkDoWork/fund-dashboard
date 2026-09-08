# -*- coding: utf-8 -*-
"""
倒T 样本外检验 + 趋势压力测试(上涨段/下跌段分别检验)
核心问题: 倒T的收益是真alpha, 还是"下跌市赠品"?
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


def reverse_t(dates, navs, up, back, w, sell_frac=0.2, lo=0, hi=None,
              verbose=False):
    """在 [lo,hi) 区间执行倒T; 返回(交易列表, 期末资产相对期初的倍数)"""
    if hi is None:
        hi = len(navs)
    n = hi
    ma20 = [ma(navs, i, 20) for i in range(len(navs))]
    shares = 1.0                     # 归一化: 期初1份
    pending = None
    trades = []
    i = max(lo + 21, 21)
    while i < n - 1:
        if pending is None:
            m = ma20[i - 1]
            if m and navs[i - 1] >= m * (1 + up / 100.0):
                sell_sh = shares * sell_frac
                hd = (dates[i] - dates[lo]).days
                cash = sell_sh * navs[i] * (1 - fee(hd))
                shares -= sell_sh
                pending = (i, navs[i], cash)
        else:
            si, sp, amt = pending
            waited = i - si
            back_hit = navs[i] <= sp * (1 - back / 100.0)
            if (back_hit and waited >= 5) or waited >= w or i == n - 2:
                shares += amt * (1 - A_BUY) / navs[i]
                trades.append({'ret': (sp / navs[i] - 1) * 100, 'wait': waited})
                pending = None
        i += 1
    final = shares * navs[n - 1] + (pending[2] if pending else 0.0)
    # 买入持有基准: 期初1份 -> 期末
    bh = navs[n - 1] / navs[lo]
    return trades, final / navs[lo], bh


def main():
    dates, navs = load()
    n = len(navs)
    split = int(n * 0.6)
    print('=' * 90)
    print('倒T 样本外检验     IS %s~%s   OOS %s~%s' %
          (dates[0], dates[split - 1], dates[split], dates[-1]))
    print('=' * 90)

    params = [(up, back, w) for up in (2, 3, 5, 8)
              for back in (2, 3, 5) for w in (10, 20, 40)]

    # IS 选优
    is_res = []
    for up, back, w in params:
        tr, fin, bh = reverse_t(dates, navs, up, back, w, lo=0, hi=split)
        if not tr:
            continue
        rs = [t['ret'] for t in tr]
        is_res.append(((up, back, w), len(tr),
                       100.0 * sum(1 for x in rs if x > 0) / len(rs),
                       statistics.mean(rs), (fin / bh - 1) * 100))
    is_res.sort(key=lambda x: -x[4])
    print('\n【A】样本内(IS)倒T 前8名 —— 超额 = 倒T期末 - 同期买入持有')
    print('-' * 90)
    print('%-22s %6s %7s %9s %11s' % ('参数(高/回/等)', '次数', '成功率', '单次均值', 'IS超额'))
    for p, cnt, good, avg, exc in is_res[:8]:
        print('%-22s %6d %6.1f%% %+8.2f%% %+10.1f%%' %
              ('%g%% / %g%% / %d日' % p, cnt, good, avg, exc))

    print('\n【B】样本内前8名, 放到样本外(OOS)验证')
    print('-' * 90)
    print('%-22s %11s %11s %9s %8s' % ('参数(高/回/等)', 'IS超额', 'OOS超额', 'OOS成功率', 'OOS次数'))
    oos_exc = []
    for p, cnt, good, avg, exc in is_res[:8]:
        tr, fin, bh = reverse_t(dates, navs, *p, lo=split, hi=n)
        if not tr:
            print('%-22s %+10.1f%% %11s' % ('%g%% / %g%% / %d日' % p, exc, '样本不足'))
            continue
        rs = [t['ret'] for t in tr]
        e = (fin / bh - 1) * 100
        oos_exc.append(e)
        print('%-22s %+10.1f%% %+10.1f%% %8.1f%% %8d' %
              ('%g%% / %g%% / %d日' % p, exc, e,
               100.0 * sum(1 for x in rs if x > 0) / len(rs), len(tr)))
    if oos_exc:
        print('\n  IS前8平均超额 %+.1f%%  →  OOS平均超额 %+.1f%%  衰减 %.1fpp' %
              (statistics.mean([x[4] for x in is_res[:8]]),
               statistics.mean(oos_exc),
               statistics.mean([x[4] for x in is_res[:8]]) - statistics.mean(oos_exc)))

    # 全部参数的 OOS 表现分布
    print('\n【C】全部%d组参数在样本外的超额分布 —— 倒T是否普遍有效?' % len(params))
    print('-' * 90)
    all_oos = []
    for p in params:
        tr, fin, bh = reverse_t(dates, navs, *p, lo=split, hi=n)
        if tr:
            all_oos.append((fin / bh - 1) * 100)
    all_oos.sort()
    if all_oos:
        print('  样本数 %d  最好 %+.1f%%  最差 %+.1f%%  中位 %+.1f%%  均值 %+.1f%%' %
              (len(all_oos), all_oos[-1], all_oos[0],
               statistics.median(all_oos), statistics.mean(all_oos)))
        print('  超额>0 的参数占比: %.0f%%  (若倒T真的有效, 应显著超过50%%)' %
              (100.0 * sum(1 for x in all_oos if x > 0) / len(all_oos)))

    # ---- 趋势压力测试 ----
    print('\n【D】趋势压力测试 —— 找出历史上最强上涨段 / 最强下跌段, 分别检验')
    print('-' * 90)

    def find_segments(win=60, top=3):
        segs = []
        for i in range(0, n - win):
            chg = (navs[i + win] / navs[i] - 1) * 100
            segs.append((chg, i, i + win))
        segs.sort()
        return segs[-top:][::-1], segs[:top]

    up_segs, dn_segs = find_segments(60)
    print('\n  ■ 最强上涨的 3 个 60 日区间:')
    print('    %-26s %10s %12s %12s %10s' %
          ('区间', '买入持有', '正T(连跌3持10)', '倒T(高3回5等20)', '倒T差值'))
    for chg, a, b in up_segs:
        tr, fin, bh = reverse_t(dates, navs, 3, 5, 20, lo=a, hi=b)
        print('    %-26s %+9.1f%% %12s %+11.1f%% %+9.1f%%' %
              ('%s~%s' % (dates[a], dates[b]), (bh - 1) * 100, '见下方注',
               (fin / bh - 1) * 100, (fin - bh) * 100))

    print('\n  ■ 最强下跌的 3 个 60 日区间:')
    for chg, a, b in dn_segs:
        tr, fin, bh = reverse_t(dates, navs, 3, 5, 20, lo=a, hi=b)
        print('    %-26s %+9.1f%% %12s %+11.1f%% %+9.1f%%' %
              ('%s~%s' % (dates[a], dates[b]), (bh - 1) * 100, '见下方注',
               (fin / bh - 1) * 100, (fin - bh) * 100))

    # ---- 全期按"年度涨跌"分组 ----
    print('\n【E】核心结论表 —— 做T收益 与 行情方向的关系')
    print('-' * 90)
    print('%-10s %10s %12s %12s' % ('行情环境', '买入持有', '正T超额', '倒T超额'))
    years = sorted({d.year for d in dates})
    for y in years:
        lo = next(i for i, d in enumerate(dates) if d.year == y)
        hi = next((i for i, d in enumerate(dates) if d.year > y), n)
        if hi - lo < 60:
            continue
        bh = (navs[hi - 1] / navs[lo] - 1) * 100
        # 正T
        ret = [0.0] + [(navs[i] / navs[i - 1] - 1) * 100 for i in range(1, n)]
        sig = [i for i in range(max(lo, 3), hi - 11)
               if all(ret[j] < 0 for j in range(i - 2, i + 1))]
        eq, j = 1.0, lo
        for s in sig:
            if s < j:
                continue
            bi, ei = s + 1, s + 1 + 10
            if ei >= hi:
                break
            hd = (dates[ei] - dates[bi]).days
            eq *= (navs[ei] / navs[bi]) * (1 - A_BUY) * (1 - fee(hd))
            j = ei + 1
        pos_t = (eq - 1) * 100
        # 倒T
        tr, fin, bhx = reverse_t(dates, navs, 3, 5, 20, lo=lo, hi=hi)
        rev_t = (fin / bhx - 1) * 100
        env = '上涨' if bh > 5 else ('下跌' if bh < -5 else '震荡')
        print('%-10s %+9.1f%% %+11.1f%% %+11.1f%%   (%d年 %s)' %
              (env, bh, pos_t - bh, rev_t, y, dates[lo].strftime('%Y')))
    print('\n  正T超额 = 正T累计 - 该年买入持有;  倒T超额 = 倒T期末 - 该年买入持有')


if __name__ == '__main__':
    main()
