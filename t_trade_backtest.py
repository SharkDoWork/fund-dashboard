# -*- coding: utf-8 -*-
"""
014414 招商中证畜牧养殖ETF联接A —— "做T / 波段交易" 历史回测
用 2022-04-19 ~ 2026-09-02 的 1064 个交易日净值，回放多种做T模式，
回答：能不能赚、概率多少、赚多少。
"""
import sqlite3, statistics, datetime, json

DB = 'data/fund_history.db'
CODE = '014414'
BUY_FEE = 0.0012          # 申购费 1 折 0.12%
SELL_FEE_SHORT = 0.015    # 持有 <7 天 赎回费 1.5%
SELL_FEE_LONG = 0.0       # 持有 >=7 天 赎回费 0


def load():
    con = sqlite3.connect(DB)
    rows = con.execute(
        "SELECT trade_date, dwjz FROM nav_history WHERE fund_code=? "
        "AND dwjz IS NOT NULL ORDER BY trade_date", (CODE,)).fetchall()
    con.close()
    dates = [datetime.date.fromisoformat(r[0]) for r in rows]
    navs = [r[1] for r in rows]
    return dates, navs


def sell_fee(hold_days):
    """场外基金赎回费：持有期 <7 天惩罚性 1.5%，>=7 天为 0"""
    return SELL_FEE_SHORT if hold_days < 7 else SELL_FEE_LONG


def ma(navs, i, win):
    if i + 1 < win:
        return None
    return sum(navs[i + 1 - win:i + 1]) / win


def run_trades(signals, dates, navs, hold_days, fee_on=True, label=''):
    """
    signals: 买入信号所在索引列表（在 i 日收盘看到信号，i+1 日按 i+1 净值成交）
    hold_days: 固定持有交易日数后按当日净值赎回
    串行执行：一笔平仓后才找下一个信号（单笔资金滚动）
    """
    n = len(navs)
    trades = []
    j = 0
    for s in signals:
        if s < j:            # 上一笔还没结束
            continue
        bi = s + 1           # T+1 日成交（保守：信号次日才买到）
        if bi >= n:
            break
        ei = bi + hold_days
        if ei >= n:
            break
        # 买入：申购费外扣
        shares = (1 - BUY_FEE) / navs[bi]
        # 赎回
        hd = (dates[ei] - dates[bi]).days
        gross = shares * navs[ei]
        net = gross * (1 - sell_fee(hd)) if fee_on else gross
        r = net - 1.0
        trades.append({'buy_date': dates[bi], 'buy_nav': navs[bi],
                       'sell_date': dates[ei], 'sell_nav': navs[ei],
                       'hold_cal_days': hd, 'ret': r * 100})
        j = ei + 1
    return trades


def stats(trades, capital=10000.0):
    if not trades:
        return None
    rets = [t['ret'] for t in trades]
    eq = capital
    peak = capital
    mdd = 0.0
    for r in rets:
        eq *= (1 + r / 100)
        peak = max(peak, eq)
        mdd = min(mdd, (eq - peak) / peak * 100)
    wins = [r for r in rets if r > 0]
    losses = [r for r in rets if r <= 0]
    return {
        'n': len(trades),
        'win_rate': 100.0 * len(wins) / len(rets),
        'avg': statistics.mean(rets),
        'median': statistics.median(rets),
        'avg_win': statistics.mean(wins) if wins else 0,
        'avg_loss': statistics.mean(losses) if losses else 0,
        'total_pct': (eq / capital - 1) * 100,
        'final': eq,
        'mdd': mdd,
        'best': max(rets), 'worst': min(rets),
        'trades': trades,
    }


def main():
    dates, navs = load()
    n = len(navs)
    ret = [0.0] + [(navs[i] / navs[i - 1] - 1) * 100 for i in range(1, n)]
    ma20 = [ma(navs, i, 20) for i in range(n)]
    ma60 = [ma(navs, i, 60) for i in range(n)]

    out = {}
    print('=' * 84)
    print('014414 做T回测  区间 %s ~ %s  (%d 个交易日)  最新净值 %.4f'
          % (dates[0], dates[-1], n, navs[-1]))
    print('费率假设: 申购 0.12%(1折) | 赎回 <7天 1.5% / >=7天 0%')
    print('=' * 84)

    # ---------- 基线：买入持有 ----------
    bh = (navs[-1] / navs[0] - 1) * 100
    print('\n【基线】全程买入持有: %+.2f%%  (%.4f -> %.4f)' % (bh, navs[0], navs[-1]))

    # ---------- 理论上限：完美择时 ----------
    for w in (5, 10, 20):
        ideal = 0.0
        cnt = 0
        for i in range(0, n - w, w):
            seg = navs[i:i + w + 1]
            lo, hi = min(seg), max(seg)
            ilo = seg.index(lo)
            seg2 = seg[ilo:]
            if len(seg2) > 1 and max(seg2) > lo:
                ideal += (max(seg2) / lo - 1) * 100
                cnt += 1
        print('【理论上限】每%d日窗口内"买最低卖最高"(后视镜): 累计 %+.1f%% (%d次)'
              % (w, ideal, cnt))

    # ---------- 策略 A：连跌买入 ----------
    print('\n' + '-' * 84)
    print('策略A  连跌N日后买入，持有M个交易日卖出（含费率，串行滚动）')
    print('-' * 84)
    print('%-22s %5s %7s %9s %9s %11s %9s' %
          ('规则', '次数', '胜率', '单次均值', '单次中位', '累计收益', '最大回撤'))
    resA = {}
    for k in (2, 3, 4):
        for M in (5, 8, 10, 15, 20):
            sig = [i for i in range(k, n - M - 1)
                   if all(ret[j] < 0 for j in range(i - k + 1, i + 1))]
            tr = run_trades(sig, dates, navs, M)
            st = stats(tr)
            if not st:
                continue
            key = '连跌%d日_持有%d' % (k, M)
            resA[key] = st
            print('%-22s %5d %6.1f%% %+8.2f%% %+8.2f%% %+10.1f%% %8.1f%%' %
                  (key, st['n'], st['win_rate'], st['avg'], st['median'],
                   st['total_pct'], st['mdd']))
    out['A'] = {k: {kk: vv for kk, vv in v.items() if kk != 'trades'}
                for k, v in resA.items()}

    # ---------- 策略 B：单日大跌买入 ----------
    print('\n' + '-' * 84)
    print('策略B  单日跌幅超X%后次日买入，持有M日卖出')
    print('-' * 84)
    print('%-22s %5s %7s %9s %9s %11s %9s' %
          ('规则', '次数', '胜率', '单次均值', '单次中位', '累计收益', '最大回撤'))
    resB = {}
    for X in (1.0, 1.5, 2.0, 3.0):
        for M in (5, 10, 15, 20):
            sig = [i for i in range(1, n - M - 1) if ret[i] <= -X]
            tr = run_trades(sig, dates, navs, M)
            st = stats(tr)
            if not st:
                continue
            key = '跌>%.1f%%_持有%d' % (X, M)
            resB[key] = st
            print('%-22s %5d %6.1f%% %+8.2f%% %+8.2f%% %+10.1f%% %8.1f%%' %
                  (key, st['n'], st['win_rate'], st['avg'], st['median'],
                   st['total_pct'], st['mdd']))
    out['B'] = {k: {kk: vv for kk, vv in v.items() if kk != 'trades'}
                for k, v in resB.items()}

    # ---------- 策略 C：均线偏离（低买高卖，动态平仓） ----------
    print('\n' + '-' * 84)
    print('策略C  跌破MA20达X%买入 → 回到MA20上方Y% 或 触及止损Z% 或 最长M日 平仓')
    print('-' * 84)

    def run_ma(buy_dev, sell_dev, stop, maxhold):
        trades = []
        i = 1
        pos = None
        while i < n - 1:
            if pos is None:
                m = ma20[i - 1]
                if m and navs[i - 1] <= m * (1 - buy_dev / 100):
                    bi = i
                    shares = (1 - BUY_FEE) / navs[bi]
                    pos = (bi, shares)
            else:
                bi, shares = pos
                m = ma20[i - 1]
                hd = (dates[i] - dates[bi]).days
                hit_sell = m and navs[i - 1] >= m * (1 + sell_dev / 100)
                hit_stop = navs[i - 1] / navs[bi] - 1 <= -stop / 100
                hit_time = (i - bi) >= maxhold
                if hd >= 7 and (hit_sell or hit_stop or hit_time or i == n - 2):
                    net = shares * navs[i] * (1 - sell_fee(hd))
                    trades.append({'buy_date': dates[bi], 'buy_nav': navs[bi],
                                   'sell_date': dates[i], 'sell_nav': navs[i],
                                   'hold_cal_days': hd, 'ret': (net - 1) * 100})
                    pos = None
            i += 1
        return trades

    print('%-26s %5s %7s %9s %11s %9s' %
          ('规则', '次数', '胜率', '单次均值', '累计收益', '最大回撤'))
    resC = {}
    for bd in (2, 3, 5):
        for sd in (0, 2):
            for st_ in (5, 8):
                tr = run_ma(bd, sd, st_, 40)
                s = stats(tr)
                if not s:
                    continue
                key = '破MA20-%d%%_回+%d%%_止损%d%%' % (bd, sd, st_)
                resC[key] = s
                print('%-26s %5d %6.1f%% %+8.2f%% %+10.1f%% %8.1f%%' %
                      (key, s['n'], s['win_rate'], s['avg'],
                       s['total_pct'], s['mdd']))
    out['C'] = {k: {kk: vv for kk, vv in v.items() if kk != 'trades'}
                for k, v in resC.items()}

    # ---------- 策略 D：网格（底仓反复做T） ----------
    print('\n' + '-' * 84)
    print('策略D  网格做T：以入场净值为基准，每跌 G%% 买入一份、每涨 G%% 卖出一份')
    print('        (模拟底仓反复高抛低吸；每笔独立计申赎费，持有<7天罚1.5%)')
    print('-' * 84)

    def run_grid(gap, capital=100000.0, layers=5):
        cash = capital
        units = 0.0
        base = navs[0]
        grid = base
        trades = []
        cost_basis = []      # (买入净值, 份额)
        for i in range(1, n):
            p = navs[i]
            # 下跌触发买入一格
            while p <= grid * (1 - gap / 100) and len(cost_basis) < layers \
                    and cash > 1000:
                amt = min(capital / layers, cash)
                sh = amt * (1 - BUY_FEE) / p
                cost_basis.append((p, sh, i))
                cash -= amt
                grid = p
            # 上涨触发卖出一格（配对最近一次买入，计算这笔 T 的盈亏）
            if cost_basis and p >= grid * (1 + gap / 100):
                bp, sh, bi = cost_basis.pop(0)
                hd = (dates[i] - dates[bi]).days
                gross = sh * p
                net = gross * (1 - sell_fee(hd))
                trades.append({'buy_date': dates[bi], 'buy_nav': bp,
                               'sell_date': dates[i], 'sell_nav': p,
                               'hold_cal_days': hd,
                               'ret': (net / (sh * bp) - 1) * 100})
                cash += net
                grid = p
        # 期末按市值清算剩余持仓
        leftover_mv = sum(sh * navs[-1] for _, sh, _ in cost_basis)
        leftover_cost = sum(sh * bp for bp, sh, _ in cost_basis)
        return trades, cash + leftover_mv, leftover_cost

    print('%-14s %5s %7s %9s %11s %11s' %
          ('网格间距', '次数', '胜率', '单次均值', '已实现T收益', '期末总资产'))
    resD = {}
    for gap in (1.5, 2.0, 3.0, 5.0):
        tr, total, lcost = run_grid(gap)
        s = stats(tr)
        if not s:
            continue
        resD['网格%.1f%%' % gap] = {
            'n': s['n'], 'win_rate': s['win_rate'], 'avg': s['avg'],
            'realized_pct': s['total_pct'],
            'final_total': total, 'leftover_cost': lcost,
            'total_pct': (total / 100000.0 - 1) * 100,
        }
        print('%-14s %5d %6.1f%% %+8.2f%% %+10.1f%% %11.0f元' %
              ('%.1f%%' % gap, s['n'], s['win_rate'], s['avg'],
               s['total_pct'], total))
        print('               └ 期末含浮亏持仓成本 %.0f 元；总资产 %.0f 元 = %+.2f%%'
              % (lcost, total, (total / 100000.0 - 1) * 100))
    out['D'] = resD

    # ---------- 费率敏感性 ----------
    print('\n' + '-' * 84)
    print('费率敏感性：同一策略，扣费 vs 不扣费（以"连跌3日买入持有10日"为例）')
    print('-' * 84)
    sig = [i for i in range(3, n - 11)
           if all(ret[j] < 0 for j in range(i - 2, i + 1))]
    for fo, lab in ((True, '扣费'), (False, '不扣费(理论)')):
        tr = run_trades(sig, dates, navs, 10, fee_on=fo)
        s = stats(tr)
        print('  %-14s 次数%3d 胜率%.1f%% 单次均值%+.3f%% 累计%+8.1f%%'
              % (lab, s['n'], s['win_rate'], s['avg'], s['total_pct']))
    # 短周期做T的费率灾难
    print('\n  短周期做T专属测算(持有<7天, 赎回费1.5%):')
    for M in (1, 2, 3, 5):
        sig = [i for i in range(3, n - M - 1)
               if all(ret[j] < 0 for j in range(i - 2, i + 1))]
        tr = run_trades(sig, dates, navs, M)
        s = stats(tr)
        if s:
            print('    连跌3日买入持有%d日: 次数%3d 胜率%.1f%% 单次均值%+.3f%% 累计%+8.1f%%'
                  % (M, s['n'], s['win_rate'], s['avg'], s['total_pct']))

    with open('t_trade_result.json', 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=1, default=str)
    print('\n结果已保存 t_trade_result.json')


if __name__ == '__main__':
    main()
