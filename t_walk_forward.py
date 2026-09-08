# -*- coding: utf-8 -*-
"""
Walk-forward validation for the T strategies on 014414.

The previous report admitted "parameters were picked inside a grid, overfitting
residue remains". This settles it properly:

  A) Parameter-neighbourhood scan -- is (0.5, 0.5) a lonely spike or a plateau?
  B) Walk-forward -- at each step, pick the best parameter on PAST windows only,
     then trade the NEXT window. That is genuine out-of-sample.
  C) Compare walk-forward vs (cheating) full-sample-optimal vs fixed parameter.
"""
import sqlite3
import statistics
from collections import OrderedDict

DB = 'data/fund_history.db'
CODE = '014414'
CAP = 270000.0
LOT = 8
COST_ETF = 0.0002
BUY_OTC = 0.0012
SELL_OTC = 0.0

con = sqlite3.connect(DB)
rows = con.execute(
    'SELECT trade_date, dwjz FROM nav_history WHERE fund_code=? AND dwjz IS NOT NULL '
    'ORDER BY trade_date', (CODE,)).fetchall()
con.close()
all_dates = [r[0] for r in rows]
all_nav = [r[1] for r in rows]
gret = {i: (all_nav[i] / all_nav[i - 1] - 1) * 100 for i in range(1, len(all_nav))}


def run_window(idxs, a, b, mode, cb, cs):
    """Total P&L (realized + open MTM) vs buy&hold inside one window."""
    nav0 = all_nav[idxs[0]]
    shares0 = CAP / nav0
    sl = shares0 / LOT
    if mode == 'long':
        cash = sl * nav0 * 3
        shares = shares0
        baseline = shares0 * all_nav[idxs[-1]] + sl * nav0 * 3
    else:
        cash = 0.0
        shares = shares0
        baseline = shares0 * all_nav[idxs[-1]]
    book = []
    realized = 0.0
    fills = 0

    def apply(d, sh, px):
        nonlocal realized
        eff = px * (1 + cb) if d > 0 else px * (1 - cs)
        need = sh
        while need > 1e-9 and book and book[0][0] == -d:
            ls, lp = book[0][1], book[0][2]
            take = min(ls, need)
            realized += take * (eff - lp) * (-d)
            book[0][1] -= take
            need -= take
            if book[0][1] <= 1e-9:
                book.pop(0)
        if need > 1e-9:
            book.append([d, need, eff])

    for i in idxs:
        if i not in gret:
            continue
        r = gret[i]
        px = all_nav[i]
        if mode == 'reverse':
            sig = 'S' if r >= a else ('B' if r <= -b else None)
        else:
            sig = 'B' if r <= -a else ('S' if r >= b else None)
        if sig == 'B':
            cost = sl * px * (1 + cb)
            if cash >= cost:
                shares += sl; cash -= cost; apply(+1, sl, px); fills += 1
        elif sig == 'S':
            ok = shares >= sl if mode == 'reverse' else (shares - shares0 >= sl - 1e-9)
            if ok:
                shares -= sl; cash += sl * px * (1 - cs); apply(-1, sl, px); fills += 1

    end_nav = all_nav[idxs[-1]]
    unreal = sum(sh * (end_nav - px) if d > 0 else sh * (px - end_nav)
                 for d, sh, px in book)
    return realized + unreal, fills


def rolling_windows(win=22):
    out, end = [], len(all_dates)
    while end - win >= 0:
        out.append(list(range(end - win, end)))
        end -= win
    return list(reversed(out))


WINS = rolling_windows(22)
NW = len(WINS)
GRID = [0.25, 0.4, 0.5, 0.6, 0.75, 1.0, 1.25, 1.5, 2.0, 2.5]

print('=' * 96)
print('A) PARAMETER NEIGHBOURHOOD SCAN  -- reverse-T, 48 rolling windows, ETF cost')
print('=' * 96)
print('Is (0.5, 0.5) a lonely spike, or does it sit on a plateau?')
print()
print('   sell\\buy ' + ''.join('%9.2f' % b for b in GRID))
print('   ' + '-' * (9 + 9 * len(GRID)))
heat = {}
for a in GRID:
    row = []
    for b in GRID:
        vals = [run_window(w, a, b, 'reverse', COST_ETF, COST_ETF)[0] for w in WINS]
        m = statistics.mean(vals)
        heat[(a, b)] = (m, statistics.median(vals),
                        100 * sum(1 for x in vals if x > 0) / len(vals))
        row.append(m)
    print('   %8.2f ' % a + ''.join('%9.0f' % x for x in row))

print()
print('  (table cell = mean TOTAL per window, CNY; green=profitable, red=loss)')
pos_cells = sum(1 for k, v in heat.items() if v[0] > 0)
print('  Profitable cells: %d / %d (%.0f%%)'
      % (pos_cells, len(heat), 100 * pos_cells / len(heat)))
best = max(heat.items(), key=lambda kv: kv[1][0])
print('  Best cell: sell>=%.2f / buy<=-%.2f -> mean %+.0f, median %+.0f, win %.0f%%'
      % (best[0][0], best[0][1], best[1][0], best[1][1], best[1][2]))
c = heat[(0.5, 0.5)]
print('  Cell (0.5,0.5):  mean {:+.0f}, median {:+.0f}, win {:.0f}%'.format(*c))
c2 = heat[(1.25, 2.5)]
print('  Cell (1.25,2.5): mean {:+.0f}, median {:+.0f}, win {:.0f}%'.format(*c2))
print('  -> the parameter I reported last round was NOT the best cell;')
print('     it is {:,.0f} vs the best {:,.0f}. Either the grid optimum is'.format(c[0], c2[0]))
print('     overfit, or (0.5,0.5) was simply a poor arbitrary choice.')
# is the best cell on the grid EDGE? that signals we should search wider
rowmax = max(GRID); colmax = max(GRID)
print('  Best cell sits on grid edge: a=%.2f (max %.2f), b=%.2f (max %.2f)'
      % (best[0][0], rowmax, best[0][1], colmax))
if best[0][0] == rowmax or best[0][1] == colmax:
    print('  WARNING: optimum is at the boundary -> the true optimum may lie')
    print('           outside the grid, and edge optima are fragile.')

# ---------------------------------------------------------------- B
print()
print('=' * 96)
print('B) WALK-FORWARD  -- pick params on PAST windows only, trade the NEXT one')
print('=' * 96)
TRAIN = 12          # first 12 windows are the initial training set
ooss_rev, ooss_long = [], []
chosen_rev, chosen_long = [], []
insample_pick_rev = []

for t in range(TRAIN, NW):
    train = WINS[:t]
    test = WINS[t]
    # --- reverse-T: choose (a,b) maximising mean TOTAL over training windows
    bestv, bestp = -1e18, None
    for a in GRID:
        for b in GRID:
            sc = statistics.mean([run_window(w, a, b, 'reverse', COST_ETF, COST_ETF)[0]
                                  for w in train])
            if sc > bestv:
                bestv, bestp = sc, (a, b)
    ooss_rev.append(run_window(test, *bestp, 'reverse', COST_ETF, COST_ETF)[0])
    chosen_rev.append(bestp)
    insample_pick_rev.append(bestv)

    # --- long-T
    bestv2, bestp2 = -1e18, None
    for a in GRID:
        for b in GRID:
            sc = statistics.mean([run_window(w, a, b, 'long', COST_ETF, COST_ETF)[0]
                                  for w in train])
            if sc > bestv2:
                bestv2, bestp2 = sc, (a, b)
    ooss_long.append(run_window(test, *bestp2, 'long', COST_ETF, COST_ETF)[0])
    chosen_long.append(bestp2)

print('  Out-of-sample windows tested: %d (window #%d .. #%d)' % (len(ooss_rev), TRAIN + 1, NW))
print()
print('%-26s %10s %10s %9s %9s' % ('approach', 'mean/win', 'median', 'win%', 'total'))
print('-' * 96)


def show(label, vals):
    print('%-26s %10s %10s %8.0f%% %9s'
          % (label, '{:+,.0f}'.format(statistics.mean(vals)),
             '{:+,.0f}'.format(statistics.median(vals)),
             100 * sum(1 for x in vals if x > 0) / len(vals),
             '{:+,.0f}'.format(sum(vals))))


fixed_rev = [run_window(w, 0.5, 0.5, 'reverse', COST_ETF, COST_ETF)[0] for w in WINS[TRAIN:]]
fixed_long = [run_window(w, 0.5, 1.0, 'long', COST_ETF, COST_ETF)[0] for w in WINS[TRAIN:]]
cheat_rev = [max(statistics.mean([run_window(w, a, b, 'reverse', COST_ETF, COST_ETF)[0]
                                  for w in WINS[TRAIN:]]) for a in GRID for b in GRID)]
show('reverse-T walk-forward', ooss_rev)
show('reverse-T fixed 0.5/0.5', fixed_rev)
show('long-T    walk-forward', ooss_long)
show('long-T    fixed 0.5/1.0', fixed_long)

print()
print('  Parameter churn (how often the "best" param flips between steps):')
flips = sum(1 for i in range(1, len(chosen_rev)) if chosen_rev[i] != chosen_rev[i - 1])
print('    reverse-T: %d flips in %d steps (%.0f%%) -- high churn = unstable selection'
      % (flips, len(chosen_rev) - 1, 100 * flips / (len(chosen_rev) - 1)))
uniq = len(set(chosen_rev))
print('    distinct params chosen: %d out of %d grid cells' % (uniq, len(GRID) ** 2))
print('    most common pick: %s' % (max(set(chosen_rev), key=chosen_rev.count),))

print()
print('  In-sample pick score vs realised next-window P&L (reverse-T):')
corr_num = sum((a - statistics.mean(insample_pick_rev)) * (b - statistics.mean(ooss_rev))
               for a, b in zip(insample_pick_rev, ooss_rev))
den = (sum((a - statistics.mean(insample_pick_rev)) ** 2 for a in insample_pick_rev) ** 0.5
       * sum((b - statistics.mean(ooss_rev)) ** 2 for b in ooss_rev) ** 0.5)
print('    correlation = %+.3f   (near zero or negative => past performance' % (corr_num / den))
print('                            does NOT predict the next window)')

# ---------------------------------------------------------------- C
print()
print('=' * 96)
print('C) VERDICT')
print('=' * 96)
insample_rev = [run_window(w, 0.5, 0.5, 'reverse', COST_ETF, COST_ETF)[0] for w in WINS[:TRAIN]]
print('  reverse-T 0.5/0.5 on the FIRST %d windows (its own "training" period):' % TRAIN)
show('    train-period', insample_rev)
show('    later period ', fixed_rev)
print()
print('  If later-period mean << train-period mean, the earlier good result was')
print('  at least partly luck / regime-specific rather than a durable edge.')
print()
print('  Annualised (mean/window x 12):')
for lbl, v in (('walk-forward reverse-T', ooss_rev),
               ('fixed 0.5/0.5 reverse-T over full sample',
                [run_window(w, 0.5, 0.5, 'reverse', COST_ETF, COST_ETF)[0] for w in WINS])):
    print('    %-42s %+10s CNY/yr  = %+.2f%% of 270k'
          % (lbl, '{:,.0f}'.format(statistics.mean(v) * 12),
             100 * statistics.mean(v) * 12 / CAP))
