# -*- coding: utf-8 -*-
"""
Robustness follow-ups flagged in report section 7:
A) Bottom-regime definition sensitivity (0.80 / 0.82 / 0.85 / 0.88 + percentile variants)
B) Trigger-grid for the profit-lock variant on unbiased windows
C) Operation card for the current phase: concrete trigger prices from last nav
"""
import sqlite3
import datetime
import statistics
from collections import deque

DB = 'data/fund_history.db'
CODE = '014414'
CAP = 270000.0
SLICE = 50000.0
MIN_HOLD = 7

con = sqlite3.connect(DB)
rows = con.execute(
    'SELECT trade_date,dwjz FROM nav_history WHERE fund_code=? '
    'AND dwjz IS NOT NULL ORDER BY trade_date', (CODE,)).fetchall()
dates = [r[0] for r in rows]
nav = [r[1] for r in rows]
n = len(nav)
dobj = [datetime.date.fromisoformat(x) for x in dates]
ret = [None] + [(nav[i] / nav[i - 1] - 1) * 100 for i in range(1, n)]


def fmt(x):
    return '{:+,.0f}'.format(x)


def engine(i0, i1, trig_sell, trig_buy, buy_fee=0.0002, settle=0,
           profit_lock=None, bottom=None):
    lots = deque([[-9999, CAP / nav[i0]]])
    legacy_sh = CAP / nav[i0]
    cash = 0.0
    pending = []
    realized = 0.0
    opens = deque()
    sells = buys = 0
    waits = []

    for i in range(i0 + 1, i1 + 1):
        keep = []
        for av, amt in pending:
            if av <= i:
                cash += amt
            else:
                keep.append((av, amt))
        pending = keep
        r = ret[i - 1]
        if r is None:
            continue
        if bottom is not None and nav[i - 1] > bottom:
            continue

        if r >= trig_sell:
            sh_need = SLICE / nav[i]
            if sum(sh for _, sh in lots) >= sh_need:
                need = sh_need
                gross = fee = 0.0
                while need > 1e-9 and lots:
                    lidx, lsh = lots[0]
                    take = min(lsh, need)
                    age = 9999 if lidx < 0 else (dobj[i] - dobj[lidx]).days
                    g = take * nav[i]
                    gross += g
                    if age < MIN_HOLD:
                        fee += g * 0.015
                    lots[0][1] -= take
                    need -= take
                    if lots[0][1] <= 1e-9:
                        lots.popleft()
                realized -= fee
                pending.append((i + settle, gross - fee))
                opens.append([nav[i], sh_need, i])
                sells += 1
        elif opens:
            if profit_lock is None:
                want = (r <= -trig_buy)
            else:
                want = (nav[i] <= opens[0][0] * (1 - profit_lock))
            if want and cash >= SLICE:
                fee = SLICE * buy_fee
                sh = (SLICE - fee) / nav[i]
                lots.append([i, sh])
                cash -= SLICE
                realized -= fee
                sold_px, sold_sh, sidx = opens.popleft()
                pair = min(sold_sh, sh)
                realized += pair * (sold_px - nav[i])
                if sold_sh > pair:
                    opens.appendleft([sold_px, sold_sh - pair, sidx])
                waits.append((dobj[i] - dobj[sidx]).days)
                buys += 1

    end_val = sum(sh for _, sh in lots) * nav[i1] + cash + sum(a for _, a in pending)
    base = legacy_sh * nav[i1]
    return dict(realized=realized, total=end_val - base, sells=sells, buys=buys,
                waits=waits)


W = 60

print('=' * 94)
print('A) BOTTOM-THRESHOLD SENSITIVITY (unbiased 60d windows, ETF T+0)')
print('=' * 94)
sv = sorted(nav)
pcts = {'p20': sv[int(n * .20)], 'p25': sv[int(n * .25)], 'p30': sv[int(n * .30)]}
thresholds = [('fixed 0.80', 0.80), ('fixed 0.82', 0.82), ('fixed 0.85', 0.85),
              ('fixed 0.88', 0.88), ('p20 %.4f' % pcts['p20'], pcts['p20']),
              ('p25 %.4f' % pcts['p25'], pcts['p25']),
              ('p30 %.4f' % pcts['p30'], pcts['p30'])]
print('  percentile navs: ' + ', '.join('%s=%.4f' % (k, v) for k, v in pcts.items()))
print()
print('%-16s %6s %11s %11s %7s %11s %11s %7s'
      % ('threshold', 'wins', 'mech mean', 'mech win%', '',
         'lock mean', 'lock win%', ''))
print('-' * 94)
for label, b in thresholds:
    wins = [i for i in range(n - W) if nav[i] <= b]
    if len(wins) < 30:
        print('%-16s %6d  (insufficient windows)' % (label, len(wins)))
        continue
    mech = [engine(i, i + W, 1.0, 1.0, bottom=b)['total'] for i in wins]
    lock = [engine(i, i + W, 1.0, 0, profit_lock=0.01, bottom=b)['total'] for i in wins]
    print('%-16s %6d %11s %10.0f%% %7s %11s %10.0f%% %7s'
          % (label, len(wins), fmt(statistics.mean(mech)),
             100 * sum(1 for x in mech if x > 0) / len(mech), '',
             fmt(statistics.mean(lock)),
             100 * sum(1 for x in lock if x > 0) / len(lock), ''))
print()
print('  -> if the sign is negative across ALL thresholds, the conclusion')
print('     does not hinge on where the 0.85 line was drawn.')


print()
print('=' * 94)
print('B) PROFIT-LOCK TRIGGER GRID (threshold 0.85, unbiased 60d windows)')
print('=' * 94)
wins = [i for i in range(n - W) if nav[i] <= 0.85]
print('%-18s %9s %9s %7s %9s %9s %7s'
      % ('variant', 'mean', 'median', 'win%', 'best', 'worst', 'fills/w'))
print('-' * 94)
best_mean = None
for lock in (0.005, 0.01, 0.015, 0.02, 0.03):
    for tsell in (0.5, 1.0, 1.5, 2.0):
        tots = []
        fills = []
        for i in wins:
            r = engine(i, i + W, tsell, 0, profit_lock=lock)
            tots.append(r['total'])
            fills.append(r['sells'])
        m = statistics.mean(tots)
        print('%-18s %9s %9s %6.0f%% %9s %9s %7.1f'
              % ('lock %.1f%%/s %.1f%%' % (lock * 100, tsell), fmt(m),
                fmt(statistics.median(tots)),
                100 * sum(1 for x in tots if x > 0) / len(tots),
                fmt(max(tots)), fmt(min(tots)), statistics.mean(fills)))
        if best_mean is None or m > best_mean[1]:
            best_mean = (('lock %.1f%%/s %.1f%%' % (lock * 100, tsell), m))
print()
print('  best cell: %s at %s/window' % (best_mean[0], fmt(best_mean[1])))
print('  NOTE: this best cell is still an in-sample pick -> treat as upper')
print('  bound, not expectation (walk-forward already showed picking loses).')


print()
print('=' * 94)
print('C) OPERATION CARD -- current phase (as of 2026-09-03, nav 0.7726)')
print('=' * 94)
last = nav[-1]
print('  position  : 270k legacy (all lots > 7 days) + up to 4x50k rolling cash')
print('  FIFO state: 5.4 sell-slots protected; used 0 since window start')
print()
print('  SELL trigger (any day whose PREVIOUS day close-to-close >= +1.0%%):')
print('    yesterday-nav >= %.4f  -> sell 5w at today close' % (last / 1.01))
print('    (in practice: check at 14:30, if the ETF/index is up >= 1%% vs')
print('     yesterday close, place the redemption/ETF sell before 15:00)')
print()
print('  BUY-BACK rule (profit-lock 1%%):')
print('    buy 5w back only at nav <= 0.99 x your sell price')
print('    e.g. sold at 0.7800 -> buy back only <= 0.7722')
print()
print('  PROTECTION:')
print('    - stop selling after 4 sells in any rolling 7 calendar days')
print('      (6+ would punch through the FIFO cushion; 4 leaves margin)')
print('    - stop ALL T activity when nav > 0.85 or pork price +15%% in a month')
print('      -> go full position')
print()
print('  Reality check on recent prices:')
recent = [(dates[i], nav[i], '%+.2f%%' % ((nav[i] / nav[i - 1] - 1) * 100))
          for i in range(n - 12, n)]
for d, v, r in recent:
    sell_today = 'SELL' if (nav[dates.index(d) - 1] / nav[dates.index(d) - 2] - 1) * 100 >= 1.0 else '    '
    print('    %s  nav %.4f  chg %s   %s' % (d, v, r, sell_today))
print()
print('  Last 12 days: how many sell signals fired? %d' % sum(
    1 for i in range(n - 12, n)
    if (nav[i - 1] / nav[i - 2] - 1) * 100 >= 1.0))
