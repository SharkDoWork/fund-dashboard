# -*- coding: utf-8 -*-
"""
做T策略 样本外检验(OOS) + 资金效率 + 终极对比
把 4.37 年切成 样本内(前60%)选参数 / 样本外(后40%)验证, 检验过拟合。
"""
import sqlite3, statistics, datetime, math, random

DB = 'data/fund_history.db'
CODE = '014414'
random.seed(7)
A_BUY, SELL_S, SELL_L = 0.0012, 0.015, 0.0
IDLE_YEAR = 0.015          # 闲置资金放货币基金年化


def load():
    con = sqlite3.connect(DB)
    rows = con.execute(
        "SELECT trade_date, dwjz FROM nav_history WHERE fund_code=? "
        "AND dwjz IS NOT NULL ORDER BY trade_date", (CODE,)).fetchall()
    con.close()
    return ([datetime.date.fromisoformat(r[0]) for r in rows],
            [r[1] for r in rows])


def fee(hd):
    return SELL_S if hd < 7 else SELL_L


def run(sig, dates, navs, M, lo, hi):
    """在 [lo,hi) 索引区间内执行串行交易"""
    out, j = [], lo
    for s in sig:
        if s < j or s < lo:
            continue
        bi = s + 1
        ei = bi + M
        if ei >= hi:
            break
        hd = (dates[ei] - dates[bi]).days
        r = (navs[ei] / navs[bi]) * (1 - A_BUY) * (1 - fee(hd)) - 1
        out.append({'bi': bi, 'ei': ei, 'hd': hd, 'ret': r * 100})
        j = ei + 1
    return out


def summ(tr):
    if not tr:
        return None
    r = [t['ret'] for t in tr]
    eq = 1.0
    for x in r:
        eq *= 1 + x / 100
    w = [x for x in r if x > 0]
    return {'n': len(r), 'win': 100.0 * len(w) / len(r),
            'avg': statistics.mean(r), 'tot': (eq - 1) * 100}


def mc_percentile(sig, dates, navs, M, lo, hi, iters=2000):
    pool = list(range(lo, hi - M - 1))
    if len(pool) < len(sig) + 5:
        return None, None
    res = []
    for _ in range(iters):
        rs = sorted(random.sample(pool, min(len(sig), len(pool))))
        eq, last = 1.0, -10 ** 9
        for x in rs:
            if x < last:
                continue
            bi, ei = x + 1, x + 1 + M
            if ei >= hi:
                break
            hd = (dates[ei] - dates[bi]).days
            eq *= (navs[ei] / navs[bi]) * (1 - A_BUY) * (1 - fee(hd))
            last = ei + 1
        res.append((eq - 1) * 100)
    res.sort()
    return statistics.median(res), res


def main():
    dates, navs = load()
    n = len(navs)
    ret = [0.0] + [(navs[i] / navs[i - 1] - 1) * 100 for i in range(1, n)]
    split = int(n * 0.6)
    print('=' * 90)
    print('样本外检验   全样本 %s ~ %s (%d日)' % (dates[0], dates[-1], n))
    print('  样本内 IS : %s ~ %s (%d日)  —— 用来挑参数' %
          (dates[0], dates[split - 1], split))
    print('  样本外 OOS: %s ~ %s (%d日)  —— 用来验证' %
          (dates[split], dates[-1], n - split))
    print('=' * 90)

    # 策略池
    cands = []
    for k in (2, 3, 4):
        for M in (5, 8, 10, 15, 20, 30):
            cands.append(('连跌%d_持%d' % (k, M),
                          lambda i, k=k: all(ret[j] < 0 for j in range(i - k + 1, i + 1)), M))
    for X in (1.0, 1.5, 2.0, 2.5, 3.0):
        for M in (5, 10, 15, 20, 30):
            cands.append(('跌%.1f%%_持%d' % (X, M), lambda i, X=X: ret[i] <= -X, M))

    # ---- IS 选优 ----
    is_res = []
    for name, fn, M in cands:
        sig = [i for i in range(max(4, M), split - M - 1) if fn(i)]
        if len(sig) < 8:
            continue
        tr = run(sig, dates, navs, M, 0, split)
        s = summ(tr)
        if s:
            is_res.append((name, fn, M, s))
    is_res.sort(key=lambda x: -x[3]['tot'])

    print('\n【A】样本内(IS)表现最好的 10 个策略')
    print('-' * 90)
    print('%-16s %5s %7s %9s %10s' % ('策略', '次数', '胜率', '单次均值', 'IS累计'))
    for name, fn, M, s in is_res[:10]:
        print('%-16s %5d %6.1f%% %+8.2f%% %+9.1f%%' %
              (name, s['n'], s['win'], s['avg'], s['tot']))

    # ---- OOS 验证 ----
    print('\n【B】把这 10 个"样本内最优"策略, 放到样本外(OOS)验证 —— 关键')
    print('-' * 90)
    print('%-16s %10s %10s %10s %8s' %
          ('策略', 'IS累计', 'OOS累计', 'OOS胜率', 'OOS次数'))
    oos_tots = []
    for name, fn, M, s in is_res[:10]:
        sig = [i for i in range(max(split, 4), n - M - 1) if fn(i)]
        tr = run(sig, dates, navs, M, split, n)
        so = summ(tr)
        if so:
            oos_tots.append(so['tot'])
            print('%-16s %+9.1f%% %+9.1f%% %9.1f%% %8d' %
                  (name, s['tot'], so['tot'], so['win'], so['n']))
        else:
            print('%-16s %+9.1f%% %10s' % (name, s['tot'], '样本不足'))
    if oos_tots:
        print('\n  OOS 平均累计: %+.1f%%   (IS前10平均: %+.1f%%)' %
              (statistics.mean(oos_tots),
               statistics.mean([s['tot'] for _, _, _, s in is_res[:10]])))
        print('  衰减幅度: %.1f 个百分点  ← 这就是过拟合的代价' %
              (statistics.mean([s['tot'] for _, _, _, s in is_res[:10]])
               - statistics.mean(oos_tots)))

    # ---- OOS 上重新做显著性检验 ----
    print('\n【C】样本外显著性检验 (OOS 区间, 随机信号 2000 次)')
    print('-' * 90)
    print('%-16s %6s %7s %9s %11s %11s %8s' %
          ('策略', '次数', '胜率', '单次均值', 'OOS累计', '随机中位', '百分位'))
    rows = []
    for name, fn, M in cands:
        sig = [i for i in range(max(split, 4), n - M - 1) if fn(i)]
        if len(sig) < 8:
            continue
        tr = run(sig, dates, navs, M, split, n)
        s = summ(tr)
        if not s:
            continue
        med, dist = mc_percentile(sig, dates, navs, M, split, n)
        if med is None:
            continue
        pct = 100.0 * sum(1 for x in dist if x < s['tot']) / len(dist)
        rows.append((name, M, s, med, pct))
    rows.sort(key=lambda x: -x[4])
    for name, M, s, med, pct in rows[:12]:
        print('%-16s %6d %6.1f%% %+8.2f%% %+10.1f%% %+10.1f%% %7.1f%%' %
              (name, s['n'], s['win'], s['avg'], s['tot'], med, pct))
    print('  ... (共%d个策略)   OOS百分位>95%%的有 %d 个;  <50%%的有 %d 个' %
          (len(rows), sum(1 for r in rows if r[4] > 95),
           sum(1 for r in rows if r[4] < 50)))

    # ---- 资金效率: 做T的年化(含闲置资金收益) ----
    print('\n【D】资金效率 —— 10万本金, 做T的"真实年化" (闲置期按货币基金1.5%/年计)')
    print('-' * 90)
    tot_days = (dates[-1] - dates[0]).days
    print('%-16s %6s %10s %11s %11s %11s' %
          ('策略', '次数', '占用天数', '策略毛收益', '闲置收益', '组合年化'))
    for name, fn, M in [('连跌4_持10', [c[1] for c in cands if c[0] == '连跌4_持10'][0], 10),
                        ('连跌2_持10', [c[1] for c in cands if c[0] == '连跌2_持10'][0], 10),
                        ('跌2.0%_持20', [c[1] for c in cands if c[0] == '跌2.0%_持20'][0], 20),
                        ('跌1.0%_持10', [c[1] for c in cands if c[0] == '跌1.0%_持10'][0], 10)]:
        sig = [i for i in range(4, n - M - 1) if fn(i)]
        tr = run(sig, dates, navs, M, 0, n)
        s = summ(tr)
        if not s:
            continue
        occ = sum(t['hd'] for t in tr)
        idle = tot_days - occ
        idle_gain = (1 + IDLE_YEAR) ** (idle / 365.25) - 1
        combo = (1 + s['tot'] / 100) * (1 + idle_gain) - 1
        ann = ((1 + combo) ** (365.25 / tot_days) - 1) * 100
        print('%-16s %6d %10d %+10.1f%% %+10.1f%% %+10.2f%%' %
              (name, s['n'], occ, s['tot'], idle_gain * 100, ann))
    bh = (navs[-1] / navs[0] - 1) * 100
    ann_bh = ((navs[-1] / navs[0]) ** (365.25 / tot_days) - 1) * 100
    print('%-16s %6s %10d %+10.1f%% %10s %+10.2f%%' %
          ('买入持有(对照)', '-', tot_days, bh, '-', ann_bh))


if __name__ == '__main__':
    main()
