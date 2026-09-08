# -*- coding: utf-8 -*-
"""
FIFO-queue swing-T model for 014414 / 014415 / 516670.

User's hypothesis:
  A large legacy position (held > 7 days) means every SELL consumes the OLDEST
  shares first (FIFO), which are already past the 7-day penalty window.
  Therefore the 1.5% short-term redemption fee is structurally avoided,
  while the BUY/SELL pair only rides the CURRENT swing.

This script tests:
  1) Is the FIFO no-penalty claim actually true? Under what condition does it break?
  2) After subscription fee + cash-in-transit (T+2) drag, what is left?
  3) Restricted to the bottom regime (nav <= 0.85), 5w per fill, 1% trigger.
  4) Upper bound if every swing really did net +1% (the user's assumption).

No look-ahead: signal uses data up to i-1, fill at close of day i.
"""
import sqlite3
import statistics
from collections import deque

DB = 'data/fund_history.db'
CODE = '014414'
CAP = 270000.0          # legacy position value at window start
SLICE = 50000.0         # 5w per fill
MIN_HOLD = 7            # calendar days for zero redemption fee
BOTTOM = 0.85           # bottom-regime nav ceiling

con = sqlite3.connect(DB)
rows = con.execute(
    'SELECT trade_date,dwjz FROM nav_history WHERE fund_code=? '
    'AND dwjz IS NOT NULL ORDER BY trade_date', (CODE,)).fetchall()
dates = [r[0] for r in rows]
nav = [r[1] for r in rows]
n = len(nav)

import datetime
dobj = [datetime.date.fromisoformat(x) for x in dates]
ret = [None] + [(nav[i] / nav[i - 1] - 1) * 100 for i in range(1, n)]


def fmt(x):
    return '{:+,.0f}'.format(x)


# ----------------------------------------------------------------------------
# Part 1: analytical condition for FIFO never touching a <7-day lot
# ----------------------------------------------------------------------------
print('=' * 94)
print('PART 1 -- IS THE FIFO "NO PENALTY" CLAIM TRUE?')
print('=' * 94)
print('Legacy position %s CNY, %s per fill -> %.1f sells before the queue head'
      % ('{:,.0f}'.format(CAP), '{:,.0f}'.format(SLICE), CAP / SLICE))
print('becomes a lot bought during this campaign.')
print()
print('Condition: by the time the queue head is a campaign-bought lot, that lot')
print('must already be >= %d days old.' % MIN_HOLD)
print('  legacy_slices = %.1f' % (CAP / SLICE))
print('  => need  (CAP/SLICE) * avg_days_between_sells >= %d' % MIN_HOLD)
print('  => avg_days_between_sells >= %.2f days' % (MIN_HOLD / (CAP / SLICE)))
print()
print('  Danger case: sells CLUSTERED inside one week (a sharp rally).')
print('  The head lot stays legacy while cumulative sells < %.1f.' % (CAP / SLICE))
print('  Once cumulative sells reach %.1f, the head becomes the 1st buy-back lot,'
      % (CAP / SLICE))
print('  whose age is roughly the elapsed span of those sells.')
LEG = CAP / SLICE
for k in (2, 3, 4, 5, 6, 8):
    if k < LEG:
        verdict = 'SAFE (head still legacy)'
        age = None
    else:
        # k sells compressed into MIN_HOLD days -> the lot that becomes head
        # was bought after sell #1, so its age <= MIN_HOLD * (k - LEG) / k
        age = MIN_HOLD * (k - LEG) / k
        verdict = 'PENALTY 1.5%% (head age ~%.1fd < %dd)' % (age, MIN_HOLD)
    print('     %d sells inside %d days -> %s' % (k, MIN_HOLD, verdict))
print()
print('  => the rule survives a slow drift, breaks in a fast rally:')
print('     %d+ sells in one week drains the legacy pile and the 1.5%% fee bites.'
      % int(LEG + 1))
print('     One penalised 5w redemption costs %s CNY.' % '{:,.0f}'.format(SLICE * 0.015))


# ----------------------------------------------------------------------------
# Engine with FIFO lots + cash in transit
# ----------------------------------------------------------------------------
def run(i0, i1, trig_sell, trig_buy, slice_amt=SLICE, cap=CAP,
        buy_fee=0.0012, svc_annual=0.0, settle_days=2,
        bottom_only=True, min_hold=MIN_HOLD, force_hold=True,
        collect=False):
    """
    FIFO lot book. lots = deque([buy_index, shares]).
    Legacy lot is stamped far in the past so it is always penalty-free.
    settle_days: trading days until redemption cash is usable.
    force_hold: if True, refuse to sell a lot younger than min_hold
                (i.e. the user's rule "only ever redeem old money").
    """
    lots = deque()
    px0 = nav[i0]
    legacy_sh = cap / px0
    lots.append([-9999, legacy_sh])          # legacy: always old enough

    cash = 0.0
    pending = []                              # [(avail_index, amount)]
    realized = 0.0                            # swing P&L, fees included
    fees_buy = 0.0
    fees_sell = 0.0
    fees_svc = 0.0
    penalty_hits = 0
    sells = 0
    buys = 0
    blocked_no_cash = 0
    blocked_penalty = 0
    log = []
    open_short = deque()                      # sold lots awaiting buy-back

    for i in range(i0 + 1, i1 + 1):
        # release settled cash
        keep = []
        for av, amt in pending:
            if av <= i:
                cash += amt
            else:
                keep.append((av, amt))
        pending = keep

        # sales service fee (C class) accrues on held value daily
        if svc_annual:
            held_val = sum(sh for _, sh in lots) * nav[i]
            f = held_val * svc_annual / 250.0
            fees_svc += f
            realized -= f

        in_bottom = (nav[i - 1] <= BOTTOM) if bottom_only else True
        r = ret[i - 1]
        if r is None or not in_bottom:
            continue

        # ---- SELL signal: yesterday rose >= trig_sell
        if r >= trig_sell:
            sh_need = slice_amt / nav[i]
            avail = sum(sh for _, sh in lots)
            if avail >= sh_need:
                head_idx = lots[0][0]
                head_age = 9999 if head_idx < 0 else (dobj[i] - dobj[head_idx]).days
                if force_hold and head_age < min_hold:
                    blocked_penalty += 1
                else:
                    need = sh_need
                    gross = 0.0
                    fee = 0.0
                    while need > 1e-9 and lots:
                        lidx, lsh = lots[0]
                        take = min(lsh, need)
                        age = 9999 if lidx < 0 else (dobj[i] - dobj[lidx]).days
                        rate = 0.0 if age >= min_hold else 0.015
                        g = take * nav[i]
                        gross += g
                        fee += g * rate
                        if rate > 0:
                            penalty_hits += 1
                        lots[0][0] = lidx
                        lots[0][1] -= take
                        need -= take
                        if lots[0][1] <= 1e-9:
                            lots.popleft()
                    net = gross - fee
                    fees_sell += fee
                    pending.append((i + settle_days, net))
                    open_short.append([nav[i], sh_need])
                    realized -= fee
                    sells += 1
                    if collect:
                        log.append((dates[i], 'SELL', nav[i], sh_need, fee))

        # ---- BUY signal: yesterday fell >= trig_buy
        elif r <= -trig_buy:
            if not open_short:
                pass                          # nothing sold -> nothing to buy back
            else:
                cost = slice_amt
                if cash >= cost:
                    fee = cost * buy_fee
                    sh = (cost - fee) / nav[i]
                    lots.append([i, sh])
                    cash -= cost
                    fees_buy += fee
                    realized -= fee
                    # pair against the oldest open short
                    sold_px, sold_sh = open_short.popleft()
                    pair_sh = min(sold_sh, sh)
                    realized += pair_sh * (sold_px - nav[i])
                    if sold_sh > pair_sh:
                        open_short.appendleft([sold_px, sold_sh - pair_sh])
                    buys += 1
                    if collect:
                        log.append((dates[i], 'BUY', nav[i], sh, fee))
                else:
                    blocked_no_cash += 1

    end_sh = sum(sh for _, sh in lots)
    end_cash = cash + sum(a for _, a in pending)
    end_val = end_sh * nav[i1] + end_cash
    base_val = legacy_sh * nav[i1]
    # unrealized on still-open shorts = missed/gained exposure
    unreal = 0.0
    for sold_px, sold_sh in open_short:
        unreal += sold_sh * (sold_px - nav[i1])
    return dict(realized=realized, total=end_val - base_val, unreal=unreal,
                sells=sells, buys=buys, fees_buy=fees_buy, fees_sell=fees_sell,
                fees_svc=fees_svc, penalty=penalty_hits,
                blocked_cash=blocked_no_cash, blocked_pen=blocked_penalty,
                log=log, end_val=end_val, base_val=base_val)


# ----------------------------------------------------------------------------
# Part 2: bottom-regime windows
# ----------------------------------------------------------------------------
print()
print('=' * 94)
print('PART 2 -- BOTTOM-REGIME SEGMENTS (nav <= %.2f)' % BOTTOM)
print('=' * 94)
segs = []
cur = None
for i in range(n):
    if nav[i] <= BOTTOM:
        if cur is None:
            cur = [i, i]
        else:
            cur[1] = i
    else:
        if cur and cur[1] - cur[0] >= 25:
            segs.append(tuple(cur))
        cur = None
if cur and cur[1] - cur[0] >= 25:
    segs.append(tuple(cur))
print('%-26s %6s %9s %9s %9s' % ('segment', 'days', 'nav_lo', 'nav_hi', 'range%'))
print('-' * 94)
for a, b in segs:
    lo = min(nav[a:b + 1]); hi = max(nav[a:b + 1])
    print('%-26s %6d %9.4f %9.4f %8.1f%%'
          % ('%s~%s' % (dates[a], dates[b]), b - a + 1, lo, hi, 100 * (hi / lo - 1)))


# ----------------------------------------------------------------------------
# Part 3: the user's exact rule, per venue
# ----------------------------------------------------------------------------
print()
print('=' * 94)
print('PART 3 -- USER RULE: 5w/fill, 1%% trigger, only sell lots >= 7 days old')
print('=' * 94)
VENUES = [
    ('014414 A  (buy 0.12%, T+2 cash)', dict(buy_fee=0.0012, svc_annual=0.0,  settle_days=2)),
    ('014414 A  (buy 1.2% no discount)', dict(buy_fee=0.012,  svc_annual=0.0,  settle_days=2)),
    ('014415 C  (buy 0%, svc 0.4%/yr)', dict(buy_fee=0.0,    svc_annual=0.004, settle_days=2)),
    ('516670 ETF (0.02%, T+0 cash)',    dict(buy_fee=0.0002, svc_annual=0.0,  settle_days=0)),
]
print('  realized = closed swings incl. all fees')
print('  drag     = total - realized = cost of sells never bought back (missed rally)')
print()
print('%-34s %10s %10s %10s %6s %6s %7s'
      % ('venue', 'realized', 'drag', 'TOTAL', 'sell', 'buy', 'penal'))
print('-' * 94)
allseg = []
for label, kw in VENUES:
    tot_r = tot_t = tot_bf = 0.0
    tot_s = tot_b = tot_p = tot_bp = 0
    for a, b in segs:
        r = run(a, b, 1.0, 1.0, **kw)
        tot_r += r['realized']; tot_t += r['total']; tot_bf += r['fees_buy']
        tot_s += r['sells']; tot_b += r['buys']; tot_p += r['penalty']
        tot_bp += r['blocked_pen']
    print('%-34s %10s %10s %10s %6d %6d %7d'
          % (label, fmt(tot_r), fmt(tot_t - tot_r), fmt(tot_t), tot_s, tot_b, tot_p))
    allseg.append((label, tot_r, tot_t, tot_s, tot_b))
print()
print('  Every venue shows a large NEGATIVE drag: the sells that were never')
print('  bought back missed the bottom-regime rebound. Fee choice changes the')
print('  small "realized" number; it does not touch the big drag.')


# ----------------------------------------------------------------------------
# Part 4: cash-in-transit cost -- the hidden killer
# ----------------------------------------------------------------------------
print()
print('=' * 94)
print('PART 4 -- COST OF CASH IN TRANSIT (sell -> money usable)')
print('=' * 94)
print('%-14s %11s %11s %8s %8s' % ('settle', 'realized', 'total', 'buys', 'blocked'))
print('-' * 94)
for sd in (0, 1, 2, 3, 4):
    tr = tt = 0.0
    tb = bl = 0
    for a, b in segs:
        r = run(a, b, 1.0, 1.0, buy_fee=0.0012, settle_days=sd)
        tr += r['realized']; tt += r['total']; tb += r['buys']; bl += r['blocked_cash']
    print('%-14s %11s %11s %8d %8d' % ('T+%d' % sd, fmt(tr), fmt(tt), tb, bl))
print()
print('  Real-world: OTC ETF-feeder redemption is T+1 confirm, cash usable T+2~T+4.')
print('  On-exchange ETF is T+0 usable for re-buying (intraday round trip allowed).')


# ----------------------------------------------------------------------------
# Part 5: does "every swing nets +1%" actually happen?
# ----------------------------------------------------------------------------
print()
print('=' * 94)
print('PART 5 -- DO THE SWINGS REALLY NET +1%%? (paired sell->buyback outcomes)')
print('=' * 94)
pairs = []
for a, b in segs:
    r = run(a, b, 1.0, 1.0, buy_fee=0.0012, settle_days=2, collect=True)
    lg = r['log']
    stack = []
    for d, side, px, sh, fee in lg:
        if side == 'SELL':
            stack.append((d, px))
        elif side == 'BUY' and stack:
            d0, p0 = stack.pop(0)
            pairs.append((d0, p0, d, px, (p0 / px - 1) * 100))
if pairs:
    gains = [p[4] for p in pairs]
    print('closed swings: %d' % len(pairs))
    print('  win rate            %.1f%%' % (100 * sum(1 for g in gains if g > 0) / len(gains)))
    print('  >= +1.0%% gross      %.1f%%' % (100 * sum(1 for g in gains if g >= 1.0) / len(gains)))
    print('  mean %+.3f%%   median %+.3f%%   worst %+.2f%%   best %+.2f%%'
          % (statistics.mean(gains), statistics.median(gains), min(gains), max(gains)))
    print()
    print('  sample of closed swings (sell -> buyback):')
    for d0, p0, d1, p1, g in pairs[:12]:
        print('    %s @%.4f -> %s @%.4f   %+.2f%%' % (d0, p0, d1, p1, g))
    print()
    unclosed = 0
    for a, b in segs:
        r = run(a, b, 1.0, 1.0, buy_fee=0.0012, settle_days=2)
        unclosed += r['sells'] - r['buys']
    print('  UNCLOSED sells (sold, never bought back in segment): %d' % unclosed)
    print('  -> these are the ones that "never lost" only because they were')
    print('     never closed. That is the survivorship trap.')


# ----------------------------------------------------------------------------
# Part 6: theoretical ceiling under the user's assumption
# ----------------------------------------------------------------------------
print()
print('=' * 94)
print('PART 6 -- CEILING IF EVERY SWING TRULY NETTED +1%% ON 5w')
print('=' * 94)
print('  per swing gross: %s' % '{:,.0f}'.format(SLICE * 0.01))
for nsw in (10, 20, 30, 50):
    gross = nsw * SLICE * 0.01
    fee_a = nsw * SLICE * 0.0012
    fee_c = CAP * 0.004
    fee_e = nsw * SLICE * 0.0002 * 2
    print('  %2d swings/yr: gross %8s | A-class net %8s | C-class net %8s | ETF net %8s'
          % (nsw, '{:,.0f}'.format(gross), '{:,.0f}'.format(gross - fee_a),
             '{:,.0f}'.format(gross - fee_c), '{:,.0f}'.format(gross - fee_e)))
print()
print('  A vs C break-even: n * 50000 * 0.0012 = 270000 * 0.004')
print('  -> n = %.1f swings/yr (above this, C class is cheaper)' % (CAP * 0.004 / (SLICE * 0.0012)))


# ----------------------------------------------------------------------------
# Part 7: current bottom segment only
# ----------------------------------------------------------------------------
print()
print('=' * 94)
print('PART 7 -- CURRENT BOTTOM SEGMENT 2026-05-12 ~ 2026-09-03')
print('=' * 94)
i0 = dates.index('2026-05-12')
i1 = n - 1
print('nav %.4f -> %.4f  (%+.2f%%), %d trading days'
      % (nav[i0], nav[i1], 100 * (nav[i1] / nav[i0] - 1), i1 - i0 + 1))
base = CAP / nav[i0] * nav[i1] - CAP
print('buy & hold on %s: %s' % ('{:,.0f}'.format(CAP), fmt(base)))
print()
print('%-34s %11s %11s %7s %7s' % ('venue', 'realized', 'total', 'sells', 'buys'))
print('-' * 94)
for label, kw in VENUES:
    r = run(i0, i1, 1.0, 1.0, **kw)
    print('%-34s %11s %11s %7d %7d'
          % (label, fmt(r['realized']), fmt(r['total']), r['sells'], r['buys']))
print()
r = run(i0, i1, 1.0, 1.0, buy_fee=0.0012, settle_days=2, collect=True)
print('  fills (A class, T+2):')
for d, side, px, sh, fee in r['log']:
    print('    %s  %-4s @ %.4f  x %9s sh   fee %6s'
          % (d, side, px, '{:,.0f}'.format(sh), '{:,.0f}'.format(fee)))
print('  penalty hits: %d   blocked by no-cash: %d   blocked by 7d-rule: %d'
      % (r['penalty'], r['blocked_cash'], r['blocked_pen']))


# ----------------------------------------------------------------------------
# Part 8: trigger sensitivity in bottom regime
# ----------------------------------------------------------------------------
print()
print('=' * 94)
print('PART 8 -- TRIGGER SENSITIVITY (bottom segments, A class T+2)')
print('=' * 94)
print('%-16s %11s %11s %8s %8s' % ('sell/buy trig', 'realized', 'total', 'sells', 'buys'))
print('-' * 94)
grid = []
for ts in (0.5, 1.0, 1.5, 2.0, 2.5):
    for tb in (0.5, 1.0, 1.5, 2.0, 2.5):
        tr = tt = 0.0
        ss = bb = 0
        for a, b in segs:
            r = run(a, b, ts, tb, buy_fee=0.0012, settle_days=2)
            tr += r['realized']; tt += r['total']; ss += r['sells']; bb += r['buys']
        grid.append((ts, tb, tr, tt, ss, bb))
for ts, tb, tr, tt, ss, bb in sorted(grid, key=lambda x: -x[3])[:10]:
    print('%-16s %11s %11s %8d %8d'
          % ('%.1f / %.1f' % (ts, tb), fmt(tr), fmt(tt), ss, bb))
print()
pos = sum(1 for g in grid if g[3] > 0)
print('  cells with positive TOTAL: %d / %d (%.0f%%)' % (pos, len(grid), 100 * pos / len(grid)))
print('  median TOTAL across grid: %s' % fmt(statistics.median([g[3] for g in grid])))
print('  cell (1.0,1.0) the user asked about: TOTAL %s'
      % fmt([g[3] for g in grid if g[0] == 1.0 and g[1] == 1.0][0]))
