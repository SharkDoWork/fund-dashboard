# -*- coding: utf-8 -*-
"""
Backtest T-trading strategies on 014414 for the most recent month.

IMPORTANT (no look-ahead bias):
  Off-fund (OTC) orders placed before 15:00 on day t are filled at day-t NAV,
  which is UNKNOWN at decision time. So every strategy here must decide using
  information up to day t-1 close, and execute at day-t NAV.
  Any backtest that says "buy because today dropped" is cheating.
"""
import sqlite3
import itertools
import statistics

DB = 'data/fund_history.db'
CODE = '014414'
START = '2026-08-03'          # one month window
CAP = 270000.0                # user's actual position
LOT = 8                       # split position into 8 slices

# Cost model (verified earlier)
BUY_FEE_OTC = 0.0012          # A-class subscription fee, discounted
SELL_FEE_OTC = 0.0            # redemption fee = 0 when holding >= 7 days (FIFO, base position is old)
COST_ETF = 0.0002             # exchange-traded ETF commission per side

con = sqlite3.connect(DB)
rows = con.execute(
    'SELECT trade_date, dwjz FROM nav_history WHERE fund_code=? AND dwjz IS NOT NULL '
    'AND trade_date>=? ORDER BY trade_date', (CODE, START)).fetchall()
con.close()

dates = [r[0] for r in rows]
nav = [r[1] for r in rows]
n = len(nav)
# ret[0] undefined -> 0.0 so day-1 produces no signal (we have no prior close yet)
ret = [0.0] + [(nav[i] / nav[i - 1] - 1) * 100 for i in range(1, n)]

print('=' * 78)
print('014414 last month  %s -> %s   (%d trading days)' % (dates[0], dates[-1], n))
print('=' * 78)
print('NAV: %.4f -> %.4f   buy&hold %+.2f%%' % (nav[0], nav[-1], (nav[-1] / nav[0] - 1) * 100))
lo, hi = min(nav), max(nav)
print('Range: low %.4f (%s)   high %.4f (%s)   amplitude %.2f%%'
      % (lo, dates[nav.index(lo)], hi, dates[nav.index(hi)], (hi / lo - 1) * 100))
print('Daily: up %d / down %d   mean |move| %.2f%%   stdev %.2f%%'
      % (sum(1 for r in ret[1:] if r > 0), sum(1 for r in ret[1:] if r < 0),
         statistics.mean([abs(r) for r in ret[1:]]), statistics.pstdev(ret[1:])))
print('Max single-day up %+.2f%% (%s)   down %+.2f%% (%s)'
      % (max(ret[1:]), dates[ret.index(max(ret[1:]))],
         min(ret[1:]), dates[ret.index(min(ret[1:]))]))
print()


def run(signal_fn, cost_buy, cost_sell, lot=LOT, cap=CAP, mode='reverse'):
    """
    signal_fn(i) -> 'B' (buy 1 slice), 'S' (sell 1 slice), None
    Called on day i, may ONLY use data with index <= i-1 (no look-ahead).

    mode='reverse' (倒T): sell from the existing base position on rallies,
        buy back on dips. Cash starts at 0, shares start at full base.
        Constraint: cannot sell more than `lot` slices below the base.

    mode='long' (正T): buy extra slices on dips using a cash reserve,
        then sell those extra slices on rallies.
        Constraint: can only sell slices acquired by T-buys, never the base.
    """
    shares0 = cap / nav[0]              # base position held from day 0
    slice_shares = shares0 / lot

    if mode == 'reverse':
        shares = shares0
        cash = 0.0
    else:
        # reserve 3 slices of cash; base stays untouched
        cash = slice_shares * nav[0] * 3
        shares = shares0 - (cap / nav[0]) * (3.0 / lot) * 0  # base kept, cash is extra
        shares = shares0
        cash = slice_shares * nav[0] * 3
        cap_total = cap + cash           # total capital under management
    trades = []
    open_lots = []          # FIFO queue of T-buys not yet closed: [shares, price]
    realized = 0.0          # closed round-trip spread, net of costs  <-- THE T P&L

    for i in range(1, n):
        sig = signal_fn(i)
        px = nav[i]
        if sig == 'B':
            cost = slice_shares * px * (1 + cost_buy)
            if cash >= cost:
                shares += slice_shares
                cash -= cost
                open_lots.append([slice_shares, px * (1 + cost_buy)])
                trades.append((dates[i], 'BUY', px, slice_shares))
        elif sig == 'S':
            ok = (shares >= slice_shares) if mode == 'reverse' \
                else (shares - shares0 >= slice_shares - 1e-9)
            if ok:
                shares -= slice_shares
                cash += slice_shares * px * (1 - cost_sell)
                trades.append((dates[i], 'SELL', px, slice_shares))
                # FIFO close
                need = slice_shares
                while need > 1e-9 and open_lots:
                    lot_sh, lot_px = open_lots[0]
                    take = min(lot_sh, need)
                    realized += take * (px * (1 - cost_sell) - lot_px)
                    open_lots[0][0] -= take
                    need -= take
                    if open_lots[0][0] <= 1e-9:
                        open_lots.pop(0)

    # unrealized P&L of slices still open at window end (directional, NOT T profit)
    open_shares = sum(l[0] for l in open_lots)
    open_cost = sum(l[0] * l[1] for l in open_lots)
    unrealized = open_shares * nav[-1] - open_cost

    end_value = shares * nav[-1] + cash
    if mode == 'reverse':
        base_end = shares0 * nav[-1]
        baseline_cap = cap
    else:
        base_end = shares0 * nav[-1] + slice_shares * nav[0] * 3   # base + idle cash
        baseline_cap = cap + slice_shares * nav[0] * 3
    return {
        'end_value': end_value,
        'base_end': base_end,
        't_pnl': end_value - base_end,          # incremental P&L from T-trading
        'total_pnl': end_value - baseline_cap,
        'realized': realized,                   # closed round trips only (TRUE T profit)
        'unrealized': unrealized,               # open slices at window end (directional)
        'open_shares': open_shares,
        'trades': trades,
        'cash_left': cash,
        'shares_end': shares,
    }


def fmt_money(x):
    return '{:+,.0f}'.format(x)


# ---------------------------------------------------------------- strategies
results = []

# 1) Buy & hold baseline
base_end = CAP / nav[0] * nav[-1]
results.append(('Buy & Hold (baseline)', None, None, base_end, 0.0, base_end - CAP, []))

# 2) Reverse-T (倒T): rally -> sell from base, dip -> buy back
for a in (0.5, 1.0, 1.5, 2.0):
    for b in (0.5, 1.0, 1.5, 2.0):
        def f(i, a=a, b=b):
            if ret[i - 1] >= a:
                return 'S'
            if ret[i - 1] <= -b:
                return 'B'
            return None
        r = run(f, BUY_FEE_OTC, SELL_FEE_OTC, mode='reverse')
        results.append(('Reverse-T (OTC-A): rally>=%.1f%% sell / dip>=%.1f%% buy' % (a, b),
                        'OTC-A', r, r['end_value'], r['t_pnl'], r['total_pnl'], r['trades']))

# 3) Long-T (正T): dip -> buy extra with cash reserve, rally -> sell the extra
for a in (0.5, 1.0, 1.5, 2.0):
    for b in (0.5, 1.0, 1.5, 2.0):
        def f(i, a=a, b=b):
            if ret[i - 1] <= -a:
                return 'B'
            if ret[i - 1] >= b:
                return 'S'
            return None
        r = run(f, BUY_FEE_OTC, SELL_FEE_OTC, mode='long')
        results.append(('Long-T (OTC-A): dip>=%.1f%% buy / rally>=%.1f%% sell' % (a, b),
                        'OTC-A', r, r['end_value'], r['t_pnl'], r['total_pnl'], r['trades']))

# 4) Grid (anchor moves with each fill) -- ETF cost
for g in (1.0, 1.5, 2.0, 3.0):
    def make(g=g):
        state = {'anchor': nav[0], 'last': nav[0]}

        def f(i):
            px_prev = nav[i - 1]
            if px_prev <= state['anchor'] * (1 - g / 100):
                state['anchor'] = px_prev
                return 'B'
            if px_prev >= state['anchor'] * (1 + g / 100):
                state['anchor'] = px_prev
                return 'S'
            return None
        return f
    r_otc = run(make(), BUY_FEE_OTC, SELL_FEE_OTC, mode='reverse')
    r_etf = run(make(), COST_ETF, COST_ETF, mode='reverse')
    results.append(('Grid %.1f%% (OTC A-class)' % g, 'OTC-A', r_otc,
                    r_otc['end_value'], r_otc['t_pnl'], r_otc['total_pnl'], r_otc['trades']))
    results.append(('Grid %.1f%% (ETF 516670)' % g, 'ETF', r_etf,
                    r_etf['end_value'], r_etf['t_pnl'], r_etf['total_pnl'], r_etf['trades']))

# 5) Reverse-T re-run on ETF cost (low friction -> the realistic T vehicle)
for a, b in ((0.5, 0.5), (0.5, 1.0), (1.0, 0.5), (1.0, 1.0), (1.0, 1.5), (1.5, 1.0), (1.5, 1.5), (2.0, 2.0)):
    def f(i, a=a, b=b):
        if ret[i - 1] >= a:
            return 'S'
        if ret[i - 1] <= -b:
            return 'B'
        return None
    r = run(f, COST_ETF, COST_ETF, mode='reverse')
    results.append(('Reverse-T %.1f/%.1f (ETF 516670)' % (a, b), 'ETF', r,
                    r['end_value'], r['t_pnl'], r['total_pnl'], r['trades']))
    r2 = run(f, COST_ETF, COST_ETF, mode='long')
    results.append(('Long-T %.1f/%.1f (ETF 516670)' % (a, b), 'ETF', r2,
                    r2['end_value'], r2['t_pnl'], r2['total_pnl'], r2['trades']))

# ---------------------------------------------------------------- printing
print('=' * 78)
print('STRATEGY BACKTEST  (base position %s CNY, %d slices, end value incl. cash)' % (fmt_money(CAP), LOT))
print('=' * 78)
print('%-46s %9s %9s %9s %6s' % ('strategy', 'realized', 'open MTM', 'total', 'fills'))
print('-' * 78)

rows_out = []
for name, venue, r, endv, tpnl, totpnl, trades in results:
    rows_out.append((name, venue, r, totpnl, len(trades), trades))
    if name.startswith('Buy & Hold'):
        print('%-46s %9s %9s %9s %6d' % (name[:46], '-', '-', fmt_money(totpnl), 0))
    else:
        print('%-46s %9s %9s %9s %6d'
              % (name[:46], fmt_money(r['realized']), fmt_money(r['unrealized']),
                 fmt_money(totpnl), len(trades)))

print()
print('  realized = closed round trips only (the honest T profit)')
print('  open MTM = slices bought but not yet sold at window end (directional bet)')

print()
print('=' * 78)
print('RANKING BY REALIZED (closed T spread) -- top 10')
print('=' * 78)
ranked = sorted([x for x in rows_out if not x[0].startswith('Buy & Hold')],
                key=lambda x: -x[2]['realized'])
print('%-46s %9s %9s %6s' % ('strategy', 'realized', 'open MTM', 'fills'))
print('-' * 78)
for name, venue, r, totpnl, ntr, trades in ranked[:10]:
    print('%-46s %9s %9s %6d'
          % (name[:46], fmt_money(r['realized']), fmt_money(r['unrealized']), ntr))

print()
print('  MEDIAN across all %d parameter sets: realized %s'
      % (len(ranked), fmt_money(statistics.median([x[2]['realized'] for x in ranked]))))
pos = sum(1 for x in ranked if x[2]['realized'] > 0)
print('  Positive realized: %d / %d (%.0f%%)' % (pos, len(ranked), 100 * pos / len(ranked)))

print()
print('=' * 78)
print('DETAIL: top 4 by realized')
print('=' * 78)
for name, venue, r, totpnl, ntr, trades in ranked[:4]:
    print()
    print('  %s  [%s]' % (name, venue))
    print('  realized %s   open MTM %s   total %s   fills %d'
          % (fmt_money(r['realized']), fmt_money(r['unrealized']), fmt_money(totpnl), ntr))
    for d, side, px, sh in trades:
        print('     {}  {:<4s} @ {:.4f}  x {:,.0f} shares'.format(d, side, px, sh))

# ---------------------------------------------------------------- perfect timing ceiling
print()
print('=' * 78)
print('UPPER BOUND: perfect daily timing (impossible for OTC, theoretical)')
print('=' * 78)
# if you could buy every local dip and sell every local peak with 1 slice
perfect = 0.0
for i in range(1, n - 1):
    if nav[i] < nav[i - 1] and nav[i] < nav[i + 1]:
        perfect += (nav[i + 1] / nav[i] - 1)
    elif nav[i] > nav[i - 1] and nav[i] > nav[i + 1]:
        perfect += (nav[i] / nav[i + 1] - 1)
slice_val = CAP / LOT
print('Sum of perfect swing returns: %+.2f%% (per slice)' % (perfect * 100))
print('Applied to full %s position: %s CNY over the month'
      % (fmt_money(CAP), fmt_money(perfect * CAP)))
print('NOTE: needs intraday fills at exact daily extremes -> ETF only, and unrealistic.')
print('      This is a ceiling, not an achievable number.')

# ---------------------------------------------------------------- cost sensitivity
print()
print('=' * 78)
print('COST SENSITIVITY on best reverse-T (1.0/1.0)')
print('=' * 78)
def f(i):
    if ret[i - 1] >= 1.0:
        return 'S'
    if ret[i - 1] <= -1.0:
        return 'B'
    return None
for label, cb, cs, extra in (
        ('OTC A-class 0.12% buy / 0% sell', 0.0012, 0.0, 0.0),
        ('OTC C-class 0% fee / 0.4%/yr', 0.0, 0.0, -CAP * 0.004 / 12),
        ('ETF 516670 0.02% both sides', 0.0002, 0.0002, 0.0),
        ('Frictionless (zero cost)', 0.0, 0.0, 0.0),
):
    r = run(f, cb, cs)
    print('  %-34s T-pnl %10s   (incl. carry %s)  fills %d'
          % (label, fmt_money(r['t_pnl'] + extra), fmt_money(extra), len(r['trades'])))

print()
print('C-class 0.4%/yr sales service fee is accrued in NAV, i.e. already deducted')
print('from the price series -- modelled above as a separate monthly carry cost.')
