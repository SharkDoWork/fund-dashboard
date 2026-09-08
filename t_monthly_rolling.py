# -*- coding: utf-8 -*-
"""
Rolling month-by-month test of the T strategies.

The single-month backtest picked a "best" parameter set out of 56 candidates on
4 round trips -- statistically meaningless. This script runs the SAME parameter
set on every calendar month since 2022-05 and reports the distribution of
monthly realized spread, so we can tell persistence from luck.
"""
import sqlite3
import statistics
from collections import OrderedDict

DB = 'data/fund_history.db'
CODE = '014414'
CAP = 270000.0
LOT = 8

BUY_FEE_OTC = 0.0012
SELL_FEE_OTC = 0.0
COST_ETF = 0.0002

con = sqlite3.connect(DB)
rows = con.execute(
    'SELECT trade_date, dwjz FROM nav_history WHERE fund_code=? AND dwjz IS NOT NULL '
    'ORDER BY trade_date', (CODE,)).fetchall()
con.close()

all_dates = [r[0] for r in rows]
all_nav = [r[1] for r in rows]


def months():
    """Split the whole series into calendar months."""
    buckets = OrderedDict()
    for i, d in enumerate(all_dates):
        ym = d[:7]
        buckets.setdefault(ym, []).append(i)
    return buckets


def month_run(idxs, signal_fn, cost_buy, cost_sell, mode='long', lot=LOT, cap=CAP):
    """Run one strategy inside a single calendar month (indices into global series)."""
    # global returns so that the first day of a month also has a prior-day return
    gret = {}
    for i in range(1, len(all_nav)):
        gret[i] = (all_nav[i] / all_nav[i - 1] - 1) * 100

    nav0 = all_nav[idxs[0]]
    shares0 = cap / nav0
    slice_shares = shares0 / lot

    if mode == 'long':
        cash = slice_shares * nav0 * 3
        shares = shares0
        baseline = shares0 * all_nav[idxs[-1]] + slice_shares * nav0 * 3
    else:
        cash = 0.0
        shares = shares0
        baseline = shares0 * all_nav[idxs[-1]]

    # Direction-agnostic FIFO position book.
    #   long  (正T): BUY  opens  +shares, SELL closes
    #   short (倒T): SELL opens  -shares, BUY  closes
    # A trade CLOSES against the oldest opposite-direction lot, otherwise OPENS.
    book = []               # [[dir, shares, eff_px], ...]  dir = +1 long / -1 short
    realized = 0.0
    fills = 0

    def apply(dir_, sh, px):
        """dir_ = +1 buy, -1 sell. Close against oldest opposite lot, else open."""
        nonlocal realized
        eff = px * (1 + cost_buy) if dir_ > 0 else px * (1 - cost_sell)
        need = sh
        while need > 1e-9 and book and book[0][0] == -dir_:
            lot_sh, lot_px = book[0][1], book[0][2]
            take = min(lot_sh, need)
            # closing a lot opened at -dir_ : pnl = (eff - lot_px) * (-dir_)
            realized += take * (eff - lot_px) * (-dir_)
            book[0][1] -= take
            need -= take
            if book[0][1] <= 1e-9:
                book.pop(0)
        if need > 1e-9:
            book.append([dir_, need, eff])

    for i in idxs:
        if i not in gret:
            continue
        sig = signal_fn(i, gret)
        px = all_nav[i]
        if sig == 'B':
            cost = slice_shares * px * (1 + cost_buy)
            if cash >= cost:
                shares += slice_shares
                cash -= cost
                apply(+1, slice_shares, px)
                fills += 1
        elif sig == 'S':
            ok = (shares >= slice_shares) if mode == 'reverse' \
                else (shares - shares0 >= slice_shares - 1e-9)
            if ok:
                shares -= slice_shares
                cash += slice_shares * px * (1 - cost_sell)
                apply(-1, slice_shares, px)
                fills += 1

    end_nav = all_nav[idxs[-1]]
    unrealized = 0.0
    for d, sh, px in book:
        unrealized += sh * (end_nav - px) if d > 0 else sh * (px - end_nav)

    end_value = shares * end_nav + cash
    return realized, unrealized, fills, end_value - baseline


def make_rev(a, b):
    def f(i, gret):
        if gret.get(i, 0) >= a:
            return 'S'
        if gret.get(i, 0) <= -b:
            return 'B'
        return None
    return f


def make_long(a, b):
    def f(i, gret):
        if gret.get(i, 0) <= -a:
            return 'B'
        if gret.get(i, 0) >= b:
            return 'S'
        return None
    return f


def rolling_windows(win=22):
    """Non-overlapping windows of `win` trading days, aligned to the newest data."""
    n_all = len(all_dates)
    end = n_all
    out = []
    while end - win >= 0:
        out.append(list(range(end - win, end)))
        end -= win
    return list(reversed(out))


WINDOW_MODE = 'rolling'      # 'calendar' | 'rolling'
if WINDOW_MODE == 'calendar':
    buckets = months()
    yms = [k for k in buckets if len(buckets[k]) >= 10]
    labels = yms
else:
    buckets = OrderedDict()
    for w in rolling_windows(22):
        buckets['%s~%s' % (all_dates[w[0]], all_dates[w[-1]])] = w
    yms = list(buckets.keys())

STRATS = [
    ('Long-T  0.5/1.0  ETF', make_long(0.5, 1.0), COST_ETF, COST_ETF, 'long'),
    ('Long-T  1.0/0.5  ETF', make_long(1.0, 0.5), COST_ETF, COST_ETF, 'long'),
    ('Long-T  0.5/0.5  ETF', make_long(0.5, 0.5), COST_ETF, COST_ETF, 'long'),
    ('Rev-T   0.5/0.5  ETF', make_rev(0.5, 0.5), COST_ETF, COST_ETF, 'reverse'),
    ('Rev-T   1.5/1.0  ETF', make_rev(1.5, 1.0), COST_ETF, COST_ETF, 'reverse'),
    ('Long-T  1.0/0.5  OTC-A', make_long(1.0, 0.5), BUY_FEE_OTC, 0.0, 'long'),
    ('Rev-T   0.5/0.5  OTC-A', make_rev(0.5, 0.5), BUY_FEE_OTC, 0.0, 'reverse'),
]

print('=' * 92)
print('ROLLING MONTHLY TEST  (%s, base %s, %d slices)  months: %d (%s..%s)'
      % (CODE, '{:,.0f}'.format(CAP), LOT, len(yms), yms[0], yms[-1]))
print('=' * 92)
print('%-22s %9s %9s %9s %9s %6s' %
      ('strategy', 'real/mo', 'TOTAL/mo', 'medTOT', 'win%TOT', 'fills'))
print('-' * 92)
print('  real/mo = closed round trips only   |   TOTAL/mo = realized + open MTM (honest)')
print('-' * 92)

summary = []
for name, fn, cb, cs, mode in STRATS:
    reals, uns, fills_l, tots = [], [], [], []
    for ym in yms:
        r, u, f, t = month_run(buckets[ym], fn, cb, cs, mode=mode)
        reals.append(r); uns.append(u); fills_l.append(f); tots.append(t)
    win_t = 100 * sum(1 for x in tots if x > 0) / len(tots)
    print('%-22s %9s %9s %9s %8.0f%% %6.1f'
          % (name, '{:+,.0f}'.format(statistics.mean(reals)),
             '{:+,.0f}'.format(statistics.mean(tots)),
             '{:+,.0f}'.format(statistics.median(tots)),
             win_t, statistics.mean(fills_l)))
    summary.append((name, reals, tots, uns, fills_l))

print()
print('  If real/mo >> TOTAL/mo, the strategy is dumping losing positions into')
print('  month-end unrealized -- the gap is the survivorship bias.')

print()
print('=' * 92)
print('YEAR-BY-YEAR AGGREGATE (reverse-T 0.5/0.5 ETF -- the robust one)')
print('=' * 92)
IDX_REV = [i for i, s in enumerate(STRATS) if s[0].startswith('Rev-T   0.5/0.5  ETF')][0]
name, reals, tots, uns, fills_l = summary[IDX_REV]
by_year = {}
for ym, r, t in zip(yms, reals, tots):
    by_year.setdefault(str(ym)[:4], []).append((r, t))
print('%-8s %11s %11s %11s %8s' % ('year', 'sum REAL', 'sum TOTAL', 'gap', 'windows'))
print('-' * 92)
for y in sorted(by_year):
    v = by_year[y]
    sr = sum(x[0] for x in v); st = sum(x[1] for x in v)
    print('%-8s %11s %11s %11s %8d' % (y, '{:+,.0f}'.format(sr), '{:+,.0f}'.format(st),
                                       '{:+,.0f}'.format(sr - st), len(v)))
print('  (gap here is NEGATIVE: reverse-T leaves its WINNERS open at window end,')

print()
print('  Distribution of the 48 window TOTALs for reverse-T 0.5/0.5 ETF:')
sv = sorted(tots)
q = lambda p: sv[min(len(sv) - 1, int(len(sv) * p))]
print('    worst %s   P10 %s   P25 %s   median %s   P75 %s   P90 %s   best %s'
      % tuple('{:+,.0f}'.format(x) for x in
              (sv[0], q(.10), q(.25), q(.50), q(.75), q(.90), sv[-1])))
worst_i = tots.index(min(tots)); best_i = tots.index(max(tots))
print('    worst window : %s  ->  %s' % (yms[worst_i], '{:+,.0f}'.format(tots[worst_i])))
print('    best  window : %s  ->  %s' % (yms[best_i], '{:+,.0f}'.format(tots[best_i])))
print('    annualised expectation (mean TOTAL x 12): %s CNY/yr  =  %.2f%% of capital'
      % ('{:+,.0f}'.format(statistics.mean(tots) * 12),
         100 * statistics.mean(tots) * 12 / CAP))

print()
print('=' * 92)
print('THE LAST MONTH IN CONTEXT')
print('=' * 92)
last = yms[-1]
print('Month analysed: %s   (%d trading days)' % (last, len(buckets[last])))
for name, reals, tots, uns, fills_l in summary:
    print('  %-22s real %+8s  TOTAL %+8s  | hist median TOTAL %+8s'
          % (name, '{:,.0f}'.format(reals[-1]), '{:,.0f}'.format(tots[-1]),
             '{:,.0f}'.format(statistics.median(tots))))

print()
print('=' * 92)
print('HOW OFTEN WAS A MONTH AS GOOD AS THE ONE WE JUST ANALYSED? (TOTAL basis)')
print('=' * 92)
for nm, idx in (('Long-T 0.5/1.0 ETF', 0), ('Rev-T  0.5/0.5 ETF', IDX_REV)):
    this_val = summary[idx][2][-1]
    all_vals = summary[idx][2]
    better = sum(1 for x in all_vals if x >= this_val)
    print('  %s this window TOTAL: %s' % (nm, '{:+,.0f}'.format(this_val)))
    print('    windows at or above this: %d / %d (%.0f%%)  ->  %.0fth percentile'
          % (better, len(all_vals), 100 * better / len(all_vals),
             100 * better / len(all_vals)))
    print()
