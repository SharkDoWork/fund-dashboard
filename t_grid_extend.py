# -*- coding: utf-8 -*-
"""
Two follow-ups that the walk-forward run left open:

1. The grid optimum sat on the boundary (b = 2.5). Extend the grid outward to
   check whether "wait for a deeper pullback before buying back" keeps improving
   or whether it was an edge artefact.

2. Walk-forward showed cherry-picking params UNDERPERFORMS a fixed parameter.
   Confirm this on a like-for-like basis and find which fixed parameter is
   most robust across two disjoint halves of the sample.
"""
import sqlite3
import statistics

DB = 'data/fund_history.db'
CODE = '014414'
CAP = 270000.0
LOT = 8
COST_ETF = 0.0002

con = sqlite3.connect(DB)
rows = con.execute(
    'SELECT trade_date, dwjz FROM nav_history WHERE fund_code=? AND dwjz IS NOT NULL '
    'ORDER BY trade_date', (CODE,)).fetchall()
con.close()
all_dates = [r[0] for r in rows]
all_nav = [r[1] for r in rows]
gret = {i: (all_nav[i] / all_nav[i - 1] - 1) * 100 for i in range(1, len(all_nav))}


def run_window(idxs, a, b, cb=COST_ETF, cs=COST_ETF):
    nav0 = all_nav[idxs[0]]
    shares0 = CAP / nav0
    sl = shares0 / LOT
    cash, shares = 0.0, shares0
    baseline = shares0 * all_nav[idxs[-1]]
    book = []
    realized = 0.0

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
        sig = 'S' if r >= a else ('B' if r <= -b else None)
        if sig == 'B':
            cost = sl * px * (1 + cb)
            if cash >= cost:
                shares += sl; cash -= cost; apply(+1, sl, px)
        elif sig == 'S':
            if shares >= sl:
                shares -= sl; cash += sl * px * (1 - cs); apply(-1, sl, px)
    end_nav = all_nav[idxs[-1]]
    unreal = sum(sh * (end_nav - px) if d > 0 else sh * (px - end_nav)
                 for d, sh, px in book)
    return realized + unreal


def rolling_windows(win=22):
    out, end = [], len(all_dates)
    while end - win >= 0:
        out.append(list(range(end - win, end)))
        end -= win
    return list(reversed(out))


WINS = rolling_windows(22)
NW = len(WINS)

print('=' * 92)
print('1) EXTENDED GRID (reverse-T) -- does a deeper buy-back threshold keep helping?')
print('=' * 92)
BIG = [1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 5.0, 6.0]
A = [0.5, 1.0, 1.25, 1.5, 2.0, 2.5, 3.0]
print('   sell\\buy ' + ''.join('%9.1f' % b for b in BIG))
print('   ' + '-' * (9 + 9 * len(BIG)))
store = {}
for a in A:
    row = []
    for b in BIG:
        v = statistics.mean([run_window(w, a, b) for w in WINS])
        store[(a, b)] = v
        row.append(v)
    print('   %8.2f ' % a + ''.join('%9.0f' % x for x in row))
bk, bv = max(store.items(), key=lambda kv: kv[1])
print()
print('  Best: sell>=%.2f / buy<=-%.1f  mean %+.0f' % (bk[0], bk[1], bv))
print('  On boundary again? b=%.1f is the max tested (%.1f)' % (bk[1], max(BIG)))
print()
print('  Marginal effect of raising the buy-back threshold (sell fixed at 1.5):')
for b in BIG:
    print('    buy back after a drop of >=%4.1f%%  ->  mean %+8.0f / window,  win %3.0f%%'
          % (b, store.get((1.5, b), float('nan')),
             100 * sum(1 for w in WINS if run_window(w, 1.5, b) > 0) / NW))

print()
print('=' * 92)
print('2) FIXED PARAMETER ROBUSTNESS -- two disjoint halves, like-for-like')
print('=' * 92)
H1, H2 = WINS[:NW // 2], WINS[NW // 2:]
print('  Sample split: H1 = windows 1-%d (%s..%s)   H2 = windows %d-%d (%s..%s)'
      % (len(H1), all_dates[H1[0][0]], all_dates[H1[-1][-1]],
         len(H1) + 1, NW, all_dates[H2[0][0]], all_dates[H2[-1][-1]]))
print()
print('%-16s %11s %11s %11s %9s' % ('param (a,b)', 'H1 mean', 'H2 mean', 'full mean', 'both>0?'))
print('-' * 92)
cands = [(0.5, 0.5), (0.5, 1.0), (1.0, 1.0), (1.25, 1.5), (1.25, 2.5),
         (1.5, 1.5), (1.5, 2.0), (1.5, 2.5), (1.5, 3.0), (2.0, 2.0),
         (2.0, 2.5), (2.0, 3.0), (2.5, 3.5)]
rowsout = []
for a, b in cands:
    h1 = statistics.mean([run_window(w, a, b) for w in H1])
    h2 = statistics.mean([run_window(w, a, b) for w in H2])
    fu = statistics.mean([run_window(w, a, b) for w in WINS])
    # tuple layout: 0=a 1=b 2=H1 3=H2 4=full
    rowsout.append((a, b, h1, h2, fu))
    print('%-16s %11s %11s %11s %9s'
          % ('(%.2f, %.1f)' % (a, b), '{:+,.0f}'.format(h1), '{:+,.0f}'.format(h2),
             '{:+,.0f}'.format(fu), 'YES' if (h1 > 0 and h2 > 0) else 'no'))

both = [r for r in rowsout if r[2] > 0 and r[3] > 0]      # H1>0 AND H2>0
print()
print('  %d of %d candidate parameters are positive in BOTH halves.' % (len(both), len(rowsout)))
if both:
    print('  Those are the only ones worth trusting:')
    for a, b, h1, h2, fu in sorted(both, key=lambda x: -min(x[2], x[3])):
        print('    (%.2f, %.1f)  H1 %+9s  H2 %+9s  full %+9s  worst-half %+9s'
              % (a, b, '{:,.0f}'.format(h1), '{:,.0f}'.format(h2),
                 '{:,.0f}'.format(fu), '{:,.0f}'.format(min(h1, h2))))
    wb = max(both, key=lambda x: min(x[2], x[3]))
    worst = min(wb[2], wb[3])
    print()
    print('  Most robust (maximises the WORST half): sell>=%.2f / buy<=-%.1f'
          % (wb[0], wb[1]))
    print('    H1 %+.0f   H2 %+.0f   worst %.0f/window' % (wb[2], wb[3], worst))
    print('    -> conservative annualised %.0f CNY (%.2f%% of 270k)'
          % (worst * 12, 100 * worst * 12 / CAP))
    print('    -> full-sample annualised  %.0f CNY (%.2f%% of 270k)'
          % (wb[4] * 12, 100 * wb[4] * 12 / CAP))
else:
    print('  NO parameter is positive in both halves -> the edge is not robust.')

print()
print('=' * 92)
print('3) HEADLINE: cherry-picked vs fixed, identical window range (13..48)')
print('=' * 92)
print('  From the walk-forward run:')
print('    reverse-T walk-forward (re-optimised each step) : +174/window, win 47%')
print('    reverse-T fixed (0.5, 0.5)  same 36 windows     : +483/window, win 69%')
print('  -> re-optimising DESTROYS ~64%% of the edge. Fixed parameter wins.')
