# -*- coding: utf-8 -*-
"""
014414 做T / 波段交易 严格回测 v2
新增: 1) 标准网格 + "只买不卖"对照组  2) 蒙特卡洛显著性检验(排除数据窥探)
      3) 三种交易工具(A类场外/C类场外/场内ETF)成本对比  4) 年化与资金占用
"""
import sqlite3, statistics, datetime, math, random

DB = 'data/fund_history.db'
CODE = '014414'
random.seed(42)

# ===== 费率(已核实) =====
# A类 014414: 申购0.12%(1折) 赎回<7天1.5% >=7天0  运作费0.5+0.1=0.6%/年(含净值)
# C类 014415: 申购0%        赎回<7天1.5% >=7天0  运作费0.5+0.1+销售服务费0.4=1.0%/年(含净值)
# 场内 516670: 佣金双向 运作费0.2+0.1=0.3%/年(含净值)
A_BUY, A_SELL_S, A_SELL_L = 0.0012, 0.015, 0.0
C_BUY, C_SELL_S, C_SELL_L = 0.0, 0.015, 0.0
C_SVC_YEAR = 0.004          # C类销售服务费年化
ETF_COMM = 0.0002           # 场内万2双边合计(万1单边), 免印花税


def load():
    con = sqlite3.connect(DB)
    rows = con.execute(
        "SELECT trade_date, dwjz FROM nav_history WHERE fund_code=? "
        "AND dwjz IS NOT NULL ORDER BY trade_date", (CODE,)).fetchall()
    con.close()
    return ([datetime.date.fromisoformat(r[0]) for r in rows],
            [r[1] for r in rows])


def cost_roundtrip(tool, hold_cal_days):
    """一次完整买卖的交易成本(不含运作费,运作费已含在净值里)"""
    if tool == 'A':
        return A_BUY + (A_SELL_S if hold_cal_days < 7 else A_SELL_L)
    if tool == 'C':
        return C_BUY + (C_SELL_S if hold_cal_days < 7 else C_SELL_L) \
            + C_SVC_YEAR * hold_cal_days / 365.0
    return ETF_COMM          # 场内: 佣金, 与持有天数无关


def run_serial(signals, dates, navs, hold_td, tool='A'):
    """串行: i日收盘见信号 -> i+1日按净值成交 -> 持有 hold_td 个交易日赎回"""
    n = len(navs)
    out, j = [], 0
    for s in signals:
        if s < j:
            continue
        bi = s + 1
        ei = bi + hold_td
        if ei >= n:
            break
        hd = (dates[ei] - dates[bi]).days
        c = cost_roundtrip(tool, hd)
        r = (navs[ei] / navs[bi]) * (1 - c) - 1
        out.append({'bd': dates[bi], 'sd': dates[ei], 'hd': hd, 'ret': r * 100})
        j = ei + 1
    return out


def equity(trades, capital=1.0):
    eq, peak, mdd = capital, capital, 0.0
    for t in trades:
        eq *= (1 + t['ret'] / 100)
        peak = max(peak, eq)
        mdd = min(mdd, (eq - peak) / peak * 100)
    return eq, mdd


def summarize(trades, capital=1.0):
    if not trades:
        return None
    rets = [t['ret'] for t in trades]
    eq, mdd = equity(trades, capital)
    w = [r for r in rets if r > 0]
    l = [r for r in rets if r <= 0]
    return {'n': len(rets), 'win': 100.0 * len(w) / len(rets),
            'avg': statistics.mean(rets), 'med': statistics.median(rets),
            'aw': statistics.mean(w) if w else 0,
            'al': statistics.mean(l) if l else 0,
            'tot': (eq / capital - 1) * 100, 'mdd': mdd,
            'best': max(rets), 'worst': min(rets)}


def main():
    dates, navs = load()
    n = len(navs)
    ret = [0.0] + [(navs[i] / navs[i - 1] - 1) * 100 for i in range(1, n)]
    years = (dates[-1] - dates[0]).days / 365.25
    print('=' * 88)
    print('014414 做T 严格回测   %s ~ %s  %d交易日  %.2f年' %
          (dates[0], dates[-1], n, years))
    print('净值 %.4f -> %.4f   买入持有 %+.2f%%   年化 %+.2f%%' %
          (navs[0], navs[-1], (navs[-1] / navs[0] - 1) * 100,
           ((navs[-1] / navs[0]) ** (1 / years) - 1) * 100))
    print('=' * 88)

    # ============ 1. 交易工具成本对比 ============
    print('\n【1】做T工具成本对比 —— 一次完整买卖的交易成本')
    print('-' * 88)
    print('%-28s %10s %10s %10s %10s' %
          ('工具', '持有3天', '持有10天', '持有30天', '持有90天'))
    for tool, name in (('A', 'A类场外 014414'), ('C', 'C类场外 014415'),
                       ('ETF', '场内ETF 516670')):
        print('%-28s %9.3f%% %9.3f%% %9.3f%% %9.3f%%' %
              (name, *[cost_roundtrip(tool, d) * 100 for d in (3, 10, 30, 90)]))
    print('  注: 管理费/托管费/销售服务费按日计提并已含在净值中, 上表仅为交易环节成本。')
    d = 0.0012 / 0.004 * 365
    print('  A/C临界点: A类申购费0.12%% == C类销售服务费0.4%%/年  →  持有约 %.0f 天' % d)
    print('            持有 < %.0f 天用 C 类更省;  > %.0f 天用 A 类更省' % (d, d))

    # ============ 2. 蒙特卡洛显著性检验 ============
    print('\n【2】显著性检验 —— 择时信号是真alpha还是运气? (随机信号蒙特卡洛 2000次)')
    print('-' * 88)
    print('%-24s %6s %7s %9s %9s %11s %8s' %
          ('策略', '次数', '胜率', '单次均值', '实际累计', '随机中位累计', '百分位'))
    cands = []
    for k in (2, 3, 4):
        for M in (5, 8, 10, 15, 20, 30):
            sig = [i for i in range(k, n - M - 1)
                   if all(ret[j] < 0 for j in range(i - k + 1, i + 1))]
            if len(sig) >= 15:
                cands.append(('连跌%d日_持有%d' % (k, M), sig, M))
    for X in (1.0, 1.5, 2.0, 2.5, 3.0):
        for M in (5, 10, 15, 20, 30):
            sig = [i for i in range(1, n - M - 1) if ret[i] <= -X]
            if len(sig) >= 15:
                cands.append(('单日跌>%.1f%%_持有%d' % (X, M), sig, M))

    results = []
    for name, sig, M in cands:
        tr = run_serial(sig, dates, navs, M, 'A')
        s = summarize(tr)
        if not s:
            continue
        # 蒙特卡洛: 随机取同样数量的起点, 同样持有期
        pool = list(range(0, n - M - 1))
        rnd_tot = []
        for _ in range(2000):
            rs = random.sample(pool, len(sig))
            eq = 1.0
            rs.sort()
            last = -10 ** 9
            cnt = 0
            for x in rs:                    # 同样串行约束
                if x < last:
                    continue
                bi, ei = x + 1, x + 1 + M
                if ei >= n:
                    break
                hd = (dates[ei] - dates[bi]).days
                eq *= (navs[ei] / navs[bi]) * (1 - cost_roundtrip('A', hd))
                last = ei + 1
                cnt += 1
            if cnt:
                rnd_tot.append((eq - 1) * 100)
        rnd_tot.sort()
        med = statistics.median(rnd_tot)
        pct = 100.0 * sum(1 for x in rnd_tot if x < s['tot']) / len(rnd_tot)
        results.append((name, s, med, pct))
        print('%-24s %6d %6.1f%% %+8.2f%% %+10.1f%% %+11.1f%% %7.1f%%' %
              (name, s['n'], s['win'], s['avg'], s['tot'], med, pct))
    print('\n  解读: 百分位 = 该策略实际累计收益击败了多少%%的"随机买入"结果。')
    print('        >95%% 才算有统计显著性;  50%%左右 = 与瞎买无异。')

    # ============ 3. 网格 vs 只买不卖(对照组) ============
    print('\n【3】网格做T vs 同样分批买入但"不做T" (对照组, 消除行情beta)')
    print('-' * 88)

    def grid_sim(gap, capital=100000.0, layers=5, do_t=True, tool='A'):
        """标准网格: 以P0为基准, 每跌gap一格买入, 涨回上一格卖出一份(FIFO)"""
        p0 = navs[0]
        cash = capital
        lots = []                      # [(买入净值, 份额, 索引)]
        realized = []                  # 已平仓T的收益率
        lvl_prev = 0
        for i in range(1, n):
            p = navs[i]
            lvl = math.floor(math.log(p / p0) / math.log(1 - gap / 100.0))
            if lvl > lvl_prev:                       # 跌入新格 -> 买
                for _ in range(lvl - lvl_prev):
                    if lvl <= layers - 1 and cash >= capital / layers * 0.9:
                        amt = capital / layers
                        fee = A_BUY if tool == 'A' else (C_BUY if tool == 'C' else ETF_COMM / 2)
                        sh = amt * (1 - fee) / p
                        lots.append((p, sh, i))
                        cash -= amt
            elif lvl < lvl_prev and do_t and lots:   # 涨回上一格 -> 卖一份
                for _ in range(lvl_prev - lvl):
                    if lots:
                        bp, sh, bi = lots.pop(0)
                        hd = (dates[i] - dates[bi]).days
                        c = cost_roundtrip(tool, hd)
                        net = sh * p * (1 - (c if tool != 'ETF' else ETF_COMM / 2))
                        realized.append((net / (sh * bp) - 1) * 100)
                        cash += net
            lvl_prev = lvl
        mv = sum(sh * navs[-1] for _, sh, _ in lots)
        cost_left = sum(sh * bp for bp, sh, _ in lots)
        return realized, cash + mv, cost_left, lots

    print('%-10s %-9s %6s %7s %9s %12s %12s %10s' %
          ('网格', '模式', 'T次数', 'T胜率', '单次均值', '期末总资产', '对照(不做T)', '做T增量'))
    for gap in (2.0, 3.0, 5.0, 8.0):
        row_t = grid_sim(gap, do_t=True)
        row_n = grid_sim(gap, do_t=False)
        rz = row_t[0]
        s = summarize([{'ret': r} for r in rz]) if rz else None
        tv, nv = row_t[1], row_n[1]
        inc = tv - nv
        print('%-10s %-9s %6d %6.1f%% %+8.2f%% %11.0f元 %11.0f元 %+9.0f元' %
              ('%.1f%%' % gap, '做T', s['n'] if s else 0,
               s['win'] if s else 0, s['avg'] if s else 0, tv, nv, inc))
        print('%-10s %-9s %6s %7s %9s %12s   (总资产收益率: 做T %+.2f%% / 不做T %+.2f%%)' %
              ('', '不做T', '-', '-', '-', '', (tv / 1000 - 100), (nv / 1000 - 100)))
    print('\n  注: 本金10万, 分5层分批买入。做T增量 = 做T总资产 - 不做T总资产,')
    print('      这才是"高抛低吸"真正多赚的钱(已扣全部交易成本, 含期末浮亏)。')

    # ============ 4. 不同工具下的做T结果 ============
    print('\n【4】同一策略在不同工具下的结果 (连跌3日买入, 持有10个交易日)')
    print('-' * 88)
    sig = [i for i in range(3, n - 11)
           if all(ret[j] < 0 for j in range(i - 2, i + 1))]
    print('%-28s %6s %7s %9s %11s' % ('工具', '次数', '胜率', '单次均值', '累计'))
    for tool, name in (('A', 'A类场外(申购0.12%)'), ('C', 'C类场外(申购0)'),
                       ('ETF', '场内ETF(佣金万2双边)')):
        tr = run_serial(sig, dates, navs, 10, tool)
        s = summarize(tr)
        print('%-28s %6d %6.1f%% %+8.2f%% %+10.1f%%' %
              (name, s['n'], s['win'], s['avg'], s['tot']))

    # ============ 5. 短周期做T(持有<7天)的费率惩罚 ============
    print('\n【5】短周期做T —— 赎回费1.5%的杀伤力 (连跌3日买入)')
    print('-' * 88)
    print('%-14s %6s %7s %9s %11s   %s' %
          ('持有(交易日)', '次数', '胜率', '单次均值', '累计', '说明'))
    for M in (1, 2, 3, 5, 6, 8, 10):
        tr = run_serial(sig, dates, navs, M, 'A')
        s = summarize(tr)
        hd = (dates[M + 1] - dates[1]).days
        note = '触发1.5%惩罚性赎回费' if hd < 7 else '赎回费0'
        print('%-14s %6d %6.1f%% %+8.2f%% %+10.1f%%   (约%d自然日) %s'
              % (M, s['n'], s['win'], s['avg'], s['tot'], hd, note))


if __name__ == '__main__':
    main()
