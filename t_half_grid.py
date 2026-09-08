# -*- coding: utf-8 -*-
"""
User's new strategy:
  600k total. Build 50% (300k) at the bottom (2026-06-25). Keep 300k cash.
  Then T locally: sell 50-100k into strength, buy 50-100k dips.
  Execution ~14:50 (day's move already decided) -> on-exchange ETF can fill
  the SAME day. OTC would need next-day fill.

Model A (user):  half base + dip-buy pool + sell-into-strength
  down day >= -1%  -> buy 50k (100k if dip >= 2%) from cash, same/next day
  up day   >= +1%  -> sell 50k (100k if rally >= 2%) from position,
                      position floor 300k (never sell below the base)
Baselines:
  B: 600k all-in at day 0, hold.
  C: 300k all-in + 300k idle cash.

Modes:
  SAME-DAY fill (ETF at 14:50, no lag)
  NEXT-DAY fill  (OTC-style, no look-ahead)
"""
import sqlite3
import statistics

DB = 'data/fund_history.db'
con = sqlite3.connect(DB)
rows = con.execute(
    "SELECT trade_date,dwjz FROM nav_history WHERE fund_code='014414' "
    "AND dwjz IS NOT NULL ORDER BY trade_date").fetchall()
dates = [r[0] for r in rows]
nav = [r[1] for r in rows]
n = len(nav)

FEE = 0.0002          # ETF commission per side
CAP = 600000.0
BASE = 300000.0
S1, S2 = 50000.0, 100000.0


def fmt(x):
    return '{:+,.0f}'.format(x)


def run(i0, i1, lag=True, log=False):
    """lag=True -> act next day on today's signal. lag=False -> act same day close."""
    start_px = nav[i0]
    shares = BASE / start_px                 # 50% base
    cash = CAP - BASE
    base_shares = shares                     # floor marker
    fills = []
    realized = 0.0                           # closed swing P&L (FIFO on T lots)
    t_lots = []                              # shares bought as T, avg cost
    for i in range(i0 + 1, i1 + 1):
        sig_i = i if not lag else i - 1
        if sig_i <= i0:
            continue
        r = (nav[sig_i] / nav[sig_i - 1] - 1) * 100
        px = nav[i]
        if r <= -1.0:                        # dip -> buy from cash
            amt = S2 if r <= -2.0 else S1
            amt = min(amt, cash)
            if amt > 0:
                sh = amt * (1 - FEE) / px
                cash -= amt
                shares += sh
                t_lots.append([sh, amt / sh])   # avg cost incl fee
                fills.append((dates[i], 'BUY', px, amt))
        elif r >= 1.0:                       # rally -> sell from position
            amt = S2 if r >= 2.0 else S1
            sh_need = amt / px
            pos_val = shares * px
            if pos_val - sh_need * px >= BASE * 0.9:   # soft floor at 90% of base
                sh_need = min(sh_need, shares - base_shares * 0.9)
                if sh_need > 0:
                    proceeds = sh_need * px * (1 - FEE)
                    cash += proceeds
                    shares -= sh_need
                    # FIFO match against T lots first, then base (base sale = no P&L pairing)
                    need = sh_need
                    while need > 1e-9 and t_lots:
                        lsh, lpx = t_lots[0]
                        take = min(lsh, need)
                        realized += take * (px * (1 - FEE) - lpx)
                        t_lots[0][0] -= take
                        need -= take
                        if t_lots[0][0] <= 1e-9:
                            t_lots.pop(0)
                    fills.append((dates[i], 'SELL', px, sh_need * px))
    end_val = shares * nav[i1] + cash
    return dict(end=end_val, realized=realized, cash=cash,
                shares=shares, fills=fills, start_px=start_px)


def report(label, i0, i1):
    seg_nav = (nav[i1] / nav[i0] - 1) * 100
    print('=' * 96)
    print('%s   nav %.4f -> %.4f (%+.1f%%), %d days'
          % (label, nav[i0], nav[i1], seg_nav, i1 - i0))
    print('=' * 96)
    b_all = CAP * (1 - FEE) / nav[i0] * nav[i1]          # baseline B
    b_half = BASE * (1 - FEE) / nav[i0] * nav[i1] + (CAP - BASE)
    print('%-30s %11s %11s %9s %9s'
          % ('strategy', 'end value', 'vs all-in', 'fills', 'realized'))
    print('-' * 96)
    print('%-30s %11s %11s %9s %9s'
          % ('B 600k all-in, hold', '{:,.0f}'.format(b_all), fmt(0), '-', '-'))
    print('%-30s %11s %11s %9s %9s'
          % ('C 300k + 300k idle', '{:,.0f}'.format(b_half), fmt(b_half - b_all), '-', '-'))
    for mode, lag in (('A lag=0 (ETF@14:50)', False), ('A lag=1 (OTC next day)', True)):
        r = run(i0, i1, lag=lag)
        nb = sum(1 for f in r['fills'] if f[1] == 'BUY')
        ns = sum(1 for f in r['fills'] if f[1] == 'SELL')
        print('%-30s %11s %11s %9s %9s'
              % (mode, '{:,.0f}'.format(r['end']), fmt(r['end'] - b_all),
                 '%dB/%dS' % (nb, ns), fmt(r['realized'])))
    print()
    return b_all


# --- windows ---
segs = []
cur = None
for i in range(n):
    if nav[i] <= 0.85:
        cur = [i, i] if cur is None else [cur[0], i]
    else:
        if cur and cur[1] - cur[0] >= 25:
            segs.append(tuple(cur))
        cur = None
if cur and cur[1] - cur[0] >= 25:
    segs.append(tuple(cur))

report('LIVE CASE: 2026-06-25 bottom -> now', dates.index('2026-06-25'), n - 1)

print()
print('### Historical bottom-regime replays (same rules) ###')
print()
for a, b in segs:
    if dates[b] >= '2026-06-01':
        continue
    report('%s ~ %s' % (dates[a], dates[b]), a, b)

# --- detail on live case ---
print()
print('### LIVE CASE DETAIL (lag=0, ETF fills) ###')
r = run(dates.index('2026-06-25'), n - 1, lag=False, log=True)
for d, side, px, amt in r['fills']:
    print('  %s  %-4s @ %.4f  %9.0f CNY' % (d, side, px, amt))
print('  end: cash %s, realized swing P&L %s'
      % ('{:,.0f}'.format(r['cash']), fmt(r['realized'])))
print()
print('### cost basis check: blended avg cost vs buy&hold ###')
i0 = dates.index('2026-06-25')
bh_cost = nav[i0]
strat_cost = BASE / (BASE / nav[i0])
print('  buy&hold cost: %.4f' % bh_cost)
print('  NOTE: blended strategy cost = weighted avg of ALL buys (base + T-buys);')
print('        sells at highs REMOVE high-cost shares, which is how cost drops')
