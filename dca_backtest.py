# -*- coding: utf-8 -*-
"""
DCA backtest for 003095 (中欧医疗健康混合A) / 003096 (C 类)
==========================================================
Question: over the last 3 years, which DCA cadence + which sell-timing rule
produced the best result, with buy/redeem fees fully priced in?

Fee model (verified on 天天基金 fundf10 rate page, 2026-08-31):
  A (003095): subscription 1.50% list / 0.15% discounted(1折);
  C (003096): subscription 0.00%, sales service fee 0.80%/yr (already inside NAV)
  Redeem (both classes, FIFO by holding days, 天天基金 disclosed table):
      <7d 1.50% | 7-30d 0.75% | 30-365d 0.50% | 365-730d 0.25% | >=730d 0.00%

Outputs: data/dca_result.json  (consumed by dca_report.py)
"""
import json, os, sqlite3, datetime, math, itertools

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE, "data", "fund_history.db")
OUT_JSON = os.path.join(BASE, "data", "dca_result.json")

# ---------------------------------------------------------------- fee model
REDEEM_TIERS = [(7, 0.0150), (30, 0.0075), (365, 0.0050), (730, 0.0025), (10 ** 9, 0.0)]


def redeem_rate(days):
    for limit, rate in REDEEM_TIERS:
        if days < limit:
            return rate
    return 0.0


class FeePlan:
    def __init__(self, code, name, buy_rate, note=""):
        self.code, self.name, self.buy_rate, self.note = code, name, buy_rate, note


PLANS = {
    "A_disc": FeePlan("003095", "A类(1折 0.15%)", 0.0015, "第三方平台1折申购, 天天基金披露赎回费表"),
    "A_full": FeePlan("003095", "A类(原价 1.50%)", 0.0150, "银行/直销原价申购"),
    "C": FeePlan("003096", "C类(0申购费)", 0.0000, "无申购费, 销售服务费0.80%/年已内含于净值"),
    "ZERO": FeePlan("003095", "零费率(理论上限)", 0.0000, "假设申赎全免, 用于量化费率拖累"),
}


# ---------------------------------------------------------------- portfolio
CASH_RATE = 0.015   # 1.5%/yr on banked proceeds (money market), avoids inflating XIRR


class Portfolio:
    """FIFO lot book. cost = money actually paid out of pocket (incl. buy fee)."""

    def __init__(self, buy_rate):
        self.buy_rate = buy_rate
        self.lots = []          # [{date, shares, cost}]
        self.cost = 0.0         # open-position cost basis
        self.invested = 0.0     # cumulative money in (never decreases)
        self.cash = 0.0         # realized proceeds banked
        self.cash_events = []   # [(date, amount)] for interest accrual
        self.buy_fee = 0.0
        self.sell_fee = 0.0
        self.sell_count = 0

    def interest(self, end_date):
        return sum(a * CASH_RATE * (end_date - d).days / 365.0 for d, a in self.cash_events)

    def buy(self, date, amount, nav):
        fee = amount - amount / (1 + self.buy_rate)      # front-end load
        net = amount - fee
        shares = net / nav
        self.lots.append({"date": date, "shares": shares, "cost": amount})
        self.cost += amount
        self.invested += amount
        self.buy_fee += fee
        return shares

    def park(self, date, amount):
        """budget not deployed this period: sits in cash, earns CASH_RATE"""
        self.cash += amount
        self.cash_events.append((date, amount))
        self.invested += amount

    def market_value(self, nav):
        return sum(l["shares"] for l in self.lots) * nav

    def ret(self, nav):
        """unrealized return on open position, before redeem fee"""
        if self.cost <= 0:
            return 0.0
        return (self.market_value(nav) - self.cost) / self.cost

    def sell_all(self, date, nav):
        return self._sell(date, nav, 1.0)

    def _sell(self, date, nav, fraction):
        """redeem `fraction` of current shares, FIFO lot order, per-lot redeem fee"""
        if not self.lots or fraction <= 0:
            return 0.0
        total_shares = sum(l["shares"] for l in self.lots)
        target = total_shares * min(fraction, 1.0)
        proceeds, fee_total, remaining = 0.0, 0.0, []
        for lot in self.lots:
            if target <= 1e-12:
                remaining.append(lot)
                continue
            take = min(lot["shares"], target)
            cost_share = lot["cost"] * (take / lot["shares"])     # must use pre-take shares
            days = (date - lot["date"]).days
            rate = 0.0 if PLAN_ZERO else redeem_rate(days)
            amount = take * nav
            fee = amount * rate
            proceeds += amount - fee
            fee_total += fee
            lot["shares"] -= take
            lot["cost"] -= cost_share
            target -= take
            if lot["shares"] > 1e-10:
                remaining.append(lot)
        self.lots = remaining
        self.cost = sum(l["cost"] for l in self.lots)
        self.cash += proceeds
        self.cash_events.append((date, proceeds))
        self.sell_fee += fee_total
        self.sell_count += 1
        return proceeds

    def sell_lot(self, date, nav, lot):
        days = (date - lot["date"]).days
        rate = 0.0 if PLAN_ZERO else redeem_rate(days)
        amount = lot["shares"] * nav
        fee = amount * rate
        self.lots.remove(lot)
        self.cash += amount - fee
        self.cash_events.append((date, amount - fee))
        self.sell_fee += fee
        self.cost -= lot["cost"]
        self.sell_count += 1
        return amount - fee

    def liquidate(self, date, nav):
        """sell everything left at window end"""
        if not self.lots:
            return 0.0
        total_shares = sum(l["shares"] for l in self.lots)
        proceeds, fee_total = 0.0, 0.0
        for lot in self.lots:
            days = (date - lot["date"]).days
            rate = 0.0 if PLAN_ZERO else redeem_rate(days)
            amount = lot["shares"] * nav
            fee = amount * rate
            proceeds += amount - fee
            fee_total += fee
        self.lots = []
        self.cost = 0.0
        self.cost = 0.0
        self.cash += proceeds
        self.sell_fee += fee_total
        self.sell_count += 1
        return proceeds


PLAN_ZERO = False  # toggled by caller for the theoretical zero-fee run


# ---------------------------------------------------------------- XIRR
def xirr(cfs, guess=0.1):
    """cfs: [(date, amount)] negative = money out. Returns annualized IRR."""
    if len(cfs) < 2:
        return None
    d0 = min(d for d, _ in cfs)
    ts = [((d - d0).days / 365.0, a) for d, a in cfs]

    def npv(r):
        s = 0.0
        for t, a in ts:
            s += a / ((1 + r) ** t) if (1 + r) > 0 else 0.0
        return s

    def dnpv(r):
        s = 0.0
        for t, a in ts:
            s += -a * t / ((1 + r) ** (t + 1)) if (1 + r) > 0 else 0.0
        return s

    r = guess
    for _ in range(200):
        try:
            f, df = npv(r), dnpv(r)
        except (OverflowError, ZeroDivisionError):
            return None
        if abs(df) < 1e-12:
            break
        nr = r - f / df
        if nr <= -0.9999:
            nr = -0.9
        if abs(nr - r) < 1e-8:
            return nr
        r = nr
        if not math.isfinite(r):
            return None
    return r if abs(npv(r)) < 1e-3 else None


# ---------------------------------------------------------------- cadence
def gen_invest_dates(trade_days, freq):
    """freq: ('daily',None) ('weekly',0..4) ('biweekly',0..4) ('monthly',int|'last')"""
    kind, arg = freq
    out, seen_month, seen_week = [], set(), set()
    for d in trade_days:
        if kind == "daily":
            out.append(d)
        elif kind == "weekly":
            if d.weekday() == arg:
                out.append(d)
        elif kind == "biweekly":
            key = d.isocalendar()[:2]
            if d.weekday() == arg and key not in seen_week:
                seen_week.add(key)
                if len(seen_week) % 2 == 1:
                    out.append(d)
        elif kind == "monthly":
            key = (d.year, d.month)
            if arg == "last":
                out.append(d)          # provisional; trimmed below
                seen_month.add(key)
            else:
                if d.day >= arg and key not in seen_month:
                    out.append(d)
                    seen_month.add(key)
    if kind == "monthly" and arg == "last":
        # keep only the last trade day of each month
        last_of_month = {}
        for d in out:
            last_of_month[(d.year, d.month)] = d
        out = sorted(last_of_month.values())
    return out


CADENCES = [
    ("daily", None, "每日"),
    ("weekly", 0, "每周一"), ("weekly", 1, "每周二"), ("weekly", 2, "每周三"),
    ("weekly", 3, "每周四"), ("weekly", 4, "每周五"),
    ("biweekly", 0, "双周一"), ("biweekly", 3, "双周四"),
    ("monthly", 1, "每月1日"), ("monthly", 5, "每月5日"), ("monthly", 10, "每月10日"),
    ("monthly", 15, "每月15日"), ("monthly", 20, "每月20日"), ("monthly", 25, "每月25日"),
    ("monthly", "last", "每月末"),
]


# ---------------------------------------------------------------- strategies
def build_strategies():
    S = [("hold", "不止盈(持有到期)", {})]
    for x in (10, 15, 20, 25, 30, 35, 40, 50, 60, 80):
        S.append((f"target{x}", f"目标+{x}%清仓", {"target": x / 100}))
    for x in (5, 8, 10, 12, 15, 20):
        S.append((f"dd{x}", f"净值回撤{x}%清仓", {"dd": x / 100}))
    for x, y in itertools.product((20, 30, 40), (5, 10)):
        S.append((f"trail{x}_{y}", f"先涨{x}%后回撤{y}%清仓", {"trail_up": x / 100, "trail_dn": y / 100}))
    S.append(("staged", "分批止盈(+20%卖1/3,+40%卖1/2,+60%清仓)", {"staged": True}))
    for x in (80, 85, 90, 95):
        S.append((f"q{x}", f"净值近3年分位≥{x}%清仓", {"quantile": x}))
    # 复合: 高位(分位) + 已有盈利, 更贴近实操的"只在贵且赚的时候卖"
    S.append(("q90r30", "净值分位≥90% 且 盈利≥30% 清仓", {"quantile": 90, "min_ret": 0.30}))
    S.append(("q85r20", "净值分位≥85% 且 盈利≥20% 清仓", {"quantile": 85, "min_ret": 0.20}))
    S.append(("q80r15", "净值分位≥80% 且 盈利≥15% 清仓", {"quantile": 80, "min_ret": 0.15}))
    S.append(("q90_half", "净值分位≥90% 且 盈利>0 减仓一半", {"quantile": 90, "min_ret": 0.0, "half": True}))
    S.append(("hold365_5", "每笔满365天且盈利≥5%赎回该笔", {"lot365": 0.05}))
    return S


STRATEGIES = build_strategies()


# ---------------------------------------------------------------- core run
def ma_bias_multiplier(navs, i, window=250):
    """buy more when NAV is below its 250d average, less/skip when far above"""
    if i < window:
        return 1.0
    ma = sum(navs[i - window + 1:i + 1]) / window
    bias = navs[i] / ma - 1.0
    if bias <= -0.20:
        return 2.0
    if bias <= -0.10:
        return 1.5
    if bias >= 0.20:
        return 0.0
    if bias >= 0.10:
        return 0.5
    return 1.0


def run_backtest(nav_series, invest_dates, strategy, plan, amount=None, budget=120000.0,
                 amount_rule="fixed"):
    """nav_series: [(date, nav)] ascending.
    Fixed total budget split evenly across all installments so that every
    cadence invests exactly the same amount of money -> ROI is comparable.
    amount_rule: 'fixed' | 'ma_bias' (deploy more when NAV is below its 250d MA;
    the undepoyed part of each instalment is parked in cash earning CASH_RATE).
    """
    global PLAN_ZERO
    PLAN_ZERO = (plan.note.startswith("假设申赎全免"))
    sid, sname, p = strategy
    per = amount if amount else (budget / max(1, len(invest_dates)))
    pf = Portfolio(plan.buy_rate)
    cashflows = []
    invest_set = set(invest_dates)

    navs = [n for _, n in nav_series]
    # rolling 750-day window for quantile rule
    peak_nav = navs[0]
    peak_ret = 0.0
    staged_step = 0
    self_last_q = {}   # cooldown guard for quantile rules

    for i, (d, nav) in enumerate(nav_series):
        if d in invest_set:
            mult = ma_bias_multiplier(navs, i) if amount_rule == "ma_bias" else 1.0
            deploy = per * mult
            if deploy > 1e-9:
                pf.buy(d, deploy, nav)
            if per - deploy > 1e-9:
                pf.park(d, per - deploy)
            cashflows.append((d, -per))

        # ---- state update
        if pf.lots:
            peak_nav = max(peak_nav, nav)
            r = pf.ret(nav)
            peak_ret = max(peak_ret, r)
        else:
            peak_nav = nav
            peak_ret = 0.0
            staged_step = 0

        if not pf.lots:
            continue

        # ---- sell rules (evaluated on that day's NAV)
        if sid == "hold":
            pass
        elif "target" in p:
            if pf.ret(nav) >= p["target"]:
                cashflows.append((d, pf.sell_all(d, nav)))
                peak_nav, peak_ret, staged_step = nav, 0.0, 0
        elif "dd" in p:
            if peak_nav > 0 and nav <= peak_nav * (1 - p["dd"]) and pf.ret(nav) > 0:
                cashflows.append((d, pf.sell_all(d, nav)))
                peak_nav, peak_ret = nav, 0.0
        elif "trail_up" in p:
            r = pf.ret(nav)
            if peak_ret >= p["trail_up"] and r <= peak_ret - p["trail_dn"]:
                cashflows.append((d, pf.sell_all(d, nav)))
                peak_nav, peak_ret = nav, 0.0
        elif "staged" in p:
            r = pf.ret(nav)
            if staged_step == 0 and r >= 0.20:
                cashflows.append((d, pf._sell(d, nav, 1 / 3)))
                staged_step = 1
                peak_ret = pf.ret(nav)
            elif staged_step == 1 and r >= 0.40:
                cashflows.append((d, pf._sell(d, nav, 1 / 2)))
                staged_step = 2
                peak_ret = pf.ret(nav)
            elif staged_step == 2 and r >= 0.60:
                cashflows.append((d, pf.sell_all(d, nav)))
                staged_step = 0
                peak_nav = nav
        elif "quantile" in p:
            if i >= 250 and i - self_last_q.get("i", -999) >= 60:
                win = navs[max(0, i - 750):i + 1]
                rank = sum(1 for v in win if v <= nav) / len(win) * 100
                if rank >= p["quantile"] and pf.ret(nav) >= p.get("min_ret", 0.0):
                    if p.get("half"):
                        cashflows.append((d, pf._sell(d, nav, 0.5)))
                    else:
                        cashflows.append((d, pf.sell_all(d, nav)))
                        peak_nav, peak_ret = nav, 0.0
                    self_last_q["i"] = i
        elif "lot365" in p:
            for lot in list(pf.lots):
                if (d - lot["date"]).days >= 365:
                    lot_ret = (nav * lot["shares"] - lot["cost"]) / lot["cost"]
                    if lot_ret >= p["lot365"]:
                        cashflows.append((d, pf.sell_lot(d, nav, lot)))

    # ---- liquidate at end + accrue interest on banked proceeds
    last_d, last_nav = nav_series[-1]
    interest = pf.interest(last_d)
    last_cf = pf.liquidate(last_d, last_nav) if pf.lots else 0.0
    cashflows.append((last_d, last_cf + interest))

    total_value = pf.cash + interest
    invested = pf.invested
    profit = total_value - invested
    irr = xirr([(d, a) for d, a in cashflows if abs(a) > 1e-9])
    return {
        "sid": sid, "sname": sname, "invested": round(invested, 2),
        "final_value": round(total_value, 2), "profit": round(profit, 2),
        "roi": round(profit / invested * 100, 2) if invested else 0.0,
        "xirr": round(irr * 100, 2) if irr is not None else None,
        "buy_fee": round(pf.buy_fee + pf.sell_fee, 2),
        "sell_count": pf.sell_count,
        "cashflows": [(d.isoformat(), round(a, 2)) for d, a in cashflows],
    }


# ---------------------------------------------------------------- lump sum
def run_lumpsum(nav_series, plan, budget=120000.0):
    global PLAN_ZERO
    PLAN_ZERO = (plan.note.startswith("假设申赎全免"))
    pf = Portfolio(plan.buy_rate)
    d0, nav0 = nav_series[0]
    pf.buy(d0, budget, nav0)
    last_d, last_nav = nav_series[-1]
    cf = [(d0, -budget), (last_d, pf.liquidate(last_d, last_nav))]
    val = pf.cash
    irr = xirr(cf)
    return {"sid": "lumpsum", "sname": "期初一次性买入",
            "invested": round(pf.invested, 2), "final_value": round(val, 2),
            "profit": round(val - pf.invested, 2),
            "roi": round((val - pf.invested) / pf.invested * 100, 2),
            "xirr": round(irr * 100, 2) if irr is not None else None,
            "buy_fee": round(pf.buy_fee + pf.sell_fee, 2), "sell_count": 1,
            "cashflows": [(d.isoformat(), round(a, 2)) for d, a in cf]}


# ---------------------------------------------------------------- robustness
def rolling_windows(series, years=3, step_months=3):
    """3-year windows, start points every step_months, over the whole history"""
    if not series:
        return []
    first, last = series[0][0], series[-1][0]
    out = []
    y, m, d = first.year, first.month, first.day
    cur = first
    while True:
        try:
            end = datetime.date(cur.year + years, cur.month, min(cur.day, 28))
        except ValueError:
            break
        if end > last:
            break
        win = [(dd, v) for dd, v in series if cur <= dd <= end]
        if len(win) > 500:
            out.append((cur, end, win))
        m2 = cur.month + step_months
        y2 = cur.year + (m2 - 1) // 12
        m2 = (m2 - 1) % 12 + 1
        try:
            cur = datetime.date(y2, m2, min(cur.day, 28))
        except ValueError:
            break
    return out


# ---------------------------------------------------------------- data
def load_series(code, start, end):
    c = sqlite3.connect(DB_PATH)
    rows = c.execute("SELECT trade_date, dwjz FROM nav_history WHERE fund_code=? AND trade_date BETWEEN ? AND ? ORDER BY trade_date",
                     (code, start, end)).fetchall()
    c.close()
    out = []
    for d, v in rows:
        if v:
            out.append((datetime.date.fromisoformat(d), float(v)))
    return out


# ---------------------------------------------------------------- main
def main():
    START, END = "2023-08-31", "2026-08-28"
    series = load_series("003095", START, END)
    print(f"回测区间 {START} ~ {END}, 交易日 {len(series)} 天, "
          f"净值 {series[0][1]} -> {series[-1][1]}")

    plan = PLANS["A_disc"]
    results = {}
    for kind, arg, label in CADENCES:
        ids = gen_invest_dates([d for d, _ in series], (kind, arg))
        for strat in STRATEGIES:
            res = run_backtest(series, ids, strat, plan)
            results[f"{label}|{strat[0]}"] = res
        print(f"  {label}: 投入期数 {len(ids)}")

    # zero-fee / A-full / C comparison on a couple of representative cadences
    fee_cmp = {}
    for label in ("每月15日", "每周四", "每日"):
        ids = gen_invest_dates([d for d, _ in series],
                               next((k, a) for k, a, l in CADENCES if l == label))
        for pkey in ("A_disc", "A_full", "C", "ZERO"):
            pl = PLANS[pkey]
            code = pl.code
            ser = series if code == "003095" else load_series(code, START, END)
            r = run_backtest(ser, ids, ("hold", "不止盈", {}), pl)
            fee_cmp[f"{label}|{pkey}|hold"] = r
            r2 = run_backtest(ser, ids, ("target25", "目标+25%", {"target": 0.25}), pl)
            fee_cmp[f"{label}|{pkey}|target25"] = r2
            r3 = run_backtest(ser, ids, ("dd12", "回撤12%", {"dd": 0.12}), pl)
            fee_cmp[f"{label}|{pkey}|dd12"] = r3

    lump = run_lumpsum(series, plan)

    # ---- robustness: 3-year rolling windows over full history
    full = load_series("003095", "2016-09-29", "2026-08-28")
    wins = rolling_windows(full, years=3, step_months=3)
    print(f"\n稳健性检验: {len(wins)} 个 3 年窗口 "
          f"({wins[0][0]}~{wins[0][1]} ... {wins[-1][0]}~{wins[-1][1]})")
    rep_cad = [("daily", None, "每日"), ("weekly", 3, "每周四"), ("monthly", 15, "每月15日")]
    rob, market = {}, {}
    for kind, arg, label in rep_cad:
        for w_start, w_end, win in wins:
            ids = gen_invest_dates([d for d, _ in win], (kind, arg))
            base = run_backtest(win, ids, ("hold", "不止盈", {}), plan)
            ls = run_lumpsum(win, plan)
            market[w_start.isoformat()] = {"end": w_end.isoformat(), "bh": ls["roi"]}
            for strat in STRATEGIES:
                r = run_backtest(win, ids, strat, plan)
                key = f"{label}|{strat[0]}"
                rob.setdefault(key, []).append({
                    "start": w_start.isoformat(), "end": w_end.isoformat(),
                    "roi": r["roi"], "base_roi": base["roi"],
                    "excess": round(r["roi"] - base["roi"], 2), "xirr": r["xirr"],
                    "bh": ls["roi"], "fee": r["buy_fee"]})
            # amount rule (smart DCA) on the monthly cadence
            if label == "每月15日":
                base_m = run_backtest(win, ids, ("hold", "不止盈", {}), plan, amount_rule="ma_bias")
                for strat in STRATEGIES:
                    r = run_backtest(win, ids, strat, plan, amount_rule="ma_bias")
                    key = f"每月15日·低位加码|{strat[0]}"
                    rob.setdefault(key, []).append({
                        "start": w_start.isoformat(), "end": w_end.isoformat(),
                        "roi": r["roi"], "base_roi": base_m["roi"],
                        "excess": round(r["roi"] - base_m["roi"], 2), "xirr": r["xirr"],
                        "bh": ls["roi"], "fee": r["buy_fee"]})
        print(f"  {label} done", end="  ")
    print()

    # ---- amount-rule grid (main window)
    ama = {}
    for kind, arg, label in rep_cad:
        ids = gen_invest_dates([d for d, _ in series], (kind, arg))
        for rule in ("fixed", "ma_bias"):
            for strat in STRATEGIES:
                r = run_backtest(series, ids, strat, plan, amount_rule=rule)
                ama[f"{label}|{rule}|{strat[0]}"] = r
    print("  amount-rule grid done")

    # ---- cadence spread: hold strategy, all 15 cadences, all windows
    cad_spread = {}
    for kind, arg, label in CADENCES:
        rois = []
        for w_start, w_end, win in wins:
            ids = gen_invest_dates([d for d, _ in win], (kind, arg))
            r = run_backtest(win, ids, ("hold", "不止盈", {}), plan)
            rois.append(r["roi"])
        mean = sum(rois) / len(rois)
        var = sum((x - mean) ** 2 for x in rois) / len(rois)
        cad_spread[label] = {"mean": round(mean, 2), "std": round(var ** 0.5, 2),
                             "min": round(min(rois), 2), "max": round(max(rois), 2)}
    print("  cadence spread done")

    # ---- robustness summary, split by market regime
    def summarize(rob, label="每月15日"):
        out = {}
        for k, arr in rob.items():
            if not k.startswith(label + "|"):
                continue
            sid = k.split("|")[1]
            groups = {"all": arr,
                      "bull": [a for a in arr if a["bh"] > 50],
                      "bear": [a for a in arr if a["bh"] < -20],
                      "range": [a for a in arr if -20 <= a["bh"] <= 50]}
            rec = {}
            for g, rows in groups.items():
                if not rows:
                    continue
                ex = [a["excess"] for a in rows]
                rec[g] = {"n": len(rows), "avg": round(sum(ex) / len(ex), 2),
                          "med": round(sorted(ex)[len(ex) // 2], 2),
                          "win": round(sum(1 for e in ex if e > 0) / len(ex) * 100, 1),
                          "max": round(max(ex), 2), "min": round(min(ex), 2)}
            out[sid] = rec
        return out

    rob_sum = summarize(rob, "每月15日")
    rob_sum_ama = summarize(rob, "每月15日·低位加码")

    payload = {"meta": {"start": START, "end": END, "days": len(series),
                        "nav_start": series[0][1], "nav_end": series[-1][1],
                        "nav_max": max(v for _, v in series),
                        "nav_min": min(v for _, v in series),
                        "budget": 120000,
                        "generated": datetime.datetime.now().strftime("%Y-%m-%d %H:%M")},
               "cadences": [{"kind": k, "arg": a, "label": l} for k, a, l in CADENCES],
               "strategies": [{"sid": s[0], "name": s[1]} for s in STRATEGIES],
               "grid": results, "fee_cmp": fee_cmp, "lumpsum": lump,
               "robust": rob, "market": market, "cad_spread": cad_spread,
               "rob_sum": rob_sum, "rob_sum_ama": rob_sum_ama, "ama": ama,
               "nav_series": [(d.isoformat(), v) for d, v in series]}
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    print("written", OUT_JSON)

    print(f"\n=== 一次性买入基准: ROI={lump['roi']}% XIRR={lump['xirr']}% 费用={lump['buy_fee']}")
    print("\n=== 近三年组合 TOP15 (按ROI, 总投入均为12万) ===")
    rows = sorted(results.items(), key=lambda kv: -kv[1]["roi"])
    for k, r in rows[:15]:
        print(f"  {k:<22} roi={r['roi']:>7.2f}%  xirr={r['xirr']:>7.2f}%  "
              f"期末={r['final_value']:>10.2f}  费用={r['buy_fee']:>7.2f}  卖出={r['sell_count']}")
    print("\n=== 不止盈基准(各频率) ===")
    for k, v in results.items():
        if v["sid"] == "hold":
            print(f"  {k.split('|')[0]:<8} roi={v['roi']:>7.2f}% xirr={v['xirr']:>7.2f}% 费用={v['buy_fee']}")
    print("\n=== 频率影响(28窗口, 不止盈): 各频率ROI均值/标准差 ===")
    for label, s in sorted(cad_spread.items(), key=lambda x: -x[1]["mean"]):
        print(f"  {label:<8} 均值={s['mean']:>6.2f}%  std={s['std']:>5.2f}  "
              f"区间[{s['min']:.1f}%, {s['max']:.1f}%]")
    means = [s["mean"] for s in cad_spread.values()]
    print(f"  >>> 频率间均值极差 = {max(means)-min(means):.2f} 个百分点")

    print("\n=== 金额策略(低位加码 vs 固定金额), 近三年主窗口 ===")
    for cad in ("每日", "每周四", "每月15日"):
        for sid in ("hold", "target25", "dd12", "q90r30"):
            a = ama.get(f"{cad}|fixed|{sid}")
            b = ama.get(f"{cad}|ma_bias|{sid}")
            if a and b:
                print(f"  {cad:<8} {sid:<10} 固定={a['roi']:>7.2f}%  低位加码={b['roi']:>7.2f}%  "
                      f"差={b['roi']-a['roi']:>+6.2f}pct")
    print("\n=== 多窗口稳健性(每月15日): 按市场状态分组的超额ROI ===")
    hdr = f"  {'策略':<12}{'全样本均值':>10}{'胜率':>7} |{'牛市均值':>9}{'胜率':>7} |{'震荡均值':>9}{'胜率':>7} |{'熊市均值':>9}{'胜率':>7}"
    print(hdr)
    def dump_block(title, data):
        print(f"\n{title}")
        print(hdr)
        for sid, rec in sorted(data.items(), key=lambda kv: -kv[1]["all"]["avg"]):
            def f(g):
                return (f"{rec[g]['avg']:>8.1f}%{rec[g]['win']:>6.0f}%" if g in rec
                        else f"{'-':>9}{'-':>7}")
            print(f"  {sid:<12}" + f("all") + " |" + f("bull") + " |" + f("range") + " |" + f("bear"))

    dump_block("=== 多窗口稳健性(每月15日·固定金额): 按市场状态分组的超额ROI ===", rob_sum)
    dump_block("=== 多窗口稳健性(每月15日·低位加码): 按市场状态分组的超额ROI ===", rob_sum_ama)

    print("\n=== 费率对比 ===")
    for k, v in fee_cmp.items():
        cad, plan_key, sid = k.split("|")
        print(f"  {cad:<8} {plan_key:<7} {sid:<9} ROI={v['roi']:>7.2f}% 费用={v['buy_fee']:>7.2f}")


if __name__ == "__main__":
    main()
