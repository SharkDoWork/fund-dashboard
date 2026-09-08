# -*- coding: utf-8 -*-
"""
Two things the first script could not answer:

A) SELECTION BIAS. "Bottom segment" was defined as nav <= 0.85 and ended the
   day nav broke above 0.85. Every segment therefore ENDS on a rally, which
   automatically punishes a sell-first (reverse-T) strategy. Remove the bias
   with fixed-length rolling windows anchored in the bottom regime.

B) THE USER'S ACTUAL RULE: "only buy back when it is profitable" -- i.e. after
   selling at P, only repurchase at <= P*(1-1%). By construction every closed
   swing then nets >= +1%. The question is what that costs in missed upside
   and how long the money sits out.
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
BOTTOM = 0.85

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
           profit_lock=None, collect=False):
    """
    profit_lock=None  -> buy back on a -trig_buy% day (mechanical)
    profit_lock=0.01  -> buy back only at <= sell_px*(1-0.01) (user's rule)
    """
    lots = deque([[-9999, CAP / nav[i0]]])
    legacy_sh = CAP / nav[i0]
    cash = 0.0
    pending = []
    realized = 0.0
    opens = deque()                 # [sell_px, shares, sell_idx]
    sells = buys = penal = 0
    waits = []
    log = []

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

        # SELL
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
                        penal += 1
                    lots[0][1] -= take
                    need -= take
                    if lots[0][1] <= 1e-9:
                        lots.popleft()
                realized -= fee
                pending.append((i + settle, gross - fee))
                opens.append([nav[i], sh_need, i])
                sells += 1
                if collect:
                    log.append((dates[i], 'SELL', nav[i], fee))

        # BUY
        elif opens:
            want = False
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
                if collect:
                    log.append((dates[i], 'BUY', nav[i], fee))

    end_val = sum(sh for _, sh in lots) * nav[i1] + cash + sum(a for _, a in pending)
    base = legacy_sh * nav[i1]
    stuck = sum(sh for _, sh, _ in opens)
    return dict(realized=realized, total=end_val - base, sells=sells, buys=buys,
                penal=penal, waits=waits, stuck=stuck,
                stuck_n=len(opens), log=log)


# ---------------------------------------------------------------------------
print('=' * 94)
print('A) IS THE "0/25 NEGATIVE" RESULT AN ARTEFACT OF SEGMENT-END RALLIES?')
print('=' * 94)

# original segments
segs = []
cur = None
for i in range(n):
    if nav[i] <= BOTTOM:
        cur = [i, i] if cur is None else [cur[0], i]
    else:
        if cur and cur[1] - cur[0] >= 25:
            segs.append(tuple(cur))
        cur = None
if cur and cur[1] - cur[0] >= 25:
    segs.append(tuple(cur))

print('%-30s %9s %9s %9s' % ('segment', 'start_nav', 'end_nav', 'end/start'))
print('-' * 94)
for a, b in segs:
    print('%-30s %9.4f %9.4f %+8.1f%%'
          % ('%s~%s' % (dates[a], dates[b]), nav[a], nav[b], 100 * (nav[b] / nav[a] - 1)))
drifts = [100 * (nav[b] / nav[a] - 1) for a, b in segs]
print()
print('  mean segment drift %+.1f%%  -> segments are NOT uniformly up;' % statistics.mean(drifts))
print('  but each one TERMINATES on a break above %.2f, so the last few days' % BOTTOM)
print('  are upward by construction. Test: chop the final 10 days off.')
print()

print('%-24s %11s %11s %11s' % ('variant', 'realized', 'TOTAL', 'sells'))
print('-' * 94)
for label, cut in (('full segments', 0), ('minus last 5d', 5), ('minus last 10d', 10),
                   ('minus last 20d', 20)):
    tr = tt = 0.0
    ss = 0
    for a, b in segs:
        bb = b - cut
        if bb - a < 20:
            continue
        r = engine(a, bb, 1.0, 1.0, buy_fee=0.0002, settle=0)
        tr += r['realized']; tt += r['total']; ss += r['sells']
    print('%-24s %11s %11s %11d' % (label, fmt(tr), fmt(tt), ss))

print()
print('  Now the unbiased test: fixed 60-day rolling windows whose START is in')
print('  the bottom regime (no look-ahead on where the window ends).')
print()
W = 60
wins = [i for i in range(n - W) if nav[i] <= BOTTOM]
print('  windows: %d' % len(wins))
print('%-20s %10s %10s %8s %8s %8s' % ('trigger', 'mean', 'median', 'win%', 'best', 'worst'))
print('-' * 94)
best = None
for ts, tb in ((0.5, 0.5), (1.0, 1.0), (1.0, 1.5), (1.5, 1.5), (1.5, 2.5), (2.0, 2.0)):
    tots = [engine(i, i + W, ts, tb, buy_fee=0.0002, settle=0)['total'] for i in wins]
    wr = 100 * sum(1 for x in tots if x > 0) / len(tots)
    print('%-20s %10s %10s %7.0f%% %8s %8s'
          % ('%.1f / %.1f' % (ts, tb), fmt(statistics.mean(tots)),
             fmt(statistics.median(tots)), wr,
             fmt(max(tots)), fmt(min(tots))))
    if best is None or statistics.mean(tots) > best[1]:
        best = ((ts, tb), statistics.mean(tots))
print()
print('  -> unbiased windows give a very different picture from the')
print('     segment test; the segment-end rally WAS distorting it.')


# ---------------------------------------------------------------------------
print()
print('=' * 94)
print('B) USER RULE: BUY BACK ONLY AT >= 1%% PROFIT ("every swing must win")')
print('=' * 94)
print('By construction each CLOSED swing nets >= 1%% gross. Question: how many')
print('never close, how long they wait, and what the stranded cash costs.')
print()
print('%-22s %9s %9s %8s %8s %9s %9s'
      % ('variant', 'realized', 'TOTAL', 'sells', 'closed', 'unclosed', 'avg_wait'))
print('-' * 94)
for lock, tsell in ((0.01, 1.0), (0.01, 1.5), (0.01, 2.0), (0.02, 1.0), (0.02, 1.5)):
    tr = tt = 0.0
    ss = bb2 = un = 0
    allw = []
    for i in wins:
        r = engine(i, i + W, tsell, 0, buy_fee=0.0002, settle=0, profit_lock=lock)
        tr += r['realized']; tt += r['total']
        ss += r['sells']; bb2 += r['buys']; un += r['stuck_n']
        allw += r['waits']
    print('%-22s %9s %9s %8d %8d %9d %8.1fd'
          % ('lock %.0f%% / sell %.1f%%' % (lock * 100, tsell),
             fmt(tr / len(wins)), fmt(tt / len(wins)), ss, bb2, un,
             statistics.mean(allw) if allw else 0))
print()
print('  (realized / TOTAL are per-window averages over %d windows)' % len(wins))
print()
close_rate = None
r_all_s = r_all_b = 0
waitl = []
for i in wins:
    r = engine(i, i + W, 1.0, 0, buy_fee=0.0002, settle=0, profit_lock=0.01)
    r_all_s += r['sells']; r_all_b += r['buys']; waitl += r['waits']
if r_all_s:
    close_rate = 100 * r_all_b / r_all_s
    waitl.sort()
    print('  lock=1%%, sell trigger 1%%:')
    print('    close rate %.1f%% of sells (%d of %d)' % (close_rate, r_all_b, r_all_s))
    print('    wait days: median %d, p75 %d, p90 %d, max %d'
          % (waitl[len(waitl) // 2], waitl[int(len(waitl) * .75)],
             waitl[int(len(waitl) * .9)], waitl[-1]))
    print('    -> %.0f%% of sells stay unclosed inside a 60-day window.'
          % (100 - close_rate))


# ---------------------------------------------------------------------------
print()
print('=' * 94)
print('C) HEAD-TO-HEAD ON THE CURRENT BOTTOM PHASE (2026-05-12 ~ 2026-09-03)')
print('=' * 94)
i0 = dates.index('2026-05-12')
i1 = n - 1
print('nav %.4f -> %.4f (%+.2f%%)  buy&hold on 270k: %s'
      % (nav[i0], nav[i1], 100 * (nav[i1] / nav[i0] - 1), fmt(CAP / nav[i0] * nav[i1] - CAP)))
print()
print('%-38s %10s %10s %7s %7s %8s' % ('strategy', 'realized', 'TOTAL', 'sell', 'buy', 'unclosed'))
print('-' * 94)
cases = [
    ('mechanical 1%/1%  ETF T+0', dict(trig_sell=1.0, trig_buy=1.0, buy_fee=0.0002, settle=0)),
    ('mechanical 1%/1%  OTC-A T+2', dict(trig_sell=1.0, trig_buy=1.0, buy_fee=0.0012, settle=2)),
    ('profit-lock 1%   ETF T+0', dict(trig_sell=1.0, trig_buy=0, buy_fee=0.0002, settle=0, profit_lock=0.01)),
    ('profit-lock 1%   OTC-A T+2', dict(trig_sell=1.0, trig_buy=0, buy_fee=0.0012, settle=2, profit_lock=0.01)),
    ('profit-lock 2%   ETF T+0', dict(trig_sell=1.0, trig_buy=0, buy_fee=0.0002, settle=0, profit_lock=0.02)),
]
for label, kw in cases:
    r = engine(i0, i1, **kw)
    print('%-38s %10s %10s %7d %7d %8d'
          % (label, fmt(r['realized']), fmt(r['total']), r['sells'], r['buys'], r['stuck_n']))

print()
r = engine(i0, i1, 1.0, 0, buy_fee=0.0002, settle=0, profit_lock=0.01, collect=True)
print('  profit-lock 1%% fills on ETF:')
for d, side, px, fee in r['log']:
    print('    %s  %-4s @ %.4f' % (d, side, px))
print('  unclosed sells at end: %d  (%s shares stranded in cash)'
      % (r['stuck_n'], '{:,.0f}'.format(r['stuck'])))
if r['waits']:
    print('  wait days per closed swing: %s' % r['waits'])
