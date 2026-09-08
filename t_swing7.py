# -*- coding: utf-8 -*-
"""
7-day swing T backtest, matching the user's exact spec:
  - slice size 50,000 CNY per entry / exit
  - trigger: move >= 1% (measured over a 7-trading-day window)
  - minimum holding 7 CALENDAR days  -> redemption fee drops from 1.50% to 0.00%
  - restricted to the pig-cycle BOTTOM OSCILLATION regime
No future function: decision on day i uses data up to i-1, fills at nav[i].
"""
import sqlite3
import datetime
import statistics

DB = 'data/fund_history.db'
CODE = '014414'
SLICE = 50000.0        # user spec: 5w per entry/exit
BASE = 270000.0        # existing core position
MIN_HOLD_CAL = 7       # calendar days, to reach 0% redemption fee

con = sqlite3.connect(DB)
rows = con.execute(
    'SELECT trade_date,dwjz FROM nav_history WHERE fund_code=? AND dwjz IS NOT NULL '
    'ORDER BY trade_date', (CODE,)).fetchall()
dates = [datetime.date.fromisoformat(r[0]) for r in rows]
nav = [float(r[1]) for r in rows]
n = len(nav)

# ---------------------------------------------------------------- cost models
# round-trip cost of ONE 5w slice held H calendar days.
# operating fees are embedded in nav; the A-class nav series is our price feed,
# so we only add the *differential* vs A class for other vehicles.
def cost_model(tool):
    """returns (buy_rate, sell_rate_ge7, extra_annual_drag)"""
    if tool == 'OTC-A':          # 014414, 1.2% list / 0.12% discounted online
        return 0.0012, 0.0, 0.0
    if tool == 'OTC-A-full':     # no discount channel
        return 0.0120, 0.0, 0.0
    if tool == 'OTC-C':          # 014415, 0% front load, +0.4%/yr sales service
        return 0.0000, 0.0, 0.0040
    if tool == 'ETF':            # 516670 on-exchange, commission both sides
        return 0.0002, 0.0002, -0.0030   # ETF saves 0.3%/yr operating fee
    raise ValueError(tool)


# ---------------------------------------------------------------- regimes
def segments(pred, min_days):
    out = []
    cur = None
    for i in range(n):
        if pred(i):
            if cur is None:
                cur = [i, i]
            else:
                cur[1] = i
        else:
            if cur:
                out.append(tuple(cur))
                cur = None
    if cur:
        out.append(tuple(cur))
    return [s for s in out if s[1] - s[0] + 1 >= min_days]


BOTTOM = segments(lambda i: nav[i] <= 0.85, 20)
DEEP = segments(lambda i: nav[i] <= 0.79, 15)
CUR_START = dates.index(datetime.date(2026, 5, 12))
CURRENT = [(CUR_START, n - 1)]
FULL = [(0, n - 1)]

# ---------------------------------------------------------------- engine
def ret7(i):
    """7-trading-day move ending at day i (only usable for decisions at i+1)"""
    if i - 7 < 0:
        return None
    return (nav[i] / nav[i - 7] - 1) * 100


def run_long_t(seg, trig, tool, max_slices=3, settle=1, min_hold=MIN_HOLD_CAL,
               time_stop=None):
    """
    Buy-the-dip swing T using spare CASH (does NOT touch the core position).
      entry : 7d move <= -trig   -> buy one 5w slice
      exit  : lot held >= min_hold calendar days AND nav >= cost*(1+trig) -> sell
      time_stop: if set, force-exit a lot after this many calendar days at any price
    """
    a, b = seg
    cb, cs, drag = cost_model(tool)
    lots = []          # [shares, cost_per_share_incl_fee, entry_date, entry_nav]
    trips = []         # (entry_date, exit_date, days, pnl, ret_pct)
    realized = 0.0
    blocked = 0        # entry signals skipped because all slices in use
    last_buy_i = -99

    for i in range(a + 1, b + 1):
        r = ret7(i - 1)
        if r is None:
            continue
        px = nav[i]
        today = dates[i]

        # --- exits first (a lot can exit and free a slice on the same day) ---
        keep = []
        for lot in lots:
            sh, cps, ed, enav = lot
            held = (today - ed).days
            eff = px * (1 - cs)
            hit = held >= min_hold and eff >= cps * (1 + trig / 100.0)
            forced = time_stop is not None and held >= time_stop and held >= min_hold
            if hit or forced:
                gross = sh * eff
                # operating-fee differential while the slice was held
                gross -= sh * enav * drag * held / 365.0
                pnl = gross - sh * cps
                realized += pnl
                trips.append((ed.isoformat(), today.isoformat(), held, pnl,
                              100 * pnl / (sh * cps), 'target' if hit else 'stop'))
            else:
                keep.append(lot)
        lots = keep

        # --- entry ---
        if r <= -trig and (i - last_buy_i) >= settle:
            if len(lots) < max_slices:
                cps = px * (1 + cb)
                sh = SLICE / cps
                lots.append([sh, cps, today, px])
                last_buy_i = i
            else:
                blocked += 1

    # mark open lots at segment end
    endpx = nav[b]
    open_pnl = 0.0
    open_detail = []
    for sh, cps, ed, enav in lots:
        held = (dates[b] - ed).days
        val = sh * endpx * (1 - cs) - sh * enav * drag * held / 365.0
        open_pnl += val - sh * cps
        open_detail.append((ed.isoformat(), held, 100 * (val - sh * cps) / (sh * cps)))
    return dict(trips=trips, realized=realized, open_pnl=open_pnl,
                open_detail=open_detail, blocked=blocked, n_open=len(lots))


def run_reverse_t(seg, trig, tool, max_slices=3, settle=2, min_hold=MIN_HOLD_CAL):
    """
    Sell-the-rally swing T against the CORE position.
      exit-first : 7d move >= +trig -> sell one 5w slice out of core (fee 0, held long)
      re-entry   : nav <= last_sell*(1-trig) and >= settle days after the sell
      a re-bought slice must be held >= min_hold before it can be sold again
    """
    a, b = seg
    cb, cs, drag = cost_model(tool)
    shorts = []        # [shares, sell_px_net, sell_date]
    rebought = []      # [shares, cost_per_share, buy_date, entry_nav]
    trips = []
    realized = 0.0
    last_sell_i = -99

    for i in range(a + 1, b + 1):
        r = ret7(i - 1)
        if r is None:
            continue
        px = nav[i]
        today = dates[i]

        # --- re-entry (buy back) ---
        keep = []
        for s in shorts:
            sh, spx, sd = s
            waited = (i - dates.index(sd)) if sd in dates else 99
            cond = px * (1 + cb) <= spx * (1 - trig / 100.0)
            if cond and waited >= settle:
                held = (today - sd).days
                pnl = sh * (spx - px * (1 + cb))
                pnl += sh * nav[dates.index(sd)] * drag * held / 365.0   # saved drag while out
                realized += pnl
                trips.append((sd.isoformat(), today.isoformat(), held, pnl,
                              100 * pnl / (sh * spx), 'target'))
                rebought.append([sh, px * (1 + cb), today, px])
            else:
                keep.append(s)
        shorts = keep

        # --- sell a new slice ---
        if r >= trig and len(shorts) < max_slices:
            sellable = True
            for lot in rebought:
                if (today - lot[2]).days < min_hold:
                    sellable = False       # freshly re-bought shares are locked
                    break
            if sellable:
                sh = SLICE / px
                shorts.append([sh, px * (1 - cs), today])
                last_sell_i = i
                if rebought:
                    rebought.pop(0)

    endpx = nav[b]
    open_pnl = 0.0
    open_detail = []
    for sh, spx, sd in shorts:
        held = (dates[b] - sd).days
        pnl = sh * (spx - endpx * (1 + cb))
        open_pnl += pnl
        open_detail.append((sd.isoformat(), held, 100 * pnl / (sh * spx)))
    return dict(trips=trips, realized=realized, open_pnl=open_pnl,
                open_detail=open_detail, blocked=0, n_open=len(shorts))


def describe(regime, name):
    td = sum(s[1] - s[0] + 1 for s in regime)
    cal = sum((dates[s[1]] - dates[s[0]]).days + 1 for s in regime)
    return '{}: {} segs, {} trading days ({:.1f} yr)'.format(name, len(regime), td, cal / 365.25)


def agg(regime, fn, trig, tool, **kw):
    tot_r = tot_o = 0.0
    trips = []
    blocked = 0
    n_open = 0
    for seg in regime:
        if seg[1] - seg[0] < 10:
            continue
        res = fn(seg, trig, tool, **kw)
        tot_r += res['realized']
        tot_o += res['open_pnl']
        trips += res['trips']
        blocked += res['blocked']
        n_open += res['n_open']
    td = sum(s[1] - s[0] + 1 for s in regime)
    yrs = td / 244.0
    wins = [t for t in trips if t[3] > 0]
    return dict(realized=tot_r, open_pnl=tot_o, total=tot_r + tot_o, trips=trips,
                ntrips=len(trips), win=100 * len(wins) / len(trips) if trips else 0,
                avg=statistics.mean([t[3] for t in trips]) if trips else 0,
                avgdays=statistics.mean([t[2] for t in trips]) if trips else 0,
                worst=min([t[3] for t in trips]) if trips else 0,
                best=max([t[3] for t in trips]) if trips else 0,
                yrs=yrs, per_year=(tot_r + tot_o) / yrs if yrs else 0,
                blocked=blocked, n_open=n_open,
                trips_per_year=len(trips) / yrs if yrs else 0)


print('=' * 96)
print('7-DAY SWING T   |  slice 5w  |  trigger 1%  |  min hold 7 calendar days')
print('=' * 96)
print(describe(FULL, 'FULL sample   '))
print(describe(BOTTOM, 'BOTTOM nav<=0.85 (>=20d segs)'))
print(describe(DEEP, 'DEEP   nav<=0.79 (>=15d segs)'))
print(describe(CURRENT, 'CURRENT 2026-05-12~'))
print()
print('BOTTOM segments:')
for a, b in BOTTOM:
    print('   {} ~ {}  {:>3}d  nav {:.4f} -> {:.4f}  ({:+.1f}%)  min {:.4f} max {:.4f}'
          .format(dates[a], dates[b], b - a + 1, nav[a], nav[b],
                  100 * (nav[b] / nav[a] - 1), min(nav[a:b + 1]), max(nav[a:b + 1])))

# ---------------------------------------------------------------- 1) cost table
print()
print('=' * 96)
print('ROUND-TRIP COST OF ONE 5w SLICE  (this is what the 7-day rule buys you)')
print('=' * 96)
print('{:<16} {:>12} {:>12} {:>12} {:>14}'.format(
    'tool', 'hold 3 days', 'hold 7 days', 'hold 14 days', 'hold 30 days'))
print('-' * 96)
for tool in ('OTC-A', 'OTC-A-full', 'OTC-C', 'ETF'):
    cb, cs, drag = cost_model(tool)
    line = '{:<16}'.format(tool)
    for H in (3, 7, 14, 30):
        sellfee = 0.015 if H < 7 else cs
        c = SLICE * (cb + sellfee + max(drag, 0) * H / 365.0)
        if drag < 0:
            c = SLICE * (cb + sellfee) + SLICE * drag * H / 365.0
        line += '{:>12}'.format('{:,.0f}'.format(c))
    print(line + '   CNY')
print()
print('  breakeven move needed to cover cost (hold 7-14 days):')
for tool in ('OTC-A', 'OTC-A-full', 'OTC-C', 'ETF'):
    cb, cs, drag = cost_model(tool)
    c = (cb + cs + max(drag, 0) * 10 / 365.0) + (drag * 10 / 365.0 if drag < 0 else 0)
    print('    {:<12} {:.4f}%   -> a 1% swing nets {:.4f}%'.format(tool, 100 * c, 1 - 100 * c))

# ---------------------------------------------------------------- 2) main test
print()
print('=' * 96)
print('LONG-T (buy dip with cash, sell on +1% after >=7 days)  --  BOTTOM regime')
print('=' * 96)
print('{:<10} {:<10} {:>8} {:>9} {:>9} {:>9} {:>7} {:>7} {:>8} {:>9}'.format(
    'trigger', 'tool', 'trips', 'realized', 'open', 'TOTAL', 'win%', 'days', 'trips/y', 'CNY/yr'))
print('-' * 96)
best = {}
for trig in (1.0, 1.5, 2.0, 3.0):
    for tool in ('OTC-A', 'OTC-C', 'ETF'):
        r = agg(BOTTOM, run_long_t, trig, tool, max_slices=3)
        print('{:<10} {:<10} {:>8} {:>9} {:>9} {:>9} {:>6.0f}% {:>7.1f} {:>8.1f} {:>9}'.format(
            '{:.1f}%'.format(trig), tool, r['ntrips'],
            '{:+,.0f}'.format(r['realized']), '{:+,.0f}'.format(r['open_pnl']),
            '{:+,.0f}'.format(r['total']), r['win'], r['avgdays'],
            r['trips_per_year'], '{:+,.0f}'.format(r['per_year'])))
        best[(trig, tool)] = r

print()
print('=' * 96)
print('REVERSE-T (sell core on +1%, buy back on -1%)  --  BOTTOM regime')
print('=' * 96)
print('{:<10} {:<10} {:>8} {:>9} {:>9} {:>9} {:>7} {:>7} {:>8} {:>9}'.format(
    'trigger', 'tool', 'trips', 'realized', 'open', 'TOTAL', 'win%', 'days', 'trips/y', 'CNY/yr'))
print('-' * 96)
for trig in (1.0, 1.5, 2.0, 3.0):
    for tool in ('OTC-A', 'OTC-C', 'ETF'):
        r = agg(BOTTOM, run_reverse_t, trig, tool, max_slices=3)
        print('{:<10} {:<10} {:>8} {:>9} {:>9} {:>9} {:>6.0f}% {:>7.1f} {:>8.1f} {:>9}'.format(
            '{:.1f}%'.format(trig), tool, r['ntrips'],
            '{:+,.0f}'.format(r['realized']), '{:+,.0f}'.format(r['open_pnl']),
            '{:+,.0f}'.format(r['total']), r['win'], r['avgdays'],
            r['trips_per_year'], '{:+,.0f}'.format(r['per_year'])))

# ---------------------------------------------------------------- 3) regime contrast
print()
print('=' * 96)
print('DOES THE BOTTOM REGIME ACTUALLY HELP?  (same rules, different regimes)')
print('=' * 96)
print('{:<28} {:>8} {:>10} {:>10} {:>7} {:>10} {:>10}'.format(
    'regime', 'trips', 'realized', 'TOTAL', 'win%', 'CNY/yr', '%of 27w/yr'))
print('-' * 96)
for reg, nm in ((FULL, 'FULL 2022-04~2026-09'), (BOTTOM, 'BOTTOM nav<=0.85'),
                (DEEP, 'DEEP nav<=0.79'), (CURRENT, 'CURRENT 2026-05-12~')):
    r = agg(reg, run_long_t, 1.0, 'OTC-C', max_slices=3)
    print('{:<28} {:>8} {:>10} {:>10} {:>6.0f}% {:>10} {:>9.2f}%'.format(
        nm, r['ntrips'], '{:+,.0f}'.format(r['realized']), '{:+,.0f}'.format(r['total']),
        r['win'], '{:+,.0f}'.format(r['per_year']), 100 * r['per_year'] / BASE))

# ---------------------------------------------------------------- 4) per segment
print()
print('=' * 96)
print('PER-SEGMENT DETAIL (long-T 1%, OTC-C) -- consistency check')
print('=' * 96)
print('{:<26} {:>5} {:>7} {:>10} {:>10} {:>10} {:>7} {:>8}'.format(
    'segment', 'days', 'navchg', 'realized', 'open', 'TOTAL', 'trips', 'win%'))
print('-' * 96)
seg_rows = []
for seg in BOTTOM:
    a, b = seg
    res = run_long_t(seg, 1.0, 'OTC-C', max_slices=3)
    tr = res['trips']
    w = 100 * sum(1 for t in tr if t[3] > 0) / len(tr) if tr else 0
    tot = res['realized'] + res['open_pnl']
    seg_rows.append((dates[a].isoformat(), dates[b].isoformat(), b - a + 1,
                     100 * (nav[b] / nav[a] - 1), res['realized'], res['open_pnl'],
                     tot, len(tr), w))
    print('{:<26} {:>5} {:>6.1f}% {:>10} {:>10} {:>10} {:>7} {:>7.0f}%'.format(
        '{}~{}'.format(dates[a], dates[b]), b - a + 1, 100 * (nav[b] / nav[a] - 1),
        '{:+,.0f}'.format(res['realized']), '{:+,.0f}'.format(res['open_pnl']),
        '{:+,.0f}'.format(tot), len(tr), w))
pos = sum(1 for r in seg_rows if r[6] > 0)
print()
print('  segments with TOTAL > 0 : {} / {}'.format(pos, len(seg_rows)))
print('  correlation with segment nav change:')
xs = [r[3] for r in seg_rows]
ys = [r[6] for r in seg_rows]
mx, my = statistics.mean(xs), statistics.mean(ys)
num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
den = (sum((x - mx) ** 2 for x in xs) * sum((y - my) ** 2 for y in ys)) ** 0.5
print('    corr(nav change, T pnl) = {:+.3f}'.format(num / den if den else 0))

# ---------------------------------------------------------------- 5) 7-day rule value
print()
print('=' * 96)
print('WHAT IS THE 7-DAY MINIMUM HOLD WORTH?  (long-T 1%, BOTTOM)')
print('=' * 96)
print('{:<34} {:>10} {:>9} {:>8} {:>8} {:>10}'.format(
    'variant', 'TOTAL', 'trips', 'win%', 'days', 'CNY/yr'))
print('-' * 96)
for mh, lbl, tool in ((0, 'no min hold (fee 1.5% applies)', 'OTC-A'),
                      (7, 'min hold 7d (fee 0)', 'OTC-A'),
                      (0, 'no min hold, C class', 'OTC-C'),
                      (7, 'min hold 7d, C class', 'OTC-C'),
                      (0, 'no min hold, ETF (no fee tier)', 'ETF'),
                      (7, 'min hold 7d, ETF', 'ETF')):
    # emulate the <7d penalty by charging 1.5% on early exits
    if mh == 0 and tool in ('OTC-A', 'OTC-C'):
        cbk, csk, drg = cost_model(tool)

        def patched(seg, trig, tl, **kw):
            a, b = seg
            lots, trips = [], []
            realized = 0.0
            for i in range(a + 1, b + 1):
                r = ret7(i - 1)
                if r is None:
                    continue
                px = nav[i]
                today = dates[i]
                keep = []
                for lot in lots:
                    sh, cps, ed, enav = lot
                    held = (today - ed).days
                    fee = 0.015 if held < 7 else csk
                    eff = px * (1 - fee)
                    if eff >= cps * (1 + trig / 100.0):
                        pnl = sh * eff - sh * enav * drg * held / 365.0 - sh * cps
                        realized += pnl
                        trips.append((ed.isoformat(), today.isoformat(), held, pnl,
                                      100 * pnl / (sh * cps), 'target'))
                    else:
                        keep.append(lot)
                lots = keep
                if r <= -trig and len(lots) < 3:
                    cps = px * (1 + cbk)
                    lots.append([SLICE / cps, cps, today, px])
            endpx = nav[b]
            op = 0.0
            for sh, cps, ed, enav in lots:
                held = (dates[b] - ed).days
                fee = 0.015 if held < 7 else csk
                op += sh * endpx * (1 - fee) - sh * enav * drg * held / 365.0 - sh * cps
            return dict(trips=trips, realized=realized, open_pnl=op, open_detail=[],
                        blocked=0, n_open=len(lots))
        r = agg(BOTTOM, patched, 1.0, tool)
    else:
        r = agg(BOTTOM, run_long_t, 1.0, tool, min_hold=mh)
    print('{:<34} {:>10} {:>9} {:>7.0f}% {:>8.1f} {:>10}'.format(
        lbl, '{:+,.0f}'.format(r['total']), r['ntrips'], r['win'], r['avgdays'],
        '{:+,.0f}'.format(r['per_year'])))

# ---------------------------------------------------------------- 6) risk
print()
print('=' * 96)
print('THE RISK NOBODY PRICES: CAPITAL LOCK-UP  (long-T 1%, OTC-C, BOTTOM)')
print('=' * 96)
allt = []
maxopen = 0
for seg in BOTTOM:
    res = run_long_t(seg, 1.0, 'OTC-C', max_slices=3)
    allt += res['trips']
    for od in res['open_detail']:
        print('  segment-end unclosed lot: bought {}  held {:>3}d  mark {:+.2f}%'
              .format(od[0], od[1], od[2]))
if allt:
    hd = sorted(t[2] for t in allt)
    print()
    print('  round-trip holding days: min {} / median {} / p90 {} / max {}'
          .format(hd[0], hd[len(hd) // 2], hd[int(len(hd) * 0.9)], hd[-1]))
    rs = sorted(t[4] for t in allt)
    print('  per-trip return %: min {:+.2f} / median {:+.2f} / max {:+.2f}'
          .format(rs[0], rs[len(rs) // 2], rs[-1]))
    print('  trips that took >30 days to close: {} / {} ({:.0f}%)'
          .format(sum(1 for x in hd if x > 30), len(hd),
                  100 * sum(1 for x in hd if x > 30) / len(hd)))
    print('  trips that took >60 days to close: {} / {} ({:.0f}%)'
          .format(sum(1 for x in hd if x > 60), len(hd),
                  100 * sum(1 for x in hd if x > 60) / len(hd)))

# ---------------------------------------------------------------- 7) current window
print()
print('=' * 96)
print('THE CURRENT BOTTOM WINDOW 2026-05-12 ~ 2026-09-03 (82 trading days)')
print('=' * 96)
for tool in ('OTC-A', 'OTC-C', 'ETF'):
    r = agg(CURRENT, run_long_t, 1.0, tool, max_slices=3)
    print('  long-T 1%  {:<7} TOTAL {:>9}  (realized {:>8} / open {:>8})  trips {}  win {:.0f}%'
          .format(tool, '{:+,.0f}'.format(r['total']), '{:+,.0f}'.format(r['realized']),
                  '{:+,.0f}'.format(r['open_pnl']), r['ntrips'], r['win']))
print()
res = run_long_t(CURRENT[0], 1.0, 'OTC-C', max_slices=3)
print('  fills (OTC-C, trigger 1%):')
for t in res['trips']:
    print('    buy {}  ->  sell {}   held {:>3}d   {:>8} CNY  ({:+.2f}%)  [{}]'
          .format(t[0], t[1], t[2], '{:+,.0f}'.format(t[3]), t[4], t[5]))
for od in res['open_detail']:
    print('    buy {}  ->  STILL OPEN  held {:>3}d  mark {:+.2f}%'.format(od[0], od[1], od[2]))
print()
bh = BASE * (nav[n - 1] / nav[CUR_START] - 1)
print('  core position 27w buy&hold over the same window: {:+,.0f} CNY ({:+.2f}%)'
      .format(bh, 100 * (nav[n - 1] / nav[CUR_START] - 1)))
