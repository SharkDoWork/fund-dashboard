# -*- coding: utf-8 -*-
"""
Stress-test the robust parameter (sell>=1.5 / buy<=-2.5) against the one I
recommended last round (0.5 / 0.5):

  1. Same last month (2026-08-05 ~ 2026-09-03) -- what the user actually asked about
  2. The 2022 uptrend killer window
  3. Year-by-year
  4. Fill frequency (is it even tradeable?)
"""
import sqlite3
import statistics

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


def run_window(idxs, a, b, cb=COST_ETF, cs=COST_ETF):
    nav0 = all_nav[idxs[0]]
    shares0 = CAP / nav0
    sl = shares0 / LOT
    cash, shares = 0.0, shares0
    baseline = shares0 * all_nav[idxs[-1]]
    book = []
    realized = 0.0
    fills = []

    def apply(d, sh, px, day):
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
        fills.append((day, 'BUY' if d > 0 else 'SELL', px))

    for i in idxs:
        if i not in gret:
            continue
        r = gret[i]
        px = all_nav[i]
        sig = 'S' if r >= a else ('B' if r <= -b else None)
        if sig == 'B':
            cost = sl * px * (1 + cb)
            if cash >= cost:
                shares += sl; cash -= cost; apply(+1, sl, px, all_dates[i])
        elif sig == 'S':
            if shares >= sl:
                shares -= sl; cash += sl * px * (1 - cs); apply(-1, sl, px, all_dates[i])
    end_nav = all_nav[idxs[-1]]
    unreal = sum(sh * (end_nav - px) if d > 0 else sh * (px - end_nav)
                 for d, sh, px in book)
    return realized, unreal, realized + unreal, fills


def rolling_windows(win=22):
    out, end = [], len(all_dates)
    while end - win >= 0:
        out.append(list(range(end - win, end)))
        end -= win
    return list(reversed(out))


WINS = rolling_windows(22)
LAST = WINS[-1]
P_OLD = (0.5, 0.5)
P_NEW = (1.5, 2.5)

print('=' * 90)
print('1) THE LAST MONTH  2026-08-05 ~ 2026-09-03  (what the user asked about)')
print('=' * 90)
print('%-22s %11s %11s %11s %8s' % ('parameter', 'realized', 'open MTM', 'TOTAL', 'fills'))
print('-' * 90)
for lbl, (a, b) in (('old  0.5 / 0.5', P_OLD), ('new  1.5 / 2.5', P_NEW)):
    for venue, cb, cs in (('ETF', COST_ETF, COST_ETF), ('OTC-A', BUY_OTC, SELL_OTC)):
        r, u, t, f = run_window(LAST, a, b, cb, cs)
        print('%-22s %11s %11s %11s %8d'
              % ('%s %s' % (lbl, venue), '{:+,.0f}'.format(r), '{:+,.0f}'.format(u),
                 '{:+,.0f}'.format(t), len(f)))
print()
print('  Buy&hold over the same window: %s'
      % '{:+,.0f}'.format(CAP / all_nav[LAST[0]] * all_nav[LAST[-1]] - CAP))
print()
r, u, t, f = run_window(LAST, *P_NEW)
print('  New parameter fill list:')
for d, s, px in f:
    print('     %s  %-4s @ %.4f' % (d, s, px))
if not f:
    print('     (no fill triggered in this window)')

print()
print('=' * 90)
print('2) THE 2022 UPTREND KILLER  (worst window for the old parameter)')
print('=' * 90)
old_vals = [run_window(w, *P_OLD)[2] for w in WINS]
new_vals = [run_window(w, *P_NEW)[2] for w in WINS]
worst_i = old_vals.index(min(old_vals))
print('  Worst window for OLD param: %s ~ %s' % (all_dates[WINS[worst_i][0]], all_dates[WINS[worst_i][-1]]))
print('    NAV %.4f -> %.4f  (%+.1f%%)'
      % (all_nav[WINS[worst_i][0]], all_nav[WINS[worst_i][-1]],
         100 * (all_nav[WINS[worst_i][-1]] / all_nav[WINS[worst_i][0]] - 1)))
print('    old (0.5/0.5): %s' % '{:+,.0f}'.format(old_vals[worst_i]))
print('    new (1.5/2.5): %s   <- improvement %s'
      % ('{:+,.0f}'.format(new_vals[worst_i]),
         '{:+,.0f}'.format(new_vals[worst_i] - old_vals[worst_i])))

print()
print('  All windows where the OLD param lost money (%d of %d):'
      % (sum(1 for x in old_vals if x < 0), len(old_vals)))
print('  %-24s %11s %11s %11s' % ('window', 'NAV chg', 'old', 'new'))
print('  ' + '-' * 62)
for i, v in enumerate(old_vals):
    if v < -1000:
        nc = 100 * (all_nav[WINS[i][-1]] / all_nav[WINS[i][0]] - 1)
        print('  %-24s %+10.1f%% %11s %11s'
              % (all_dates[WINS[i][0]][:7] + '~' + all_dates[WINS[i][-1]][:7], nc,
                 '{:+,.0f}'.format(v), '{:+,.0f}'.format(new_vals[i])))

print()
print('=' * 90)
print('3) YEAR BY YEAR')
print('=' * 90)
by_year = {}
for w, ov, nv in zip(WINS, old_vals, new_vals):
    y = all_dates[w[-1]][:4]
    by_year.setdefault(y, []).append((ov, nv))
print('%-8s %14s %14s %8s' % ('year', 'old 0.5/0.5', 'new 1.5/2.5', 'windows'))
print('-' * 90)
for y in sorted(by_year):
    v = by_year[y]
    print('%-8s %14s %14s %8d'
          % (y, '{:+,.0f}'.format(sum(x[0] for x in v)),
             '{:+,.0f}'.format(sum(x[1] for x in v)), len(v)))

print()
print('=' * 90)
print('4) TRADEABILITY -- how often does it actually fire?')
print('=' * 90)
nf = [len(run_window(w, *P_NEW)[3]) for w in WINS]
of = [len(run_window(w, *P_OLD)[3]) for w in WINS]
print('  old 0.5/0.5: mean %.1f fills/window, min %d, max %d, windows with 0 fills: %d'
      % (statistics.mean(of), min(of), max(of), sum(1 for x in of if x == 0)))
print('  new 1.5/2.5: mean %.1f fills/window, min %d, max %d, windows with 0 fills: %d'
      % (statistics.mean(nf), min(nf), max(nf), sum(1 for x in nf if x == 0)))
print()
print('  Fewer fills = less cost and less effort, but also thinner statistics.')
print('  A parameter that fires only once a month is barely testable.')

print()
print('=' * 90)
print('5) SUMMARY')
print('=' * 90)
print('%-24s %12s %12s %10s' % ('', 'old 0.5/0.5', 'new 1.5/2.5', 'delta'))
print('-' * 90)
print('%-24s %12s %12s' % ('mean/window (48)', '{:+,.0f}'.format(statistics.mean(old_vals)),
                           '{:+,.0f}'.format(statistics.mean(new_vals))))
print('%-24s %12s %12s' % ('median/window', '{:+,.0f}'.format(statistics.median(old_vals)),
                           '{:+,.0f}'.format(statistics.median(new_vals))))
print('%-24s %11.0f%% %11.0f%%'
      % ('win rate', 100 * sum(1 for x in old_vals if x > 0) / len(old_vals),
         100 * sum(1 for x in new_vals if x > 0) / len(new_vals)))
print('%-24s %12s %12s' % ('worst window', '{:+,.0f}'.format(min(old_vals)),
                           '{:+,.0f}'.format(min(new_vals))))
print('%-24s %12s %12s' % ('annualised (mean x12)',
                           '{:+,.0f}'.format(statistics.mean(old_vals) * 12),
                           '{:+,.0f}'.format(statistics.mean(new_vals) * 12)))
print('%-24s %12s %12s' % ('as % of 270k',
                           '{:+.2f}%'.format(100 * statistics.mean(old_vals) * 12 / CAP),
                           '{:+.2f}%'.format(100 * statistics.mean(new_vals) * 12 / CAP)))
